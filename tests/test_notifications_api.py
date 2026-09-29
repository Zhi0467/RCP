from __future__ import annotations

import socket
import threading
from datetime import datetime, timedelta

import httpx
import uvicorn
from fastapi.testclient import TestClient

from rcp.api import create_app
from rcp.core.models import AuthorizedHuman
from rcp.limits import (
    NOTIFICATION_RETRY_MAX_SECONDS,
    NOTIFICATION_TTL_SECONDS,
    TEAM_SESSION_IDLE_DAYS,
)
from rcp.storage import AppStore, EpisodeRecord

from .helpers import create_named_app, wait_until


def test_personal_preferences_and_device_registration(manifest, tmp_path) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    client = TestClient(app)
    project_id = app.state.default_project_id
    preferences_url = f"/api/projects/{project_id}/notifications"
    preferences = client.get(preferences_url)
    assert preferences.status_code == 200
    assert preferences.json() == {
        "proposal": True,
        "decision": True,
        "blocker": True,
        "episode_needs_action": True,
        "episode_finished": False,
    }
    updated = client.patch(preferences_url, json={"proposal": False, "episode_finished": True})
    assert updated.status_code == 200
    assert updated.json()["proposal"] is False
    assert updated.json()["episode_finished"] is True
    assert client.get(preferences_url).json() == updated.json()
    assert client.patch(preferences_url, json={"unknown": True}).status_code == 422
    device = client.post("/api/notifications/devices/desktop", json={})
    assert device.status_code == 200
    assert set(device.json()) == {"device_id", "kind"}
    assert client.post("/api/notifications/devices/desktop", json={}).json() == device.json()
    assert (
        client.get(f"/api/notifications/devices/{device.json()['device_id']}/pending").json() == []
    )
    assert client.get("/api/projects/unknown/notifications").status_code == 404


def test_team_device_session_ownership_expiry_and_no_delivery_refresh(
    tmp_path, monkeypatch
) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lab")
    member, token = store.enroll_team_member(bootstrap, "Member")
    app = create_app(data_dir=tmp_path)
    clients = [TestClient(app, base_url="https://testserver") for _ in range(2)]
    for client in clients:
        assert client.post("/api/team/session/exchange", json={"token": token}).status_code == 200
    first, second = clients
    device = first.post("/api/notifications/devices/desktop", json={}).json()["device_id"]
    pending_url = f"/api/notifications/devices/{device}/pending"
    ack_url = f"/api/notifications/devices/{device}/items/absent"
    assert second.get(pending_url).status_code == 404
    assert second.post(ack_url, json={"status": "posted"}).status_code == 404
    session_id = store.notification_device(device)["session_id"]
    before = next(
        item for item in store.team_sessions(member.user_id) if item.session_id == session_id
    )
    later = datetime.fromisoformat(store.now()) + timedelta(hours=1)
    monkeypatch.setattr(AppStore, "now", staticmethod(lambda: later.isoformat()))
    pending = first.get(pending_url)
    assert pending.status_code == 200
    assert "set-cookie" not in pending.headers
    acknowledgment = first.post(ack_url, json={"status": "posted"})
    assert acknowledgment.status_code == 404
    assert "set-cookie" not in acknowledgment.headers
    after = next(
        item for item in store.team_sessions(member.user_id) if item.session_id == session_id
    )
    assert (after.last_seen_at, after.expires_at) == (before.last_seen_at, before.expires_at)
    later += timedelta(days=TEAM_SESSION_IDLE_DAYS)
    assert first.get(pending_url).status_code == 401
    assert store.notification_device(device) is None


def test_logout_and_revocation_detach_team_devices(tmp_path) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lab")
    member, token = store.enroll_team_member(bootstrap, "Member")
    app = create_app(data_dir=tmp_path)
    first, second = [TestClient(app, base_url="https://testserver") for _ in range(2)]
    for client in (first, second):
        client.post("/api/team/session/exchange", json={"token": token})
    devices = [
        client.post("/api/notifications/devices/desktop", json={}).json()["device_id"]
        for client in (first, second)
    ]
    second_session = store.notification_device(devices[1])["session_id"]
    assert first.post(f"/api/team/sessions/{second_session}/revoke", json={}).status_code == 200
    assert store.notification_device(devices[1]) is None
    assert store.notification_device(devices[0]) is not None
    assert first.post("/api/team/session/logout", json={}).status_code == 200
    assert store.notification_devices() == []


