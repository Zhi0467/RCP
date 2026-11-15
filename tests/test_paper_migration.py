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
    return migrate_paper(store, "p", folder, lambda: workspace)


def _empty_workspace(tmp_path):
    root = tmp_path / "empty-state"
    root.mkdir(exist_ok=True)
    return LocalStateWorkspace(root, str(root))


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

    if step == "remove_projection":
        monkeypatch.setattr(workspace, "remove_paper_tree", crash)
    else:
        monkeypatch.setattr(paper_migration, step, crash)
    with pytest.raises(RuntimeError):
        _migrate(store, folder, workspace)
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
        assert migrate_paper(store, "p", folder, lambda: _empty_workspace(tmp_path))
        assert _files(folder) == {"introduction.md": b"Draft text"}
        assert _row(store, "SELECT count(*) FROM paper_drafts") == (0,)
    finally:
        store.close()


def test_a_project_without_legacy_paper_owes_nothing(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        assert not migrate_paper(store, "p", folder, lambda: _empty_workspace(tmp_path))
        assert not migrate_paper(store, "p", folder, lambda: pytest.fail("already settled"))
        assert _files(folder) == {}
        assert _row(store, "SELECT count(*) FROM paper_migrations WHERE project_id='p'") == (1,)
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
        assert migrate_paper(store, "p", folder, lambda: _empty_workspace(tmp_path))
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


def test_startup_migrates_every_registered_project_through_its_workspace(manifest, tmp_path):
    from rcp.agents import AgentLauncher
    from rcp.projects import ProjectCatalog

    data_dir = tmp_path / "data"
    store = AppStore(data_dir / "rcp.sqlite3")
    try:
        catalog = ProjectCatalog(data_dir, store, AgentLauncher())
        record = catalog.register(str(manifest.path), identity_action="adopted")
        seed_legacy_paper(store, record.project_id, manifest.research_dir)
        assert catalog.migrate_papers() == []
        folder = catalog.paper_folder(record.project_id)
        assert _files(folder) == legacy_tree()
        assert not (manifest.research_dir / "paper").exists()
        assert not needs_paper_migration(store, record.project_id)
    finally:
        store.close()


def test_body_collection_keeps_the_saved_tree_of_an_unmigrated_project(legacy):
    from rcp.paper.persistence import collect_bodies

    store, _seeded, folder, workspace = legacy
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        collect_bodies(connection)

    assert _migrate(store, folder, workspace)
    assert _files(folder) == legacy_tree()


def _projection(tmp_path, files: dict[str, str]) -> LocalStateWorkspace:
    workspace = _empty_workspace(tmp_path)
    for path, text in files.items():
        (workspace.root / "paper" / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace.root / "paper" / path).write_text(text)
    return workspace


def _versions(store) -> dict[str, dict[str, str]]:
    from rcp.paper.persistence import PaperSnapshotReader

    with store.connection() as connection:
        reader = PaperSnapshotReader(connection)
        names = connection.execute(
            "SELECT name, version_id FROM paper_versions WHERE project_id='p' AND kind='named'"
        ).fetchall()
        return {
            name: {file.path: file.text or "" for file in reader.snapshot("p", version).files}
            for name, version in names
        }


def test_a_paper_published_only_to_the_state_moves_into_its_folder(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        workspace = _projection(tmp_path, {"introduction.md": "Published", "a/b.tex": "B"})
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        live = PaperLive(
            store, lambda _project: folder, prepare=lambda p: _migrate(store, folder, workspace)
        )

        async def tree():
            files = (await live.tree("p")).files
            await live.close()
            return {file.path for file in files}

        assert asyncio.run(tree()) == {"introduction.md", "a/b.tex"}
        assert _files(folder) == {"introduction.md": b"Published", "a/b.tex": b"B"}
        assert not (workspace.root / "paper").exists()
    finally:
        store.close()


def test_a_committed_paper_tree_without_paper_rows_is_adopted(tmp_path):
    import subprocess

    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        workspace = _projection(tmp_path, {"introduction.md": "Intro", "fig/a.png": "PNG"})
        for command in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "paper"]):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *command],
                cwd=workspace.root,
                check=True,
            )
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))

        assert _migrate(store, folder, workspace)

        assert _files(folder) == {"introduction.md": b"Intro", "fig/a.png": b"PNG"}
        assert not (workspace.root / "paper").exists()
        assert _row(store, "SELECT count(*) FROM paper_migrations WHERE project_id='p'") == (1,)
        assert _row(
            store, "SELECT count(DISTINCT file_id) FROM paper_files WHERE project_id='p'"
        ) == (2,)
        assert _versions(store) == {}
    finally:
        store.close()


def test_a_draft_behind_the_published_text_never_overwrites_it(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO paper_drafts(project_id, content, base_hash, updated_at) "
                "VALUES ('p', 'Older draft', 'deadbeef', '2026-01-01T00:00:00+00:00')"
            )
        workspace = _projection(tmp_path, {"introduction.md": "Newer published"})
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        assert _migrate(store, folder, workspace)
        assert _files(folder) == {"introduction.md": b"Newer published"}
        assert {"introduction.md": "Older draft"} in _versions(store).values()
    finally:
        store.close()


