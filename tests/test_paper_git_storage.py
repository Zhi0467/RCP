from __future__ import annotations

from pathlib import Path

from rcp.storage import AppStore
from rcp.storage.paper_git_links import PaperGitLink


def test_link_roundtrip_and_delete(tmp_path: Path):
    store = AppStore(tmp_path / "state.sqlite")
    assert store.paper_git_link("p") is None
    link = PaperGitLink(project_id="p", repository="owner/paper", linked_by="member")
    store.save_paper_git_link(link)
    assert store.paper_git_link("p") == link
    link.held = [{"file_id": "f", "path": "paper.tex", "base_commit": "abc", "kind": "text"}]
    store.save_paper_git_link(link)
    assert store.paper_git_links() == [link]
    store.delete_paper_git_link("p")
    assert store.paper_git_link("p") is None


def test_previous_schema_opens_and_edits_unlinked(tmp_path: Path):
    from rcp.paper import PaperLive
    from rcp.paper.folder import LocalPaperFolder
    from tests.paper_helpers import create, human_edit, saved, start_paper

    path = tmp_path / "old.sqlite"
    store = AppStore(path)
    with store.connection() as connection:
        connection.execute("DROP TABLE paper_git_links")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=82")
    assert store.paper_git_link("p") is None
    root = tmp_path / "paper"
    root.mkdir()
    live = start_paper(PaperLive(store, lambda _: LocalPaperFolder(root)))
    file_id = create(live, "paper.tex", "old writer")
    live.run_sync(live.close())
    store.close()
    upgraded = AppStore(path)
    live = start_paper(PaperLive(upgraded, lambda _: LocalPaperFolder(root)))
    assert saved(live).files[0].text == "old writer"
    human_edit(live, file_id, "new writer")
    assert upgraded.paper_git_link("p") is None
    assert (root / "paper.tex").read_text() == "new writer"


def test_delete_removes_machine_local_link_and_transport(tmp_path: Path):
    from tests.test_storage import _project

    store = AppStore(tmp_path / "state.sqlite")
    store.upsert_project(_project("delete-me"))
    store.save_paper_git_link(
        PaperGitLink(project_id="delete-me", repository="owner/paper", linked_by="member")
    )
    directory = tmp_path / "paper-git/delete-me"
    directory.mkdir(parents=True)
    (directory / "id_ed25519").write_bytes(b"disposable fixture")
    store.delete_project_records("delete-me")
    assert store.paper_git_link("delete-me") is None
    assert not directory.exists()
