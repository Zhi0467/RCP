from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from rcp.paper import PaperLive
from rcp.paper import migration as paper_migration
from rcp.paper.folder import LocalPaperFolder
from rcp.paper.folder_io import paper_root
from rcp.paper.migration import migrate_paper, needs_paper_migration
from rcp.storage import AppStore
from rcp.transport import LocalStateWorkspace
from tests.paper_legacy_seed import legacy_tree, seed_legacy_paper


@pytest.fixture
def legacy(tmp_path):
    store = AppStore(tmp_path / "data" / "rcp.sqlite3")
    state = tmp_path / "state"
    state.mkdir()
    seeded = seed_legacy_paper(store, "p", state)
    folder = LocalPaperFolder(paper_root(tmp_path / "data", "p"))
    workspace = LocalStateWorkspace(state, str(state))
    yield store, seeded, folder, workspace
    store.close()


def _migrate(store, folder, workspace) -> bool:
    return migrate_paper(store, "p", folder, workspace.remove_paper_tree)


def _files(folder) -> dict[str, bytes]:
    paths = [entry.path for entry in folder.scan()]
    return {path: data for path, data in folder.read(paths).items() if data is not None}


def _row(store, sql, *args):
    with store.connection() as connection:
        row = connection.execute(sql, args).fetchone()
    return None if row is None else tuple(row)


def test_old_writer_paper_moves_into_its_folder_with_every_step(legacy):
    store, seeded, folder, workspace = legacy
    assert needs_paper_migration(store, "p")

    assert _migrate(store, folder, workspace)

    assert _files(folder) == legacy_tree()
    assert not (workspace.root / "paper").exists()
    assert not folder.root.parent.joinpath(".install-p").exists()
    assert not needs_paper_migration(store, "p")
    assert _row(store, "SELECT hold FROM paper_publication WHERE project_id='p'") == (None,)
    assert _row(store, "SELECT count(*) FROM paper_updates WHERE project_id='p'") == (0,)
    for file_id, text in (("intro", b"Hello world"), ("chapter", b"Chapter one\n")):
        assert _row(
            store,
            "SELECT text_sha256 FROM paper_doc_states WHERE project_id='p' AND file_id=?",
            file_id,
        ) == (hashlib.sha256(text).hexdigest(),)
    running = store.agent_task(seeded.running_coach)
    finished = store.agent_task(seeded.finished_coach)
    assert running is not None and finished is not None
    assert running.status == "interrupted" and finished.status == "succeeded"
    assert running.history_only and finished.history_only
    transcript = str((finished.result or {})["transcript"])
    for part in ("Coach request", "Coach answer", "Also fix the title", "sections/intro.md"):
        assert part in transcript
    assert _row(
        store, "SELECT group_concat(DISTINCT state) FROM paper_change_groups WHERE project_id='p'"
    ) == ("rejected",)
    with store.connection() as connection:
        warned = {
            row[0]
            for row in connection.execute(
                "SELECT operation_id FROM graph_run_events WHERE level='warning'"
            )
        }
    assert {seeded.running_coach, seeded.finished_coach} <= warned
    assert not _migrate(store, folder, workspace)


def test_editor_keeps_old_file_ids_and_uses_the_folded_cache(legacy):
    store, _seeded, folder, workspace = legacy
    live = PaperLive(
        store, lambda _project: folder, prepare=lambda project: _migrate(store, folder, workspace)
    )

    async def scenario():
        tree = await live.tree("p")
        ids = {file.path: file.file_id for file in tree.files}
        assert ids == {
            "chapter.tex": "chapter",
            "figures/plot.png": "figure",
            "sections/intro.md": "intro",
        }
        before = _row(store, "SELECT state FROM paper_doc_states WHERE file_id='intro'")
        room = await live.connect("p", "intro")
        assert str(room.doc["text"]) == "Hello world"
        assert _row(store, "SELECT state FROM paper_doc_states WHERE file_id='intro'") == before
        await live.close()

    asyncio.run(scenario())
    assert not needs_paper_migration(store, "p")


