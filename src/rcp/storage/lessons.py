"""The legacy lessons tables, kept as the file-memory migration source."""

from __future__ import annotations

import sqlite3


def migrate_operational_lessons(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS operational_lessons (lesson_id TEXT PRIMARY KEY, "
        "project_id TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, author_kind TEXT NOT NULL CHECK(author_kind IN ('agent','human')), "
        "operation_id TEXT, user_id TEXT, display_name TEXT, human_owned INTEGER NOT NULL "
        "CHECK(human_owned IN (0,1)), CHECK((author_kind='agent' AND operation_id IS NOT NULL "
        "AND user_id IS NULL AND display_name IS NULL) OR (author_kind='human' AND operation_id "
        "IS NULL AND user_id IS NOT NULL AND display_name IS NOT NULL AND human_owned=1)))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS operational_lessons_project ON "
        "operational_lessons(project_id,human_owned DESC,updated_at DESC,lesson_id)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS lesson_command_receipts (project_id TEXT NOT NULL, "
        "operation_id TEXT NOT NULL, subcommand TEXT NOT NULL, key TEXT NOT NULL, "
        "args_digest TEXT NOT NULL, result_json TEXT NOT NULL, "
        "PRIMARY KEY(operation_id,subcommand,key))"
    )
