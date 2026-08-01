from __future__ import annotations

from contextlib import contextmanager

from rcp.paper import INTRODUCTION_TEMPLATE, PaperService
from rcp.storage import AppStore
from rcp.transport import StateUnavailable, StateWorkspace


class RecordingWorkspace(StateWorkspace):
    def __init__(self, root) -> None:
        super().__init__(root, "test-host:/canonical/.research")
        self.remote = True
        self.refreshes = 0
        self.transactions = 0
        self.published: list[list[str]] = []

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


def test_create_is_local_first_and_next_save_synchronizes(manifest, tmp_path) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    store = AppStore(tmp_path / "app.sqlite3")
    service = PaperService(manifest, store, workspace)

    created = service.create()

    assert created.sync_state == "unsynced"
    assert created.content == INTRODUCTION_TEMPLATE
    assert created.base_hash is None
    assert workspace.refreshes == 0
    assert workspace.transactions == 0
    assert workspace.published == []
    assert not (manifest.research_dir / "paper" / "introduction.md").exists()
    with store.connection() as connection:
        draft = connection.execute(
            "SELECT content, base_hash FROM paper_drafts WHERE project_id = ?",
            (manifest.name,),
        ).fetchone()
    assert draft is not None
    assert draft["content"] == INTRODUCTION_TEMPLATE
    assert draft["base_hash"] is None

    synchronized = service.save(created.content, created.base_hash)

    assert synchronized.sync_state == "synced"
    assert workspace.transactions == 1
    assert workspace.published == [["paper/introduction.md"]]
    assert (manifest.research_dir / "paper" / "introduction.md").read_text(
        encoding="utf-8"
    ) == INTRODUCTION_TEMPLATE


def test_create_preserves_synced_cached_canonical_without_workspace_io(manifest, tmp_path) -> None:
    workspace = RecordingWorkspace(manifest.research_dir)
    canonical = manifest.research_dir / "paper" / "introduction.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# Existing introduction\n", encoding="utf-8")
    service = PaperService(manifest, AppStore(tmp_path / "app.sqlite3"), workspace)

    created = service.create()

    assert created.sync_state == "synced"
    assert created.content == "# Existing introduction\n"
    assert created.base_hash is not None
    assert canonical.read_text(encoding="utf-8") == "# Existing introduction\n"
    assert workspace.refreshes == 0
    assert workspace.transactions == 0
    assert workspace.published == []


def test_save_keeps_local_draft_when_workspace_is_unavailable(manifest, tmp_path) -> None:
    workspace = UnavailableWorkspace(manifest.research_dir)
    store = AppStore(tmp_path / "app.sqlite3")
    service = PaperService(manifest, store, workspace)
    created = service.create()

    unsynced = service.save("# Recover this local draft\n", created.base_hash)

    assert unsynced.sync_state == "unsynced"
    assert unsynced.content == "# Recover this local draft\n"
    with store.connection() as connection:
        draft = connection.execute(
            "SELECT content FROM paper_drafts WHERE project_id = ?",
            (manifest.name,),
        ).fetchone()
    assert draft is not None
    assert draft["content"] == "# Recover this local draft\n"


def test_template_is_created_once_and_remains_freeform(manifest, tmp_path) -> None:
    service = PaperService(manifest, AppStore(tmp_path / "app.sqlite3"))

    created = service.create()
    assert created.sync_state == "unsynced"
    assert created.content == INTRODUCTION_TEMPLATE
    assert service.create().content == INTRODUCTION_TEMPLATE

    synchronized = service.save(created.content, created.base_hash)
    assert synchronized.sync_state == "synced"

    changed = service.save(
        "# My own structure\n\nNo enforced headings.\n", synchronized.base_hash
    )
    assert changed.sync_state == "synced"
    assert "What question we study" not in changed.content
    recreated = service.create()
    assert recreated.content == changed.content
    assert recreated.sync_state == "synced"


def test_external_change_creates_conflict_without_overwrite(manifest, tmp_path) -> None:
    service = PaperService(manifest, AppStore(tmp_path / "app.sqlite3"))
    created = service.create()
    synchronized = service.save(created.content, created.base_hash)
    canonical = manifest.research_dir / "paper" / "introduction.md"
    canonical.write_text("# Changed elsewhere\n", encoding="utf-8")

    conflicted = service.save("# Local human draft\n", synchronized.base_hash)
    assert conflicted.sync_state == "conflict"
    assert canonical.read_text(encoding="utf-8") == "# Changed elsewhere\n"

    resolved = service.resolve_conflict("use_canonical")
    assert resolved.sync_state == "synced"
    assert resolved.content == "# Changed elsewhere\n"


def test_legacy_named_draft_is_copied_to_stable_project_id(manifest, tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite3")
    legacy = PaperService(manifest, store)
    legacy.create()
    legacy.save("# Unsynced legacy draft\n", "not-the-canonical-hash")

    store.migrate_legacy_project_data(manifest.name, "stable-project-id")

    with store.connection() as connection:
        copied = connection.execute(
            "SELECT content FROM paper_drafts WHERE project_id = ?",
            ("stable-project-id",),
        ).fetchone()
    assert copied is not None
    assert copied["content"] == "# Unsynced legacy draft\n"
