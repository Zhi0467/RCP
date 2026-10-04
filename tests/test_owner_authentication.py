from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rcp.__main__ import instance_lock
from rcp.api import create_app
from rcp.api.identity import OWNER_SESSION_COOKIE
from rcp.limits import TEAM_CODE_FAILED_ATTEMPT_LIMIT, TEAM_PUBLIC_AUTH_REQUEST_MAX_BYTES
from rcp.storage import AppStore, TeamAuthenticationError
from tests.helpers import signed_in_client


def test_personal_http_and_socket_admission(tmp_path):
    app = create_app(data_dir=tmp_path)
    with TestClient(app) as client:
        for path in ("/api/projects", "/api/identity", "/api/health/details", "/openapi.json"):
            assert client.get(path).status_code == 401
        health = client.get("/api/health").json()
        assert {"status", "version", "instance_id", "data_dir_id"} <= health.keys()
        assert (
            not {"project", "projects", "space_name", "active_agent_tasks", "project_creation"}
            & health.keys()
        )
        with (
            pytest.raises(WebSocketDisconnect) as error,
            client.websocket_connect(
                "/api/projects/missing/terminals/missing/ws",
                headers={"origin": "http://testserver"},
            ),
        ):
            pass
        assert error.value.code == 4401
    with signed_in_client(app) as client:
        assert client.get("/api/projects").status_code == 200
        assert client.get("/api/health/details").status_code == 200


