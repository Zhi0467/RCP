"""Per-project display choices for conversations: archived out of the list, or renamed.

Neither choice touches a transcript or task; a row goes away once it holds neither.
Read markers are per user: each records the newest turn end that user has seen.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class ChatDisplayStoreMixin:
    def chat_display(self, project_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT chat_id, title, archived_at FROM chat_display "
                "WHERE project_id = ? ORDER BY chat_id",
                (project_id,),
            ).fetchall()
        return {
            "archived": [row["chat_id"] for row in rows if row["archived_at"] is not None],
            "titles": {row["chat_id"]: row["title"] for row in rows if row["title"] is not None},
        }

    def set_chat_archived(
        self, project_id: str, chat_id: str, user_id: str, *, archived: bool
    ) -> None:
        self._set_chat_display(
            project_id,
            chat_id,
            "archived_user_id = ?, archived_at = ?",
            (user_id, self.now()) if archived else (None, None),
        )

    def set_chat_title(
        self, project_id: str, chat_id: str, user_id: str, title: str | None
    ) -> None:
        """A None title returns the conversation to its derived name."""
        self._set_chat_display(
            project_id,
            chat_id,
            "title = ?, titled_user_id = ?",
            (title, user_id if title is not None else None),
        )

    def chat_reads(self, project_id: str, user_id: str) -> dict[str, Any]:
        """A chat without a marker counts as read through the migration that added markers."""
        with self.connection() as connection:
            baseline = connection.execute(
                "SELECT completed_at FROM storage_schema_migrations "
                "WHERE migration_name = 'chat_reads_v1'"
            ).fetchone()
            rows = connection.execute(
                "SELECT chat_id, read_through FROM chat_reads "
                "WHERE project_id = ? AND user_id = ? ORDER BY chat_id",
                (project_id, user_id),
            ).fetchall()
        return {
            "baseline": baseline["completed_at"],
            "reads": {row["chat_id"]: row["read_through"] for row in rows},
        }

    def mark_chat_read(
        self, project_id: str, chat_id: str, user_id: str, read_through: datetime
    ) -> None:
        """Move the marker forward only, so a late request cannot unread a newer turn."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT read_through FROM chat_reads "
                "WHERE project_id = ? AND chat_id = ? AND user_id = ?",
                (project_id, chat_id, user_id),
            ).fetchone()
            if row is not None and datetime.fromisoformat(row["read_through"]) >= read_through:
                return
            connection.execute(
                "INSERT INTO chat_reads (project_id, chat_id, user_id, read_through) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, chat_id, user_id) "
                "DO UPDATE SET read_through = excluded.read_through",
                (project_id, chat_id, user_id, read_through.isoformat()),
            )

    def _set_chat_display(
        self, project_id: str, chat_id: str, assignment: str, values: tuple[Any, ...]
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO chat_display (project_id, chat_id) VALUES (?, ?) "
                "ON CONFLICT(project_id, chat_id) DO NOTHING",
                (project_id, chat_id),
            )
            connection.execute(
                f"UPDATE chat_display SET {assignment} WHERE project_id = ? AND chat_id = ?",
                (*values, project_id, chat_id),
            )
            connection.execute(
                "DELETE FROM chat_display WHERE project_id = ? AND chat_id = ? "
                "AND title IS NULL AND archived_at IS NULL",
                (project_id, chat_id),
            )
