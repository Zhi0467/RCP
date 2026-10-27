from __future__ import annotations

import sqlite3

import pytest

from rcp.storage import AppStore

PAPER_TABLES = (
    "paper_files",
    "paper_updates",
    "paper_doc_states",
    "paper_blobs",
    "paper_revisions",
    "paper_publication",
    "paper_versions",
    "paper_version_texts",
    "paper_apply_receipts",
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
            connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=62")
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
                    "SELECT COUNT(*) FROM storage_schema_migrations WHERE migration_version=62"
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
