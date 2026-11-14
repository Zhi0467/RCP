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
            for table in (*PAPER_TABLES, "paper_state", "paper_turn_files"):
                connection.execute(f'DROP TABLE "{table}"')
            connection.execute(
                "DELETE FROM storage_schema_migrations WHERE migration_version IN (63, 85)"
            )
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


def test_folder_migration_seeds_the_revision_from_the_saved_one(tmp_path):
    from rcp.storage.paper_folder import migrate_paper_folder

    store = AppStore(tmp_path / "app.sqlite3")
    try:
        with store.connection() as connection:
            for statement in (
                "DROP TABLE paper_state",
                "DROP TABLE paper_turn_files",
                "ALTER TABLE paper_doc_states DROP COLUMN text_sha256",
            ):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO paper_publication(project_id,saved_revision,published_revision) "
                "VALUES ('p',7,5)"
            )
            migrate_paper_folder(connection)
            assert connection.execute("SELECT project_id, revision FROM paper_state").fetchall()[0][
                :
            ] == ("p", 7)
    finally:
        store.close()


def test_pruning_keeps_the_running_turns_before_version(tmp_path, monkeypatch):
    from rcp import limits
    from rcp.paper.persistence import prune

    monkeypatch.setattr(limits, "PAPER_HISTORY_AUTO_LIMIT", 1)
    store = AppStore(tmp_path / "app.sqlite3")
    try:
        with store.connection() as connection:
            connection.executemany(
                "INSERT INTO paper_versions VALUES ('p',?,?,'auto',NULL,'automatic',?,'[]')",
                [(f"v{index}", index, f"2026-01-0{index}") for index in (1, 2, 3)],
            )
            connection.execute(
                "INSERT INTO paper_state VALUES ('p', 3, 'op', 'v1')",
            )
            prune(connection, "p")
            assert [
                row[0]
                for row in connection.execute(
                    "SELECT version_id FROM paper_versions ORDER BY version_id"
                )
            ] == ["v1", "v3"]
    finally:
        store.close()