def test_enrollment_requires_held_matching_lock_and_never_replaces(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    secret = secrets.token_urlsafe(32)
    with instance_lock(tmp_path) as ownership:
        assert store.enroll_owner_secret(secret, ownership=ownership)
        assert not store.enroll_owner_secret(secrets.token_urlsafe(32), ownership=ownership)
    with (tmp_path / "rcp.lock").open("r+") as unlocked, pytest.raises(ValueError):
        store.enroll_owner_secret(secret, ownership=unlocked)
    with instance_lock(tmp_path / "other") as wrong, pytest.raises(ValueError):
        store.enroll_owner_secret(secret, ownership=wrong)
    session, owner = store.create_owner_session(secret)
    assert store.resolve_owner_session(session) == owner
    assert store.resolve_team_session(session) is None
    assert secret.encode() not in store.path.read_bytes()


def test_exchange_redemption_logout_and_revocation(tmp_path):
    app = create_app(data_dir=tmp_path)
    store = app.state.background_tasks.store
    secret = secrets.token_urlsafe(32)
    with TestClient(app) as client:
        assert client.post("/api/owner/exchange", json={"secret": secret}).status_code == 401
        code = store.create_owner_sign_in_code()
        response = client.post("/api/owner/redeem", json={"code": code, "secret": secret})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Domain=" not in cookie and "Secure" not in cookie
        first = client.cookies.get(OWNER_SESSION_COOKIE)
        assert client.post("/api/owner/redeem", json={"code": code}).status_code == 409
        response = client.post("/api/owner/exchange", json={"secret": secret})
        assert response.status_code == 200
        previous = next(
            item
            for item in client.get("/api/owner/sessions").json()["sessions"]
            if not item["is_current"]
        )
        assert client.delete(f"/api/owner/sessions/{previous['session_id']}").status_code == 200
        assert store.resolve_owner_session(first) is None
        assert client.post("/api/owner/logout", json={}).status_code == 200
        assert client.get("/api/projects").status_code == 401
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/owner/exchange", json={"secret": secret})
        assert "Secure" in response.headers["set-cookie"]


def test_codes_expire_and_lock_without_rolling_back_failed_attempts(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    code = store.create_owner_sign_in_code()
    now = datetime.fromisoformat(store.now())
    monkeypatch.setattr(store, "now", lambda: (now + timedelta(minutes=11)).isoformat())
    with pytest.raises(TeamAuthenticationError) as error:
        store.redeem_owner_sign_in_code(code)
    assert error.value.code == "owner_code_expired"
    code = store.create_owner_sign_in_code()
    wrong = code[:-1] + ("A" if code[-1] != "A" else "B")
    for _ in range(TEAM_CODE_FAILED_ATTEMPT_LIMIT):
        with pytest.raises(TeamAuthenticationError):
            store.redeem_owner_sign_in_code(wrong)
    with pytest.raises(TeamAuthenticationError) as error:
        store.redeem_owner_sign_in_code(code)
    assert error.value.code == "owner_code_locked"


def test_public_bodies_origin_and_snapshot_exclusion(tmp_path):
    app = create_app(data_dir=tmp_path)
    store = app.state.background_tasks.store
    with TestClient(app) as client:
        code = store.create_owner_sign_in_code()
        secret = secrets.token_urlsafe(32)
        assert (
            client.post(
                "/api/owner/redeem",
                json={"code": code},
                headers={"origin": "https://other.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/owner/redeem",
                content=b"x" * (TEAM_PUBLIC_AUTH_REQUEST_MAX_BYTES + 1),
                headers={"content-type": "application/json"},
            ).status_code
            == 413
        )
        assert (
            client.post("/api/owner/redeem", json={"code": code, "secret": secret}).status_code
            == 200
        )
        session = client.cookies.get(OWNER_SESSION_COOKIE)
        folder = tmp_path / "capture"
        folder.mkdir(mode=0o700)
        target = folder / "rcp.sqlite3"
        store.online_snapshot(target)
        snapshot = AppStore.open_read_only_snapshot(target)
        with snapshot.connection() as connection:
            for table in ("owner_credentials", "owner_sign_in_codes", "team_sessions"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert store.resolve_owner_session(session) is not None
        session, owner = store.create_owner_session(secret)
    assert owner == store.local_owner
    store.redeem_owner_sign_in_code(store.create_owner_sign_in_code(), secret=secret)
    assert store.resolve_owner_session(session) == owner


def test_owner_session_expiry_and_credential_replacement(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    code = store.create_owner_sign_in_code()
    session, _ = store.redeem_owner_sign_in_code(code)
    now = datetime.fromisoformat(store.now())
    from rcp.limits import TEAM_SESSION_IDLE_DAYS

    monkeypatch.setattr(
        store, "now", lambda: (now + timedelta(days=TEAM_SESSION_IDLE_DAYS + 1)).isoformat()
    )
    assert store.resolve_owner_session(session) is None
    code = store.create_owner_sign_in_code()
    session, _ = store.redeem_owner_sign_in_code(code)
    code = store.create_owner_sign_in_code()
    secret = secrets.token_urlsafe(32)
    store.redeem_owner_sign_in_code(code, secret=secret)
    assert store.resolve_owner_session(session) is None
    session, owner = store.create_owner_session(secret)
    assert owner == store.local_owner
    store.redeem_owner_sign_in_code(store.create_owner_sign_in_code(), secret=secret)
    assert store.resolve_owner_session(session) == owner


def test_owner_reads_preserve_session_until_mutation_and_enforce_expiry(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path)
    store = app.state.background_tasks.store
    client = signed_in_client(app)
    [before] = store.team_sessions(store.local_owner.user_id)
    now = datetime.fromisoformat(store.now()) + timedelta(hours=1)
    monkeypatch.setattr(store, "now", lambda: now.isoformat())
    assert client.get("/api/identity").status_code == 200
    assert store.team_sessions(store.local_owner.user_id) == [before]
    assert client.patch("/api/identity", json={"display_name": "Researcher"}).status_code == 200
    [after] = store.team_sessions(store.local_owner.user_id)
    assert after.last_seen_at > before.last_seen_at
    assert after.expires_at > before.expires_at
    now = datetime.fromisoformat(after.expires_at) + timedelta(seconds=1)
    assert client.get("/api/identity").status_code == 401
