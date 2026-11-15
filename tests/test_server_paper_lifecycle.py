from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from rcp import limits
from rcp.agents import AgentLauncher
from rcp.paper import PaperConflict, PaperLive, PaperOp
from rcp.paper.folder import LocalPaperFolder
from rcp.paper.folder_io import paper_root
from rcp.projects import ProjectCatalog
from rcp.runs.project_deletion import stop_project_work
from rcp.server_ops import paper_lifecycle
from rcp.server_ops.maintenance import (
    MaintenanceCoordinator,
    MaintenanceIdentity,
    MaintenanceRefused,
    RuntimeAdmissionGate,
)
from rcp.server_ops.paper_lifecycle import register_paper_live, stop_paper_project
from rcp.storage import AppStore


class PaperOwner:
    def __init__(self):
        self.events = []
        self.fail_dispose = False

    def pause(self, project_id):
        self.events.append(("pause", project_id))

    def resume(self, project_id):
        self.events.append(("resume", project_id))

    def dispose(self, project_id):
        self.events.append(("dispose", project_id))
        if self.fail_dispose:
            raise RuntimeError("publisher still running")


@pytest.mark.parametrize("failure", ["dispose", "timeout"])
def test_paper_is_drained_before_maintenance_capture(manifest, tmp_path, monkeypatch, failure):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    owner = PaperOwner()
    monkeypatch.setitem(paper_lifecycle._owners, store, owner)
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
    owner.fail_dispose = failure == "dispose"
    if failure == "timeout":
        import time

        clock = time.monotonic
        monkeypatch.setattr(
            "rcp.server_ops.maintenance.time.monotonic",
            lambda: clock() + (10 if any(e[0] == "dispose" for e in owner.events) else 0),
        )
    with pytest.raises(RuntimeError if failure == "dispose" else MaintenanceRefused):
        coordinator.enter(identity, timeout=1)
    assert not coordinator.quiescent
    expected = ["pause", "dispose", "resume"]
    assert [event[0] for event in owner.events] == expected
    if failure == "timeout":
        monkeypatch.undo()
        monkeypatch.setitem(paper_lifecycle._owners, store, owner)
    owner.events.clear()
    owner.fail_dispose = False
    coordinator.enter(identity, timeout=1)
    assert coordinator.quiescent
    assert [event[0] for event in owner.events] == ["pause", "dispose", "capture"]


def test_real_owner_disposes_reopens_and_deletes_only_project_rows(manifest, tmp_path, monkeypatch):
    from rcp.server_ops.backup_paper import PAPER_PROJECT_TABLES

    monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)
    monkeypatch.setattr(limits, "PAPER_POLL_SECONDS", 0.01)

    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    live = PaperLive(store, lambda project: LocalPaperFolder(paper_root(tmp_path, project)))
    background = SimpleNamespace(wait_for_stopped_workers=lambda *args, **kwargs: [])

    async def scenario():
        await live.start()
        register_paper_live(store, live)
        for project in (record.project_id, "other"):
            receipt = await live.apply(
                project, [PaperOp(kind="write", path="a.md", data=b"saved")], "human"
            )
            await live.create_version(project, "named", "human")
            with store.connection() as connection:
                connection.execute(
                    "INSERT INTO paper_change_groups VALUES (?, 'group', 'task', ?, 'review', 'accepted', 'human', ?, ?)",
                    (project, receipt.revision, store.now(), store.now()),
                )
                connection.execute(
                    "INSERT INTO paper_changes VALUES (?, 'change', 'group', '{}')", (project,)
                )
                connection.execute(
                    "INSERT INTO paper_change_receipts VALUES (?, 'accept', 'group', 'accept', '{}', ?)",
                    (project, store.now()),
                )
        await live.apply(record.project_id, [PaperOp(kind="write", path="b.md", data=b"")], "human")
        await asyncio.to_thread(stop_paper_project, store, record.project_id)
        assert not any(key[0] == record.project_id for key in live.rooms)
        (paper_root(tmp_path, record.project_id) / "c.md").write_text("while stopped")
        await live.tick()
        await live.drain(record.project_id)
        assert record.project_id not in live.projects
        await live.resume(record.project_id)
        resumed = await live.tree(record.project_id)
        assert {file.path for file in resumed.files} == {"a.md", "b.md", "c.md"}
        assert (
            await asyncio.to_thread(stop_project_work, store, background, record.project_id, None)
            == []
        )
        store.delete_project_records(record.project_id)
        with store.connection() as connection:
            for table in ("paper_state", "paper_turn_files", *PAPER_PROJECT_TABLES):
                assert not connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE project_id=?', (record.project_id,)
                ).fetchall()
            assert connection.execute(
                "SELECT count(*) FROM paper_versions WHERE project_id='other'"
            ).fetchone()[0]
            assert connection.execute("SELECT count(*) FROM paper_version_texts").fetchone()[0]
            for table in ("paper_change_groups", "paper_changes", "paper_change_receipts"):
                assert (
                    connection.execute(
                        f'SELECT count(*) FROM "{table}" WHERE project_id=? AND group_id=?',
                        ("other", "group"),
                    ).fetchone()[0]
                    == 1
                )
        await live.close()
        paper_lifecycle.unregister_paper_live(store, live)

    asyncio.run(scenario())
    store.close()