@pytest.mark.parametrize("step", ["_settle_database", "remove_projection", "_mark"])
def test_a_crash_after_any_step_resumes_to_the_same_result(legacy, monkeypatch, step):
    store, seeded, folder, workspace = legacy
    staging = folder.root.parent / ".install-p"
    staging.mkdir()
    (staging / "half-written.md").write_text("left by an interrupted install")

    def crash(*args, **kwargs):
        raise RuntimeError("crash")

    if step != "remove_projection":
        monkeypatch.setattr(paper_migration, step, crash)
    remove = crash if step == "remove_projection" else workspace.remove_paper_tree
    with pytest.raises(RuntimeError):
        migrate_paper(store, "p", folder, remove)
    monkeypatch.undo()
    assert needs_paper_migration(store, "p")
    assert _files(folder) == legacy_tree()

    assert _migrate(store, folder, workspace)

    assert _files(folder) == legacy_tree()
    assert not staging.exists()
    assert not (workspace.root / "paper").exists()
    running = store.agent_task(seeded.running_coach)
    assert running is not None and running.status == "interrupted"
    with store.connection() as connection:
        notices = connection.execute(
            "SELECT operation_id, count(*) FROM graph_run_events WHERE level='warning' "
            "GROUP BY operation_id"
        ).fetchall()
    assert notices and all(count == 1 for _operation, count in notices)
    finished = store.agent_task(seeded.finished_coach)
    assert finished is not None
    assert json.dumps(finished.result).count("Also fix the title") == 1


def test_a_draft_from_before_the_file_tree_becomes_introduction(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO paper_drafts(project_id, content, updated_at) "
                "VALUES ('p', 'Draft text', '2026-01-01T00:00:00+00:00')"
            )
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        assert migrate_paper(store, "p", folder, lambda: None)
        assert _files(folder) == {"introduction.md": b"Draft text"}
    finally:
        store.close()


def test_a_project_without_legacy_paper_owes_nothing(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        assert not migrate_paper(store, "p", folder, lambda: pytest.fail("nothing to remove"))
    finally:
        store.close()


def _fake_ssh(stage, home):
    import subprocess
    import sys

    def remote(arguments, *, input_data=None, **kwargs):
        return subprocess.run(
            [sys.executable, "-I", *arguments[1:]],
            input=input_data,
            capture_output=True,
            env={"HOME": str(home)},
        )

    stage._ssh_bytes = remote


def test_an_ssh_paper_installs_on_its_base(tmp_path):
    from rcp.paper.folder import RemotePaperFolder
    from rcp.transport.run_stage import RemoteRunStage

    store = AppStore(tmp_path / "data" / "rcp.sqlite3")
    try:
        seed_legacy_paper(store, "p", None)
        stage = RemoteRunStage("example.invalid")
        (tmp_path / "base").mkdir()
        _fake_ssh(stage, tmp_path / "base")
        folder = RemotePaperFolder(stage, "p")
        assert migrate_paper(store, "p", folder, lambda: None)
        assert _files(folder) == legacy_tree()
        assert (tmp_path / "base/.rcp/paper/p/figures/plot.png").exists()
    finally:
        store.close()


def test_project_identity_change_moves_and_deletion_removes_the_folder(tmp_path):
    from rcp.machine_cards import BACKEND_MACHINE_ID
    from rcp.paper.folder import change_project_paper
    from rcp.storage import ProjectRecord

    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        old = LocalPaperFolder(paper_root(tmp_path, "old"))
        old.write("main.tex", b"body", None)
        paper_root(tmp_path, "new")
        project = ProjectRecord.model_validate(
            {
                "project_id": "old",
                "name": "P",
                "locator": str(tmp_path / "m.toml"),
                "state_machine_id": BACKEND_MACHINE_ID,
                "state_path": str(tmp_path / "state"),
                "added_at": "2026-01-01T00:00:00+00:00",
            }
        )
        change_project_paper(store, project, "new")
        assert not old.root.exists()
        assert (tmp_path / "paper/new/main.tex").read_bytes() == b"body"
        change_project_paper(store, project, "new")
        moved = project.model_copy(update={"project_id": "new"})
        LocalPaperFolder(paper_root(tmp_path, "other")).write("x.md", b"x", None)
        with pytest.raises(ValueError):
            change_project_paper(store, moved, "other")
        change_project_paper(store, moved, None)
        assert not (tmp_path / "paper/new").exists()
    finally:
        store.close()


def test_ssh_identity_change_and_deletion_run_on_the_base(tmp_path):
    from rcp.paper.folder import RemotePaperFolder
    from rcp.transport.run_stage import RemoteRunStage

    stage = RemoteRunStage("example.invalid")
    _fake_ssh(stage, tmp_path)
    RemotePaperFolder(stage, "old").write("a.md", b"a", None)
    stage.paper_folder({"op": "change", "project_id": "old", "destination": "new"})
    assert (tmp_path / ".rcp/paper/new/a.md").read_bytes() == b"a"
    stage.paper_folder({"op": "change", "project_id": "new", "destination": None})
    assert not (tmp_path / ".rcp/paper/new").exists()
