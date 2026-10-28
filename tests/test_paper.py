from __future__ import annotations

from contextlib import contextmanager

from rcp.paper import PaperService
from rcp.storage import AppStore, ProjectRecord
from rcp.transport import StateUnavailable, StateWorkspace


class RecordingWorkspace(StateWorkspace):
    def __init__(self, root) -> None:
        super().__init__(root, "test-host:/canonical/.research")
        self.remote = True
        self.refreshes = 0
        self.transactions = 0
        self.published: list[list[str]] = []
        self.generation = None

    @contextmanager
    def read_snapshot(self):
        yield self.generation or self.root

    def refresh(self) -> bool:
        self.refreshes += 1
        return True

    def refresh_if_stale(self, max_age_seconds: float = 2.0) -> bool:
        del max_age_seconds
        return self.refresh()

    @contextmanager
    def transaction(self):
        self.transactions += 1
        yield

    def publish(self, relative_paths) -> None:
        self.published.append([str(path) for path in relative_paths])


class UnavailableWorkspace(RecordingWorkspace):
    def refresh_if_stale(self, max_age_seconds: float = 2.0) -> bool:
        del max_age_seconds
        raise StateUnavailable("offline")

    @contextmanager
    def transaction(self):
        self.transactions += 1
        raise StateUnavailable("offline")
        yield


def test_last_good_canonical_remains_readable_while_remote_is_unavailable(
    manifest, tmp_path
) -> None:
    canonical = manifest.research_dir / "paper" / "introduction.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# Cached introduction\n", encoding="utf-8")
    service = PaperService(
        manifest,
        AppStore(tmp_path / "app.sqlite3"),
        UnavailableWorkspace(manifest.research_dir),
    )

    snapshot = service.snapshot()

    assert snapshot.sync_state == "synced"
    assert snapshot.content == "# Cached introduction\n"
    assert snapshot.canonical_available is True


def test_legacy_named_draft_is_copied_to_stable_project_id(manifest, tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    seed_draft(store, manifest.name, "# Legacy draft\n")
    stable_project_id = "11111111-1111-4111-8111-111111111111"
    store.upsert_project(
        ProjectRecord(
            project_id=stable_project_id,
            home_space_id=store.space_id,
            locator=str(manifest.path),
            name=manifest.name,
            state_location=str(manifest.research_dir),
            state_remote=False,
            added_at=store.now(),
        )
    )

    store.migrate_legacy_project_data(manifest.name, stable_project_id)

    with store.connection() as connection:
        copied = connection.execute(
            "SELECT content, ancestor_content FROM paper_drafts WHERE project_id = ?",
            (stable_project_id,),
        ).fetchone()
        legacy_row = connection.execute(
            "SELECT 1 FROM paper_drafts WHERE project_id = ?",
            (manifest.name,),
        ).fetchone()
    assert copied is not None
    assert copied["content"] == "# Legacy draft\n"
    assert copied["ancestor_content"] == "# Legacy draft\n"
    assert legacy_row is None


def test_legacy_named_draft_is_deleted_when_canonical_draft_already_wins(
    manifest, tmp_path
) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    seed_draft(store, manifest.name, "# Legacy draft\n")
    stable_project_id = "11111111-1111-4111-8111-111111111111"
    store.upsert_project(
        ProjectRecord(
            project_id=stable_project_id,
            home_space_id=store.space_id,
            locator=str(manifest.path),
            name=manifest.name,
            state_location=str(manifest.research_dir),
            state_remote=False,
            added_at=store.now(),
        )
    )
    seed_draft(store, stable_project_id, "# Canonical draft\n")

    store.migrate_legacy_project_data(manifest.name, stable_project_id)

    with store.connection() as connection:
        rows = connection.execute(
            "SELECT project_id, content FROM paper_drafts ORDER BY project_id"
        ).fetchall()
    assert [dict(row) for row in rows] == [
        {"project_id": stable_project_id, "content": "# Canonical draft\n"}
    ]


def seed_draft(store, project_id, content):
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO paper_drafts(project_id,content,ancestor_content,updated_at) VALUES(?,?,?,?)",
            (project_id, content, content, store.now()),
        )
