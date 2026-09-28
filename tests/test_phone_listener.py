from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rcp.limits import TEAM_CODE_FAILED_ATTEMPT_LIMIT
from rcp.phone_listener import PhoneListener, create_phone_listener_app
from rcp.storage import AppStore

from .helpers import create_named_app
from .test_web_push import _PUBLIC_ADDRESS, _receiver

_ORIGIN = {"Origin": "https://mac.example.ts.net"}


def _registration(code):
    _, p256dh, auth = _receiver()
    return {
        "code": code,
        "endpoint": "https://web.push.apple.com/phone",
        "keys": {"p256dh": p256dh, "auth": auth},
    }


def _listener(store):
    return TestClient(
        create_phone_listener_app(store, lambda _host: [_PUBLIC_ADDRESS]),
        base_url="https://mac.example.ts.net",
    )


def test_listener_serves_only_its_allowlist(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = _listener(app.state.background_tasks.store)
    for path in ("/", "/sw.js", "/manifest.webmanifest", "/icon-180.png", "/api/key"):
        assert client.get(path).status_code == 200, path
    for path in (
        "/index.html",
        "/api/health",
        f"/api/projects/{app.state.default_project_id}/notifications",
        "/api/notifications/devices/desktop",
        "/docs",
        "/openapi.json",
        "/assets/../index.html",
    ):
        assert client.get(path).status_code == 404, path
    assert client.post("/api/notifications/phone-pairings", json={}).status_code in (404, 405)


def test_redeeming_a_code_registers_a_notify_only_phone(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    owner = TestClient(app)
    code = owner.post("/api/notifications/phone-pairings", json={}).json()["code"]
    client = _listener(store)
    response = client.post("/api/register", json=_registration(code), headers=_ORIGIN)
    assert response.status_code == 200
    # The response carries no session, token, or cookie: no read access.
    assert response.json() == {"ok": True}
    assert "set-cookie" not in response.headers
    [device] = store.notification_devices()
    assert device["kind"] == "web_push" and device["session_id"] is None
    assert store.web_push_subscription(device["device_id"])["opens_links"] == 0
    again = client.post("/api/register", json=_registration(code), headers=_ORIGIN)
    assert again.json()["detail"]["code"] == "code_consumed"
    assert not store.notification_phone_pairing_live()


def test_wrong_codes_lock_and_refused_endpoints_consume_nothing(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    client = _listener(store)
    store.notification_vapid_key()
    code, _ = store.create_notification_phone_pairing()
    refused = {**_registration(code), "endpoint": "https://push.attacker.example/x"}
    assert client.post("/api/register", json=refused, headers=_ORIGIN).status_code == 422
    assert store.notification_phone_pairing_live()
    wrong = code[:5] + ("A" if code[5] != "A" else "B") + code[6:]
    codes = [
        client.post("/api/register", json=_registration(wrong), headers=_ORIGIN).json()["detail"][
            "code"
        ]
        for _ in range(TEAM_CODE_FAILED_ATTEMPT_LIMIT)
    ]
    assert codes[-1] == "code_locked" and set(codes[:-1]) == {"code_invalid"}
    locked = client.post("/api/register", json=_registration(code), headers=_ORIGIN)
    assert locked.json()["detail"]["code"] == "code_locked"
    assert store.notification_devices() == []


def test_team_space_does_not_pair_notify_only_phones(tmp_path) -> None:
    store, _bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lab")
    with pytest.raises(ValueError):
        store.create_notification_phone_pairing()


def test_listener_resumes_for_a_live_code_and_stops_without_one(
    manifest, tmp_path, monkeypatch
) -> None:
    from .helpers import wait_until

    monkeypatch.setattr("rcp.phone_listener.PHONE_LISTENER_POLL_SECONDS", 0.05)
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.background_tasks.store
    store.notification_vapid_key()
    store.create_notification_phone_pairing()
    listener = PhoneListener(store, lambda _host: [_PUBLIC_ADDRESS], port=0)
    # A restart finds the persisted code and serves again.
    listener.resume()
    assert listener.is_running()
    store.create_notification_phone_pairing()
    with store.connection() as connection:
        connection.execute("UPDATE notification_phone_pairings SET revoked_at=created_at")
    wait_until(lambda: not listener.is_running())
    listener.resume()
    assert not listener.is_running()
