from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from rcp import limits
from rcp.agents import AgentLauncher
from rcp.paper import PaperConflict, PaperLive, PaperOp
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
from rcp.transport import LocalStateWorkspace


class PaperOwner:
    def __init__(self):
        self.events = []
        self.fail_drain = False

    def pause(self, project_id):
        self.events.append(("pause", project_id))

    def resume(self, project_id):
        self.events.append(("resume", project_id))

    def drain(self, project_id):
        self.events.append(("drain", project_id))
        if self.fail_drain:
            raise RuntimeError("publisher still running")

    def dispose(self, project_id):
        self.events.append(("dispose", project_id))


@pytest.mark.parametrize("failure", ["drain", "timeout"])
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
    owner.fail_drain = failure == "drain"
    if failure == "timeout":
        import time

        clock = time.monotonic
        monkeypatch.setattr(
            "rcp.server_ops.maintenance.time.monotonic",
            lambda: clock() + (10 if any(e[0] == "dispose" for e in owner.events) else 0),
        )
    with pytest.raises(RuntimeError if failure == "drain" else MaintenanceRefused):
        coordinator.enter(identity, timeout=1)
    assert not coordinator.quiescent
    expected = ["pause", "drain"] + (["dispose"] if failure == "timeout" else []) + ["resume"]
    assert [event[0] for event in owner.events] == expected
    if failure == "timeout":
        monkeypatch.undo()
        monkeypatch.setitem(paper_lifecycle._owners, store, owner)
    owner.events.clear()
    owner.fail_drain = False
    coordinator.enter(identity, timeout=1)
    assert coordinator.quiescent
    assert [event[0] for event in owner.events] == ["pause", "drain", "dispose", "capture"]


def test_real_owner_disposes_reopens_and_deletes_only_project_rows(manifest, tmp_path, monkeypatch):
    from rcp.server_ops.backup_paper import PAPER_PROJECT_TABLES

    monkeypatch.setattr(limits, "PAPER_PUBLISH_IDLE_SECONDS", 0)
    monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)

    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    root = tmp_path / "paper-state"
    root.mkdir()
    live = PaperLive(store, lambda _: LocalStateWorkspace(root, str(root)))
    background = SimpleNamespace(wait_for_stopped_workers=lambda *args, **kwargs: [])

    async def scenario():
        await live.start()
        register_paper_live(store, live)
        for project in (record.project_id, "other"):
            receipt = await live.apply(
                project,
                [PaperOp(kind="create", path="a.md", text="saved")],
                "human",
                {"receipt_key": "create"},
            )
            await live.create_version(project, "named", "human")
            await asyncio.to_thread(
                live._compact_bytes, project, receipt.file_ids[0], 1, receipt.revision
            )
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
        await live.apply(record.project_id, [PaperOp(kind="create", path="b.md")], "human")
        await asyncio.to_thread(stop_paper_project, store, record.project_id)
        assert not any(key[0] == record.project_id for key in live.rooms)
        before = await live.tree(record.project_id)
        history = await live.history(record.project_id)
        await live.tick()
        await live.drain(record.project_id)
        assert await live.tree(record.project_id) == before
        assert await live.history(record.project_id) == history
        # Resume recovers the existing outbox and history burst without a new edit.
        await live.resume(record.project_id)
        await live.tick()
        await live.drain(record.project_id)
        resumed = await live.tree(record.project_id)
        assert resumed.published_revision == before.saved_revision
        assert any(
            version["revision"] == before.saved_revision and version["kind"] == "auto"
            for version in await live.history(record.project_id)
        )
        assert (
            await asyncio.to_thread(stop_project_work, store, background, record.project_id, None)
            == []
        )
        store.delete_project_records(record.project_id)
        with store.connection() as connection:
            present = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema")}
            for table in PAPER_PROJECT_TABLES:
                if table in present:
                    assert not connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE project_id=?', (record.project_id,)
                    ).fetchall()
            assert connection.execute(
                "SELECT count(*) FROM paper_saved_snapshots WHERE project_id='other'"
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


def test_restore_detaches_runtime_without_resetting_publication(manifest, tmp_path, monkeypatch):
    store = AppStore(tmp_path / "rcp.sqlite3")
    catalog = ProjectCatalog(tmp_path, store, AgentLauncher())
    record = catalog.register(str(manifest.path), identity_action="adopted")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_publication VALUES (?, 8, 3, 'pending', 'offline', 'legacy', 'now')",
            (record.project_id,),
        )
        before = tuple(connection.execute("SELECT * FROM paper_publication").fetchone())
    owner = PaperOwner()
    monkeypatch.setitem(paper_lifecycle._owners, store, owner)
    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert [event[0] for event in owner.events] == ["pause", "drain", "dispose"]
    with store.connection() as connection:
        assert tuple(connection.execute("SELECT * FROM paper_publication").fetchone()) == before


def test_saved_paper_without_registered_owner_cannot_claim_quiescence(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    stop_paper_project(store, "legacy")
    with store.connection() as connection:
        connection.execute("INSERT INTO paper_publication(project_id) VALUES ('live')")
    with pytest.raises(RuntimeError):
        stop_paper_project(store, "live")


@pytest.mark.parametrize("saved", [False, True])
def test_released_project_stays_inactive_after_restart(tmp_path, monkeypatch, saved):
    from tests.test_transfer_source import _released_source

    _, app, store, _, transfer, _, _ = _released_source(tmp_path)
    live = app.state.paper_live
    monkeypatch.setattr(limits, "PAPER_PUBLISH_IDLE_SECONDS", 0)
    monkeypatch.setattr(limits, "PAPER_HISTORY_IDLE_SECONDS", 0)

    async def scenario():
        # Saved rows may predate the release, but startup must not resume their outbox.
        if saved:
            await live.apply(transfer.project_id, [PaperOp(kind="create", path="held.md")], "human")
        await live.close()
        restarted = PaperLive(store, live.workspace_for)
        await restarted.start()
        if not saved:
            with pytest.raises(PaperConflict):
                await restarted.tree(transfer.project_id)
            assert store.project_transfer_record_schema_version(transfer.project_id) < 7
            await restarted.close()
            return
        before = await restarted.tree(transfer.project_id)
        history = await restarted.history(transfer.project_id)
        await restarted.tick()
        await restarted.drain(transfer.project_id)
        assert await restarted.tree(transfer.project_id) == before
        assert await restarted.history(transfer.project_id) == history
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
        (action, record.project_id) for action in ("pause", "drain", "dispose", "resume")
    ]
