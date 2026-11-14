"""SQLite caches beside the Paper folder: revision counter, buffer hashes, turn writes."""

from __future__ import annotations

import sqlite3

PAPER_TURN_FILE_KINDS = ("write", "delete", "rename", "outside_turn", "unsupported")


def migrate_paper_folder(connection: sqlite3.Connection) -> None:
    from rcp.storage.resource_versions import install_paper_state_versions

    connection.execute(
        """CREATE TABLE paper_state (
            project_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
            turn_operation_id TEXT,
            turn_version_id TEXT)"""
    )
    connection.execute(
        "INSERT INTO paper_state(project_id, revision) "
        "SELECT project_id, saved_revision FROM paper_publication"
    )
    connection.execute("ALTER TABLE paper_doc_states ADD COLUMN text_sha256 TEXT")
    kinds = ",".join(f"'{kind}'" for kind in PAPER_TURN_FILE_KINDS)
    connection.execute(
        f"""CREATE TABLE paper_turn_files (
            project_id TEXT NOT NULL,
            operation_id TEXT,
            path TEXT NOT NULL,
            base_sha TEXT,
            coach_sha TEXT,
            clash INTEGER NOT NULL DEFAULT 0 CHECK(clash IN (0, 1)),
            kind TEXT NOT NULL CHECK(kind IN ({kinds})),
            created_at TEXT NOT NULL)"""
    )
    connection.execute(
        "CREATE INDEX paper_turn_files_operation ON paper_turn_files(project_id, operation_id)"
    )
    install_paper_state_versions(connection)