def test_desktop_retry_partial_success_ttl_resolution_and_toggle(
    manifest, tmp_path, monkeypatch
) -> None:
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "rcp.sqlite3", "Lab")
    member, token = store.enroll_team_member(bootstrap, "Member")
    app = create_app(str(manifest.path), data_dir=tmp_path)
    project_id = app.state.default_project_id
    clients = [TestClient(app, base_url="https://testserver") for _ in range(2)]
    devices = []
    for client in clients:
        client.post("/api/team/session/exchange", json={"token": token})
        devices.append(
            client.post("/api/notifications/devices/desktop", json={}).json()["device_id"]
        )
    now = datetime.fromisoformat(store.now())
    monkeypatch.setattr(AppStore, "now", staticmethod(lambda: now.isoformat()))
    store.set_notification_preferences(project_id, member.user_id, {"episode_finished": True})
    store.create_episode(
        EpisodeRecord(
            episode_id="finished",
            project_id=project_id,
            mode="experiment_loop",
            status="queued",
            control_node_id="exp/test",
            invocation_ceiling=1,
            authorized_by=AuthorizedHuman(
                space_id=store.space_id, user_id=member.user_id, display_name="Member"
            ),
            created_at=store.now(),
            updated_at=store.now(),
        )
    )
    store.end_episode_without_report("finished", ending="completed")
    with store.connection() as connection:
        for notification_id, item_id, age in (
            ("current", "finished", 0),
            ("expired", "finished", NOTIFICATION_TTL_SECONDS),
            ("resolved", "absent", 0),
        ):
            store.enqueue_notification(
                connection,
                notification_id=notification_id,
                project_id=project_id,
                target="main",
                kind="episode_finished",
                item_id=item_id,
                reason="episode_finished",
                observed_health="completed",
                observed_blocked_reason=None,
                project_name="Lab project",
                deep_link="#/projects/project/episodes/finished",
                created_at=(now - timedelta(seconds=age)).isoformat(),
            )
    pending_urls = [f"/api/notifications/devices/{device}/pending" for device in devices]
    payloads = [client.get(url).json() for client, url in zip(clients, pending_urls, strict=True)]
    assert [[item["notification_id"] for item in items] for items in payloads] == [
        ["current"],
        ["current"],
    ]
    assert set(payloads[0][0]) == {"notification_id", "reason", "project_name", "deep_link"}
    assert payloads[0][0]["reason"] == "episode_finished"
    assert all(item["notification_id"] == "current" for item in store.notification_outbox())
    ack_urls = [f"/api/notifications/devices/{device}/items/current" for device in devices]
    assert clients[0].post(ack_urls[0], json={"status": "posted"}).status_code == 200
    # A native send accepted before a process crash leaves its row unacknowledged.
    now += timedelta(seconds=NOTIFICATION_RETRY_MAX_SECONDS + 1)
    assert clients[0].get(pending_urls[0]).json() == []
    assert clients[1].get(pending_urls[1]).json() == payloads[1]
    rows = {row["device_id"]: row for row in store.notification_outbox()}
    assert (rows[devices[0]]["attempts"], rows[devices[0]]["last_status"]) == (1, "posted")
    assert rows[devices[1]]["attempts"] == 2
    assert clients[1].post(ack_urls[1], json={"status": "failed"}).status_code == 200
    clients[1].patch(f"/api/projects/{project_id}/notifications", json={"episode_finished": False})
    now += timedelta(seconds=NOTIFICATION_RETRY_MAX_SECONDS + 1)
    assert clients[1].get(pending_urls[1]).json() == []
    assert [row["device_id"] for row in store.notification_outbox()] == [devices[0]]


def test_served_desktop_register_attention_pull_and_ack(manifest, tmp_path, caplog) -> None:
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    store = app.state.services.store
    project_id = app.state.default_project_id
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert port != 8421
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=True))
        worker = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        worker.start()
        try:
            wait_until(lambda: server.started)
            wait_until(lambda: app.state.startup_effect_runtime_event.is_set())
            wait_until(lambda: store.notification_graph_marker(project_id))
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                registered = client.post("/api/notifications/devices/desktop", json={})
                assert registered.status_code == 200
                device_id = registered.json()["device_id"]
                committed = client.post(
                    f"/api/projects/{project_id}/sync",
                    json={
                        "base_revision": app.state.catalog.open(project_id)
                        .history.state()
                        .revision,
                        "custom_nodes": [
                            {
                                "id": "blk/live",
                                "type": "blocker",
                                "title": "private title",
                                "description": "private detail",
                                "status": "open",
                            }
                        ],
                    },
                )
                assert committed.status_code == 200, committed.text
                rows = wait_until(lambda: store.notification_outbox(device_id))
                assert [row["item_id"] for row in rows] == ["blk/live"]
                pending_url = f"/api/notifications/devices/{device_id}/pending"
                pending = client.get(pending_url)
                assert pending.status_code == 200
                items = pending.json()
                assert [item["notification_id"] for item in items] == [rows[0]["notification_id"]]
                assert items[0]["reason"] == "blocker"
                assert items[0]["deep_link"].startswith("#/")
                ack_url = (
                    f"/api/notifications/devices/{device_id}/items/{items[0]['notification_id']}"
                )
                assert client.post(ack_url, json={"status": "posted"}).status_code == 200
                assert client.get(pending_url).json() == []
                assert store.notification_outbox(device_id)[0]["last_status"] == "posted"
        finally:
            server.should_exit = True
            worker.join(timeout=10)
        assert not worker.is_alive()
    assert not [record for record in caplog.records if record.levelno >= 40]
