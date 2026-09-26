"""Per-project display choices for conversations: archived out of the list, or renamed.

Neither choice touches a transcript or task; a row goes away once it holds neither.
"""

from __future__ import annotations

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
