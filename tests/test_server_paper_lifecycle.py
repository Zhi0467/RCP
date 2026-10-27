from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from rcp.agents import AgentLauncher
from rcp.projects import ProjectCatalog
from rcp.runs.project_deletion import stop_project_work
from rcp.server_ops.maintenance import (
    MaintenanceCoordinator,
    MaintenanceIdentity,
    RuntimeAdmissionGate,
)
from rcp.server_ops.paper_lifecycle_seam import register_paper_live, stop_paper_project
from rcp.storage import AppStore


class PaperOwner:
    def __init__(self):
        self.events = []
        self.fail_drain = False

    def pause(self, project_id):
        self.events.append(("pause", project_id))

    def drain(self, project_id):
        self.events.append(("drain", project_id))
        if self.fail_drain:
            raise RuntimeError("publisher still running")

    def dispose(self, project_id):
        self.events.append(("dispose", project_id))


def test_paper_is_drained_before_maintenance_capture(manifest, tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    owner = PaperOwner()
    register_paper_live(store, owner)
    background = SimpleNamespace(
        close_watcher_notifications=lambda: None, runtime_is_idle=lambda: True
    )
    coordinator = MaintenanceCoordinator(
        admission=RuntimeAdmissionGate(),
        background_admission=RuntimeAdmissionGate(),
        background=background,
        catalog=catalog,
        store=store,
        capture_sqlite=lambda: owner.events.append(("capture", record.project_id)),
    )
    identity = MaintenanceIdentity(str(uuid.uuid4()), "a" * 64)
    owner.fail_drain = True
    with pytest.raises(RuntimeError):
        coordinator.enter(identity, timeout=1)
    assert not coordinator.quiescent
    assert [event[0] for event in owner.events] == ["pause", "drain"]
    owner.events.clear()
    owner.fail_drain = False
    coordinator.enter(identity, timeout=1)
    assert coordinator.quiescent
    assert [event[0] for event in owner.events] == ["pause", "drain", "dispose", "capture"]


def test_delete_disposes_workers_and_removes_only_project_paper_rows(manifest, tmp_path):
    from rcp.server_ops.backup_paper import PAPER_PROJECT_TABLES

    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    with store.connection() as connection:
        for table in PAPER_PROJECT_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (project_id TEXT)')
            connection.executemany(
                f'INSERT INTO "{table}" VALUES (?)', [(record.project_id,), ("other",)]
            )
        connection.execute("CREATE TABLE paper_blobs (sha256 TEXT, bytes BLOB)")
        connection.execute("INSERT INTO paper_blobs VALUES ('shared', X'00')")
        connection.execute("CREATE TABLE paper_version_texts (sha256 TEXT, zbody BLOB)")
        connection.execute("INSERT INTO paper_version_texts VALUES ('shared', X'00')")
    owner = PaperOwner()
    register_paper_live(store, owner)
    background = SimpleNamespace(wait_for_stopped_workers=lambda *args, **kwargs: [])
    assert stop_project_work(store, background, record.project_id, None) == []
    assert [event[0] for event in owner.events] == ["pause", "drain", "dispose"]
    counts = store.delete_project_records(record.project_id)
    with store.connection() as connection:
        for table in PAPER_PROJECT_TABLES:
            assert counts[table] == 1
            assert connection.execute(f'SELECT project_id FROM "{table}"').fetchone()[0] == "other"
        for table in ("paper_blobs", "paper_version_texts"):
            assert connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 1


def test_restore_detaches_runtime_without_resetting_publication(manifest, tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    with store.connection() as connection:
        connection.execute(
            "CREATE TABLE paper_publication (project_id TEXT, saved_revision INTEGER, "
            "published_revision INTEGER, pending_since TEXT, last_error TEXT, hold TEXT)"
        )
        connection.execute(
            "INSERT INTO paper_publication VALUES (?, 8, 3, 'pending', 'offline', 'legacy')",
            (record.project_id,),
        )
        before = tuple(connection.execute("SELECT * FROM paper_publication").fetchone())
    owner = PaperOwner()
    register_paper_live(store, owner)
    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert [event[0] for event in owner.events] == ["pause", "drain", "dispose"]
    with store.connection() as connection:
        assert tuple(connection.execute("SELECT * FROM paper_publication").fetchone()) == before


def test_saved_paper_without_registered_owner_cannot_claim_quiescence(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    stop_paper_project(store, "legacy")
    with store.connection() as connection:
        connection.execute("CREATE TABLE paper_publication (project_id TEXT)")
        connection.execute("INSERT INTO paper_publication VALUES ('live')")
    with pytest.raises(RuntimeError):
        stop_paper_project(store, "live")