def test_saved_paper_without_registered_owner_cannot_claim_quiescence(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    stop_paper_project(store, "legacy")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_files(project_id, file_id, path, kind, created_at) VALUES ('live', 'f', 'a.md', 'text', 'now')"
        )
    with pytest.raises(RuntimeError):
        stop_paper_project(store, "live")


@pytest.mark.parametrize("saved", [False, True])
def test_released_project_stays_inactive_after_restart(tmp_path, monkeypatch, saved):
    from tests.test_transfer_source import _released_source

    _, app, store, _, transfer, _, _ = _released_source(tmp_path)
    live = app.state.paper_live
    monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)
    monkeypatch.setattr(limits, "PAPER_POLL_SECONDS", 0.01)

    async def scenario():
        if saved:
            await live.apply(
                transfer.project_id, [PaperOp(kind="write", path="held.md", data=b"")], "human"
            )
        await live.close()
        with store.connection() as connection:
            before = connection.execute(
                "SELECT revision FROM paper_state WHERE project_id=?", (transfer.project_id,)
            ).fetchall()
        restarted = PaperLive(store, live.folder_for)
        await restarted.start()
        with pytest.raises(PaperConflict):
            await restarted.tree(transfer.project_id)
        await restarted.tick()
        await restarted.drain(transfer.project_id)
        with store.connection() as connection:
            assert (
                connection.execute(
                    "SELECT revision FROM paper_state WHERE project_id=?", (transfer.project_id,)
                ).fetchall()
                == before
            )
        assert bool(before) == saved
        await restarted.close()

    asyncio.run(scenario())


def test_deletion_conflict_resumes_paper(manifest, tmp_path, monkeypatch):
    from rcp.storage import ProjectActiveTaskConflict

    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    owner = PaperOwner()
    monkeypatch.setitem(paper_lifecycle._owners, store, owner)
    background = SimpleNamespace(wait_for_stopped_workers=lambda *args, **kwargs: ["busy"])
    with pytest.raises(ProjectActiveTaskConflict):
        stop_project_work(store, background, record.project_id, None)
    assert owner.events == [("pause", record.project_id), ("resume", record.project_id)]


def test_maintenance_release_resumes_disposed_paper(manifest, tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    owner = PaperOwner()
    monkeypatch.setitem(paper_lifecycle._owners, store, owner)
    coordinator = MaintenanceCoordinator(
        admission=RuntimeAdmissionGate(),
        background_admission=RuntimeAdmissionGate(),
        background=SimpleNamespace(
            close_watcher_notifications=lambda: None,
            accept_watcher_notifications=lambda: None,
            runtime_is_idle=lambda: True,
        ),
        catalog=catalog,
        store=store,
        capture_sqlite=lambda: None,
    )
    identity = MaintenanceIdentity(str(uuid.uuid4()), "a" * 64)
    coordinator.enter(identity, timeout=1)
    coordinator.release(identity, timeout=1)
    assert owner.events == [
        (action, record.project_id) for action in ("pause", "dispose", "resume")
    ]


def test_release_reopens_admission_for_a_never_opened_paper(manifest, tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    live = PaperLive(store, lambda project: LocalPaperFolder(paper_root(tmp_path, project)))
    coordinator = MaintenanceCoordinator(
        admission=RuntimeAdmissionGate(),
        background_admission=RuntimeAdmissionGate(),
        background=SimpleNamespace(
            close_watcher_notifications=lambda: None,
            accept_watcher_notifications=lambda: None,
            runtime_is_idle=lambda: True,
        ),
        catalog=catalog,
        store=store,
        capture_sqlite=lambda: None,
    )
    identity = MaintenanceIdentity(str(uuid.uuid4()), "a" * 64)

    async def scenario():
        await live.start()
        register_paper_live(store, live)
        await asyncio.to_thread(coordinator.enter, identity, timeout=5)
        await asyncio.to_thread(coordinator.release, identity, timeout=5)
        assert not coordinator.admission.closed
        assert (await live.tree(record.project_id)).revision == 0
        await live.close()
        paper_lifecycle.unregister_paper_live(store, live)

    asyncio.run(scenario())
    store.close()


def test_deletion_failing_after_stop_resumes_kept_paper(manifest, tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    owner = PaperOwner()
    monkeypatch.setitem(paper_lifecycle._owners, store, owner)

    def fail(project_id, *, memory_host_key=None):
        raise RuntimeError("records stayed")

    monkeypatch.setattr(store, "delete_project_records", fail)
    with pytest.raises(RuntimeError):
        catalog.delete(record.project_id, stop_work=lambda project_id: [])
    assert owner.events == [("resume", record.project_id)]
    assert store.project(record.project_id) is not None
