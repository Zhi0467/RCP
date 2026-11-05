"""One-time migration receipt for the legacy operational lesson store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable


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


def migrate_memory_deletion_machines(
    connection: sqlite3.Connection, upgraded_project_json: Callable[[str], str]
) -> None:
    """Key pending memory deletions by the state card and path (migration 77).

    `machine_id` is the card the deleted project's memory lives on; cleanup
    reads its route from the card.
    """

    columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_deletions)")}
    if "state_machine_id" in columns:
        return
    rows = connection.execute("SELECT project_id, project_json FROM memory_deletions").fetchall()
    connection.execute("DROP TABLE memory_deletions")
    connection.execute(
        "CREATE TABLE memory_deletions ("
        "project_id TEXT NOT NULL, state_machine_id TEXT NOT NULL, state_path TEXT NOT NULL, "
        "project_json TEXT NOT NULL, machine_id TEXT, "
        "PRIMARY KEY (project_id, state_machine_id, state_path))"
    )
    for project_id, project_json in rows:
        upgraded = upgraded_project_json(str(project_json))
        project = json.loads(upgraded)
        connection.execute(
            "INSERT OR IGNORE INTO memory_deletions VALUES (?, ?, ?, ?, ?)",
            (
                project_id,
                project["state_machine_id"],
                project["state_path"],
                upgraded,
                project["state_machine_id"],
            ),
        )
