"""One-time migration receipt for the legacy operational lesson store."""

from __future__ import annotations

import sqlite3


def migrate_memory_files(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS memory_migrations ("
        "project_id TEXT PRIMARY KEY, migrated_at TEXT NOT NULL, lesson_count INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS memory_deletions ("
        "project_id TEXT NOT NULL, state_location TEXT NOT NULL, project_json TEXT NOT NULL, host_key TEXT, "
        "PRIMARY KEY (project_id, state_location))"
    )
