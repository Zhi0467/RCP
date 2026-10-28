from __future__ import annotations

import gzip
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rcp.server_ops.backup_capture import _database_schema_sha256
from rcp.server_ops.restore import SUPPORTED_RESTORE_DATABASE_SCHEMAS
from rcp.server_ops.restore import _database_schema_sha256 as raw_schema_digest
from rcp.storage import AppStore

from .test_storage import _project


def _notice(project_id="p", notification_id="n"):
    return dict(
        notification_id=notification_id,
        project_id=project_id,
        target="main",
        kind="proposal",
        item_id="item",
        reason="proposal",
        project_name="Project",
        deep_link="#/projects/p/proposals/item",
    )


def _setup(tmp_path):
    store = AppStore(tmp_path / "db")
    store.upsert_project(_project("p"))
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO project_members VALUES (?,?,?,NULL)",
            ("p", store.local_owner.user_id, store.now()),
        )
    device = store.register_notification_device(store.local_owner.user_id)
    return store, device


def test_notification_migration_boundary_and_restore_detachment(tmp_path):
    path = tmp_path / "db"
    path.write_bytes(
        gzip.decompress(
            Path("tests/fixtures/restore_schema/pre-notifications-v27.sqlite3.gz").read_bytes()
        )
    )
    assert (
        raw_schema_digest(path)
        == "0f0456bec7bb95895d5d7287684b03aa4b97c605fa36ef8fec17459948fbb967"
    )
    assert raw_schema_digest(path) in SUPPORTED_RESTORE_DATABASE_SCHEMAS
    store = AppStore(path)
    assert store.storage_schema_ledger_head() == store.storage_schema_registry_head()
    assert _database_schema_sha256(store) in SUPPORTED_RESTORE_DATABASE_SCHEMAS
    assert store.notification_project_baseline("p") is None
    store, _device = _setup(tmp_path / "fresh")
    with store.connection() as connection:
        store.enqueue_notification(connection, **_notice())
    store.baseline_notification_project("p", [])
    assert len(store.notification_outbox()) == 1
    store.detach_restored_lifecycle(diagnostic="Restore", confirmed_by="Operator")
    assert store.notification_devices() == store.notification_outbox() == []
    assert store.notification_project_baseline("p") is None


def test_outbox_marker_atomicity_and_empty_baseline(tmp_path, monkeypatch):
    store, device = _setup(tmp_path)
    store.consume_notification_graph_boundary("p", "main", 0, None, {}, [_notice()])
    assert store.notification_outbox() == []
    enqueue = store.enqueue_notification

    def interrupt_after_enqueue(connection, **notification):
        enqueue(connection, **notification)
        raise RuntimeError()

    monkeypatch.setattr(store, "enqueue_notification", interrupt_after_enqueue)
    with pytest.raises(RuntimeError):
        store.consume_notification_graph_boundary(
            "p", "main", 1, "t1", {"proposal": ["item"]}, [_notice()]
        )
    assert store.notification_graph_marker("p")["revision"] == 0
    assert store.notification_outbox() == []
    monkeypatch.setattr(store, "enqueue_notification", enqueue)
    store.consume_notification_graph_boundary(
        "p", "main", 1, "t1", {"proposal": ["item"]}, [_notice()]
    )
    assert [row["notification_id"] for row in store.notification_outbox()] == ["n"]
    store.consume_notification_graph_boundary(
        "p", "main", 1, "t1", {}, [_notice(notification_id="replayed")]
    )
    assert len(store.notification_outbox()) == 1


