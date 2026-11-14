"""Schema history for the retired Paper coach steering table."""

from __future__ import annotations

import sqlite3


def migrate_paper_coach_steers(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE paper_coach_steers (
        message_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL REFERENCES graph_runs(operation_id),
        text TEXT NOT NULL, attempt INTEGER NOT NULL, turn_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('delivered', 'refused', 'unknown')),
        label TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL)""")
    connection.execute(
        "CREATE INDEX paper_coach_steers_operation ON paper_coach_steers(operation_id, created_at)"
    )