def test_a_draft_ahead_of_the_published_text_wins_and_history_keeps_the_published(tmp_path):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO paper_drafts(project_id, content, base_hash, updated_at) "
                "VALUES ('p', 'Edited draft', ?, '2026-01-01T00:00:00+00:00')",
                (hashlib.sha256(b"Published").hexdigest(),),
            )
        workspace = _projection(tmp_path, {"introduction.md": "Published"})
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        assert _migrate(store, folder, workspace)
        assert _files(folder) == {"introduction.md": b"Edited draft"}
        assert {"introduction.md": "Published"} in _versions(store).values()
    finally:
        store.close()


def test_a_published_tree_that_differs_from_the_saved_tree_stays_in_history(legacy):
    store, _seeded, folder, workspace = legacy
    assert _migrate(store, folder, workspace)
    assert {"sections/intro.md": "Hello"} in _versions(store).values()


def _seed_saved_tree(store, files: dict[str, str]) -> None:
    import zlib

    manifest = []
    with store.connection() as connection:
        for index, (path, text) in enumerate(sorted(files.items())):
            sha = hashlib.sha256(text.encode()).hexdigest()
            connection.execute(
                "INSERT OR IGNORE INTO paper_version_texts VALUES (?, ?)",
                (sha, zlib.compress(text.encode())),
            )
            manifest.append(
                {"file_id": f"f{index}", "path": path, "kind": "text", "text_sha256": sha}
            )
        connection.execute(
            "INSERT INTO paper_publication VALUES ('p', 1, 1, '2026-01-01T00:00:00+00:00', "
            "NULL, NULL, '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO paper_saved_snapshots VALUES ('p', 1, ?)", (json.dumps(manifest),)
        )


def test_case_clashing_saved_paths_both_survive_and_hidden_paths_are_logged(tmp_path, caplog):
    store = AppStore(tmp_path / "rcp.sqlite3")
    try:
        _seed_saved_tree(store, {"Notes.md": "upper", "notes.md": "lower", "build/main.aux": "aux"})
        folder = LocalPaperFolder(paper_root(tmp_path, "p"))
        with caplog.at_level("WARNING", logger="rcp.paper.migration"):
            assert migrate_paper(store, "p", folder, lambda: _empty_workspace(tmp_path))
        files = _files(folder)
        assert sorted(files.values()) == [b"lower", b"upper"]
        assert len({path.casefold() for path in files}) == 2
        assert "build/main.aux" in caplog.text
    finally:
        store.close()


def test_startup_moves_papers_after_machine_cards_settle_and_before_recovery(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from rcp.api.app import create_app
    from rcp.projects import ProjectCatalog

    order = []
    monkeypatch.setattr(
        "rcp.api.app.fill_space_machines", lambda *args, **kwargs: order.append("cards")
    )
    monkeypatch.setattr(ProjectCatalog, "migrate_papers", lambda self: order.append("paper"))
    app = create_app(data_dir=tmp_path)
    monkeypatch.setattr(
        app.state.background_tasks, "recover_at_startup", lambda: order.append("recovery")
    )
    with TestClient(app, base_url="http://127.0.0.1:8421"):
        assert order == ["cards", "paper", "recovery"]


def test_startup_leaves_a_paper_on_an_unreachable_host_for_later(manifest, tmp_path):
    from rcp.agents import AgentLauncher
    from rcp.paper.migration import migrate_papers
    from rcp.projects import ProjectCatalog

    class Unreachable:
        remote = True

        def reachable(self) -> bool:
            return False

        def install(self, files):
            pytest.fail("startup must not wait on an unreachable host")

    data_dir = tmp_path / "data"
    store = AppStore(data_dir / "rcp.sqlite3")
    try:
        catalog = ProjectCatalog(data_dir, store, AgentLauncher())
        record = catalog.register(str(manifest.path), identity_action="adopted")
        seed_legacy_paper(store, record.project_id, manifest.research_dir)
        failed = migrate_papers(store, lambda _p: Unreachable(), catalog._paper_workspace)
        assert failed == [record.project_id]
        assert needs_paper_migration(store, record.project_id)
        assert (manifest.research_dir / "paper").exists()
    finally:
        store.close()


def test_the_remote_reachability_probe_uses_the_short_startup_timeout(monkeypatch):
    from rcp import limits
    from rcp.paper.folder import RemotePaperFolder
    from rcp.transport.run_stage import RemoteRunStage

    stage = RemoteRunStage("example.invalid")
    seen = []

    def unreachable(arguments, *, input_data=None, timeout_seconds=None, **kwargs):
        import subprocess

        seen.append(timeout_seconds)
        return subprocess.CompletedProcess(arguments, 255, b"", b"no route")

    monkeypatch.setattr(stage, "_ssh_bytes", unreachable)
    assert not RemotePaperFolder(stage, "p").reachable()
    assert seen == [limits.PAPER_STARTUP_PROBE_SECONDS]