def test_crash_retry_partial_success_and_toggle_off(tmp_path):
    store, bootstrap = AppStore.initialize_team_space(tmp_path / "db", "Team")
    member, token = store.enroll_team_member(bootstrap, "Member")
    store.upsert_project(_project("p"))
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO project_members VALUES (?,?,?,NULL)", ("p", member.user_id, store.now())
        )
    sessions = [store.create_team_session(token)[0] for _ in range(2)]
    devices = [
        store.register_notification_device(member.user_id, session.session_id)
        for session in store.team_sessions(member.user_id)
    ]
    with store.connection() as connection:
        store.enqueue_notification(connection, **_notice())
    assert len(store.notification_outbox()) == 2
    first, second = [device["device_id"] for device in devices]
    later = (datetime.fromisoformat(store.now()) + timedelta(seconds=10)).isoformat()
    assert store.begin_notification_attempt(first, "n", later)
    # A leased row is not due again, so a concurrent pull cannot lease it twice.
    assert not store.begin_notification_attempt(first, "n", later)
    reopened = AppStore(store.path)
    assert reopened.notification_outbox(first)[0]["last_status"] == "pending"
    assert reopened.notification_outbox(first)[0]["attempts"] == 1
    assert reopened.acknowledge_notification(first, "n", posted=True)
    assert reopened.acknowledge_notification(first, "n", posted=True)
    assert reopened.notification_outbox(second)[0]["last_status"] == "pending"
    assert not reopened.acknowledge_notification(first, "n", posted=False)
    reopened.set_notification_preferences("p", member.user_id, {"proposal": False})
    assert not reopened.guard_notification_delivery(second, "n")
    assert reopened.notification_outbox(second) == []
    # Expiry is checked without a queued item, and polling never extends it.
    with store.connection() as connection:
        old = connection.execute(
            "SELECT expires_at FROM team_sessions WHERE session_id=?", (devices[0]["session_id"],)
        ).fetchone()[0]
    assert store.resolve_team_session(sessions[0], touch=False)
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT expires_at FROM team_sessions WHERE session_id=?",
                (devices[0]["session_id"],),
            ).fetchone()[0]
            == old
        )
        connection.execute("UPDATE team_sessions SET expires_at=?", (store.now(),))
    store.prune_notification_devices()
    assert store.notification_devices() == []


@pytest.mark.parametrize("registered", [True, False])
def test_notification_state_follows_project_identity_adoption(tmp_path, registered):
    store, device = _setup(tmp_path)
    owner = store.local_owner.user_id
    project_id = str(uuid.uuid4())
    store.set_notification_preferences("p", owner, {"episode_finished": True})
    store.baseline_notification_project("p", [("episode", "completed", None)])
    store.consume_notification_graph_boundary("p", "main", 0, None, {}, [])
    with store.connection() as connection:
        store.enqueue_notification(connection, **_notice())
    if registered:
        store.migrate_project_identity("p", project_id, store.space_id)
    else:
        with store.connection() as connection:
            connection.execute("DELETE FROM projects WHERE project_id='p'")
        store.upsert_project(
            _project(project_id).model_copy(update={"home_space_id": store.space_id})
        )
        store.migrate_legacy_project_data("p", project_id)
    assert store.notification_project_baseline("p") is None
    assert store.notification_project_baseline(project_id) is not None
    assert store.notification_graph_marker("p") is None
    assert store.notification_graph_marker(project_id)["revision"] == 0
    assert store.notification_episode_observations("p") == {}
    assert set(store.notification_episode_observations(project_id)) == {"episode"}
    assert store.notification_preferences(project_id, owner)["episode_finished"] is True
    assert store.notification_device(device["device_id"]) is not None
    rows = store.notification_outbox()
    assert [(row["notification_id"], row["project_id"], row["deep_link"]) for row in rows] == [
        ("n", project_id, f"#/projects/{project_id}/proposals/item")
    ]


def test_a_project_work_fence_does_not_block_opting_out(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path)

    def fenced(*_args):
        raise ValueError("project is fenced")

    monkeypatch.setattr(store, "_require_project_accepts_new_work", fenced)
    owner = store.local_owner.user_id
    assert store.set_notification_preferences("p", owner, {"proposal": False})["proposal"] is False
