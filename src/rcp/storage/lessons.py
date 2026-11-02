"""Read-only legacy lessons retained for file-memory migration."""

from __future__ import annotations

import sqlite3
from typing import Any

from rcp.storage.mixin_base import StoreMixinBase


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


def _record(row: sqlite3.Row) -> dict[str, Any]:
    author = {"kind": row["author_kind"]}
    if row["author_kind"] == "agent":
        author["operation_id"] = row["operation_id"]
    else:
        author.update(user_id=row["user_id"], display_name=row["display_name"])
    return {
        **{name: row[name] for name in ("lesson_id", "text", "created_at", "updated_at")},
        "author": author,
        "human_owned": bool(row["human_owned"]),
    }


class LessonStoreMixin(StoreMixinBase):
    def list_lessons(self, project_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                _record(row)
                for row in connection.execute(
                    "SELECT * FROM operational_lessons WHERE project_id=? "
                    "ORDER BY human_owned DESC,updated_at DESC,lesson_id",
                    (project_id,),
                )
            ]
