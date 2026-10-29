from __future__ import annotations

import sqlite3

import pytest

from rcp.storage import AppStore

PAPER_TABLES = (
    "paper_changes",
    "paper_change_groups",
    "paper_files",
    "paper_updates",
    "paper_doc_states",
    "paper_blobs",
    "paper_revisions",
    "paper_publication",
    "paper_versions",
    "paper_version_texts",
    "paper_change_receipts",
    "paper_saved_snapshots",
)


@pytest.mark.parametrize("upgrade", [False, True])
def test_paper_schema_fresh_and_upgrade_preserve_legacy_rows(tmp_path, upgrade):
    path = tmp_path / "app.sqlite3"
    store = AppStore(path)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_drafts "
            "(project_id, content, base_hash, updated_at, ancestor_content) VALUES (?, ?, ?, ?, ?)",
            ("legacy", "draft text", "base-hash", store.now(), "ancestor text"),
        )
        if upgrade:
            for table in PAPER_TABLES:
                connection.execute(f'DROP TABLE "{table}"')
            connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=63")
    store.close()

    for _ in range(2):
        reopened = AppStore(path)
        with reopened.connection() as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert set(PAPER_TABLES) <= tables
            assert connection.execute(
                "SELECT content, ancestor_content FROM paper_drafts WHERE project_id='legacy'"
            ).fetchone()[:] == ("draft text", "ancestor text")
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM storage_schema_migrations WHERE migration_version=63"
                ).fetchone()[0]
                == 1
            )
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        reopened.close()


def test_paper_live_path_is_unique_but_deleted_paths_can_be_reused(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_files VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("project", "old", "section.md", "text", store.now(), None, None),
        )
    with pytest.raises(sqlite3.IntegrityError), store.connection() as connection:
        connection.execute(
            "INSERT INTO paper_files VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("project", "new", "section.md", "text", store.now(), None, None),
        )
    with store.connection() as connection:
        connection.execute(
            "UPDATE paper_files SET deleted_at=? WHERE file_id='old'", (store.now(),)
        )
        connection.execute(
            "INSERT INTO paper_files VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("project", "new", "section.md", "text", store.now(), None, None),
        )
        assert connection.execute("SELECT COUNT(*) FROM paper_files").fetchone()[0] == 2
    store.close()


@pytest.mark.parametrize("component", ["a" * 256, "é" * 128])
def test_paper_path_limits_each_utf8_component(component):
    from rcp.paper.models import paper_path

    with pytest.raises(ValueError):
        paper_path(f"sections/{component}/file.md")
    assert paper_path("a" * 255 + "/file.md").endswith("/file.md")


@pytest.mark.parametrize("published,newest", [(2, 4), (4, 2), (4, None)])
def test_revision_pruning_requires_publication_and_retained_version(tmp_path, published, newest):
    from rcp.paper.persistence import prune

    store = AppStore(tmp_path / "app.sqlite3")
    try:
        with store.connection() as connection:
            connection.execute(
                "INSERT INTO paper_publication(project_id,saved_revision,published_revision) "
                "VALUES ('p',5,?)",
                (published,),
            )
            connection.executemany(
                "INSERT INTO paper_revisions VALUES (?,?, 'now','human','edit')",
                [(project, revision) for project in ("p", "other") for revision in range(1, 6)],
            )
            if newest is not None:
                connection.executemany(
                    "INSERT INTO paper_versions VALUES ('p',?,?,'named',?,'human','now','[]')",
                    [(str(revision), revision, str(revision)) for revision in (1, newest)],
                )
            prune(connection, "p")
            boundary = min(published, newest) if newest is not None else 0
            assert [
                row[0]
                for row in connection.execute(
                    "SELECT revision FROM paper_revisions WHERE project_id='p' ORDER BY revision"
                )
            ] == list(range(boundary + 1, 6))
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM paper_revisions WHERE project_id='other'"
                ).fetchone()[0]
                == 5
            )
    finally:
        store.close()
