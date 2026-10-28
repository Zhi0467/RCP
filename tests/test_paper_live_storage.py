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
            connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version>=62")
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


def test_migration_64_merges_prepared_and_committed_receipts(tmp_path):
    import json

    from rcp.storage.base import _migrate_paper_changes

    path = tmp_path / "old63.sqlite"
    store = AppStore(path)
    with store.connection() as conn:
        for table in ("paper_change_receipts", "paper_changes", "paper_change_groups"):
            conn.execute(f"DROP TABLE {table}")
        _migrate_paper_changes(conn)
        conn.execute(
            "CREATE TABLE paper_apply_receipts (project_id TEXT, receipt_key TEXT, fingerprint TEXT, receipt TEXT)"
        )
        conn.execute(
            "INSERT INTO paper_change_groups VALUES ('p','g','t',1,'review','pending',NULL,NULL,'now')"
        )
        conn.execute(
            "INSERT INTO paper_change_receipts VALUES ('p','key','g','accept',?, 'now')",
            (json.dumps({"inverse": [{"kind": "delete", "file_id": "f"}]}),),
        )
        receipt = {"revision": 2, "receipt_key": "key", "file_ids": ["f"]}
        conn.execute(
            "INSERT INTO paper_apply_receipts VALUES ('p','key','fingerprint',?)",
            (json.dumps(receipt),),
        )
        conn.execute(
            "INSERT INTO paper_apply_receipts VALUES ('p','generic','other',?)",
            (json.dumps({"revision": 1}),),
        )
        conn.execute("DELETE FROM storage_schema_migrations WHERE migration_version=64")
    store.close()
    upgraded = AppStore(path)
    with upgraded.connection() as conn:
        rows = {
            row["receipt_key"]: row for row in conn.execute("SELECT * FROM paper_change_receipts")
        }
        assert len(rows) == 2
        payload = json.loads(rows["key"]["payload"])
        assert payload["inverse"] == [{"kind": "delete", "file_id": "f"}]
        assert payload["receipt"] == receipt
        assert payload["fingerprint"] == "fingerprint"
        assert rows["generic"]["group_id"] is None
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='paper_apply_receipts'"
        ).fetchone()
    upgraded.close()


@pytest.mark.parametrize("component", ["a" * 256, "é" * 128])
def test_paper_path_limits_each_utf8_component(component):
    from rcp.paper.models import paper_path

    with pytest.raises(ValueError):
        paper_path(f"sections/{component}/file.md")
    assert paper_path("a" * 255 + "/file.md").endswith("/file.md")
