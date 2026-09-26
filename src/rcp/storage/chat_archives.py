"""Which conversations a project's members have archived out of the agent list."""

from __future__ import annotations


class ChatArchiveStoreMixin:
    def archived_chat_ids(self, project_id: str) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM chat_archives WHERE project_id = ? ORDER BY archived_at",
                (project_id,),
            ).fetchall()
        return [row["chat_id"] for row in rows]

    def set_chat_archived(
        self, project_id: str, chat_id: str, user_id: str, *, archived: bool
    ) -> None:
        """Archive only hides a conversation; its transcript and tasks stay."""
        with self.connection() as connection:
            if archived:
                connection.execute(
                    "INSERT INTO chat_archives (project_id, chat_id, archived_user_id, archived_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, chat_id) DO NOTHING",
                    (project_id, chat_id, user_id, self.now()),
                )
            else:
                connection.execute(
                    "DELETE FROM chat_archives WHERE project_id = ? AND chat_id = ?",
                    (project_id, chat_id),
                )
