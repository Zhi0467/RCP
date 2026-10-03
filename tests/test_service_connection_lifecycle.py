from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.api import service_connections as routes
from rcp.service_connections import ConnectionError, ServiceConnections, VoiceSettings
from rcp.storage import AppStore

from .test_server_member_removal import _team
from .test_service_connections import KEY, MIME, connection


def test_removal_fences_late_member_writes_and_resumes_cleanup(tmp_path, monkeypatch):
    store, _, bob, _, _, coordinator = _team(tmp_path)
    private = ServiceConnections(store, bob.user_id)
    item = connection()
    private.save(item, KEY)
    private.select(item["id"])
    previous = private.credentials(item["id"])[0]
    preview = coordinator.plan(bob.user_id).snapshot
    original = ServiceConnections.remove_member_data
    attempts = 0

    def interrupted(self):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("interrupted cleanup")
        original(self)

    monkeypatch.setattr(ServiceConnections, "remove_member_data", interrupted)
    with pytest.raises(OSError):
        coordinator.advance(bob.user_id, boundary_sha256=preview.boundary_sha256)
    assert store.space_user(bob.user_id).removal_started_at is not None
    for write in (
        lambda: private.save(connection(), KEY),
        lambda: private.select(item["id"]),
        lambda: private.voice_settings(VoiceSettings()),
        lambda: private.update_purposes(previous, {**previous, "purposes": ["voice"]}),
    ):
        with pytest.raises(ConnectionError) as error:
            write()
        assert error.value.status == 403
    coordinator.reconcile_pending()
    assert not private.root.exists()
    coordinator.advance(bob.user_id, boundary_sha256=preview.boundary_sha256)
    assert not private.root.exists() and store.space_user(bob.user_id).removed_at is not None


def test_removal_serializes_with_a_member_write(tmp_path, monkeypatch):
    store, _, bob, _, _, coordinator = _team(tmp_path)
    private = ServiceConnections(store, bob.user_id)
    writing, release = threading.Event(), threading.Event()
    original = private._mkdir

    def paused(path):
        writing.set()
        assert release.wait(5)
        original(path)

    monkeypatch.setattr(private, "_mkdir", paused)
    preview = coordinator.plan(bob.user_id).snapshot
    with ThreadPoolExecutor(2) as pool:
        write = pool.submit(private.save, connection(), KEY)
        assert writing.wait(5)
        remove = pool.submit(
            coordinator.advance, bob.user_id, boundary_sha256=preview.boundary_sha256
        )
        # Removal waits for the member write that holds the lock.
        with pytest.raises(TimeoutError):
            remove.result(timeout=0.2)
        release.set()
        write.result()
        remove.result()
    assert not private.root.exists()


def test_team_audio_allowlist_retains_origin_and_member_isolation(tmp_path, monkeypatch):
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Team")
    alice, token = store.enroll_team_member(bootstrap, "Alice")
    _, code = store.create_team_invitation(alice.user_id)
    _, bob_token = store.enroll_team_member(code, "Bob")
    private = ServiceConnections(store, alice.user_id)
    item = connection()
    private.save(item, KEY)

    async def transcribe(*_):
        return "hello"

    monkeypatch.setattr(routes, "transcribe", transcribe)
    app = create_app(data_dir=tmp_path)
    with TestClient(app, base_url="https://team.test") as client:
        assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
        path = f"/api/service-connections/{item['id']}/transcribe"
        headers = {"content-type": MIME, "origin": "https://team.test"}
        assert client.post(path, content=b"audio", headers=headers).json() == {"text": "hello"}
        assert (
            client.post("/api/service-connections", content=b"audio", headers=headers).status_code
            == 415
        )
        assert (
            client.post(
                path, content=b"audio", headers={**headers, "origin": "https://other.test"}
            ).status_code
            == 403
        )
        assert (
            client.post("/api/team/session/exchange", json={"token": bob_token}).status_code == 200
        )
        assert client.get("/api/service-connections").json()["connections"] == []
        assert client.post(path, content=b"audio", headers=headers).status_code == 404
        assert (
            client.request("DELETE", f"/api/service-connections/{item['id']}", json={}).status_code
            == 404
        )
