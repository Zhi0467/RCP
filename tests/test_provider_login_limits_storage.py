from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rcp.api import provider_login
from rcp.api.dependencies import get_store
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.storage import AppStore


def test_reset_time_expires_login(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "state.sqlite3")
    monkeypatch.setattr(store, "now", lambda: "2026-01-01T03:00:00+00:00")
    key = LoginKey("codex", BACKEND_MACHINE_ID, "")
    assert store.set_provider_login_limit(
        key,
        provider_started_at="2026-01-01T02:00:00+00:00",
        limited=True,
        resets_at="2026-01-01T03:05:00+00:00",
    )
    assert store.provider_login_limit_lapses_in(key) == 300
    assert store.provider_login_limited(key)
    assert not store.provider_login_limited(key, now="2026-01-01T03:05:00+00:00")


@pytest.mark.parametrize("clear", ["success", "sign_in"])
def test_clear_limit_clears_reset_time(tmp_path, monkeypatch, clear):
    store = AppStore(tmp_path / "state.sqlite3")
    monkeypatch.setattr(store, "now", lambda: "2026-01-01T03:00:00+00:00")
    key = LoginKey("codex", BACKEND_MACHINE_ID, "")
    store.set_provider_login_limit(
        key,
        provider_started_at="2026-01-01T02:00:00+00:00",
        limited=True,
        resets_at="2026-01-01T04:00:00+00:00",
    )
    if clear == "success":
        store.set_provider_login_limit(
            key, provider_started_at="2026-01-01T03:01:00+00:00", limited=False
        )
    else:
        store.mark_provider_login_verified(key, member_id="member", detail="ok", signed_in=True)
    assignment = store.provider_login_assignment(key)
    assert assignment.limited_at is None
    assert assignment.limit_resets_at is None


def test_old_limit_and_member_tables_upgrade_without_losing_rows(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = AppStore(path)
    owner = store.local_owner
    assert owner is not None
    key = LoginKey("codex", BACKEND_MACHINE_ID, "")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE provider_login_assignments DROP COLUMN limit_resets_at")
        connection.execute("ALTER TABLE space_users DROP COLUMN auto_resume_after_limits")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=98")
        connection.execute(
            "INSERT INTO provider_login_assignments(provider,machine_id,login_id,limited_at) VALUES(?,?,?,?)",
            (*key, "2026-01-01T03:00:00+00:00"),
        )
    reopened = AppStore(path)
    assert reopened.local_owner is not None
    assert reopened.local_owner.user_id == owner.user_id
    assert reopened.local_owner.auto_resume_after_limits is True
    assignment = reopened.provider_login_assignment(key)
    assert assignment.limited_at == "2026-01-01T03:00:00+00:00"
    assert assignment.limit_resets_at is None
    assert reopened.provider_login_limit_lapses_in(key, now="2026-01-01T04:00:00+00:00") == 14400


def test_member_limit_resume_api_persists_personal_owner_preference(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    store = AppStore(path)
    owner = store.local_owner
    assert owner is not None
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: owner),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as client:
        assert client.get("/api/providers/limit-resume").json() == {"enabled": True}
        response = client.put("/api/providers/limit-resume", json={"enabled": False})
        assert response.status_code == 200
        assert response.json() == {"enabled": False}
        assert client.get("/api/providers/limit-resume").json() == {"enabled": False}
        assert (
            client.put("/api/providers/limit-resume", json={"enabled": "false"}).status_code == 422
        )
    reopened = AppStore(path).space_user(owner.user_id)
    assert reopened is not None and reopened.auto_resume_after_limits is False


def test_member_limit_resume_api_changes_only_acting_member(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "state.sqlite3", space_kind="team")
    member = store.preprovision_team_member("First")
    other = store.preprovision_team_member("Second")
    monkeypatch.setattr(
        provider_login,
        "get_identity_access",
        lambda _: SimpleNamespace(acting_user=lambda _: member),
    )
    app = FastAPI()
    app.include_router(provider_login.router)
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app) as client:
        response = client.put("/api/providers/limit-resume", json={"enabled": False})
        assert response.status_code == 200
        assert client.get("/api/providers/limit-resume").json() == {"enabled": False}
    changed = store.space_user(member.user_id)
    unchanged = store.space_user(other.user_id)
    assert changed is not None and not changed.auto_resume_after_limits
    assert unchanged is not None and unchanged.auto_resume_after_limits


def test_login_row_exposes_only_active_reset_time(tmp_path, monkeypatch):
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.provider_login_rows import provider_login_row

    store = AppStore(tmp_path / "state.sqlite3")
    monkeypatch.setattr(store, "now", lambda: "2026-01-01T03:00:00+00:00")
    key = LoginKey("codex", BACKEND_MACHINE_ID, "")
    credentials = ProviderCredentialStore(tmp_path / "credentials")
    store.set_provider_login_limit(
        key,
        provider_started_at="2026-01-01T02:00:00+00:00",
        limited=True,
        resets_at="2026-01-01T04:00:00+00:00",
    )
    row = provider_login_row(store, credentials, None, key, viewer_id=None)
    assert row["limit_resets_at"] == "2026-01-01T04:00:00+00:00"
    monkeypatch.setattr(store, "now", lambda: "2026-01-01T04:00:00+00:00")
    row = provider_login_row(store, credentials, None, key, viewer_id=None)
    assert row["limit_resets_at"] is None
    assert row["limited_at"] is None
