"""Per-project display choices for conversations: archived, renamed, or pinned.

No choice touches a transcript or task; a row goes away once it holds none.
Read markers are per user: each records the newest turn end that user has seen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rcp.core.transition_models import GraphTargetRef


class ChatDisplayStoreMixin:
    def chat_display(self, project_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT chat_id, title, archived_at, pinned_at FROM chat_display "
                "WHERE project_id = ? ORDER BY chat_id",
                (project_id,),
            ).fetchall()
        pinned = sorted(
            (row for row in rows if row["pinned_at"] is not None),
            key=lambda row: row["pinned_at"],
            reverse=True,
        )
        return {
            "archived": [row["chat_id"] for row in rows if row["archived_at"] is not None],
            "titles": {row["chat_id"]: row["title"] for row in rows if row["title"] is not None},
            # Newest pin first.
            "pinned": [row["chat_id"] for row in pinned],
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

    def set_chat_pinned(self, project_id: str, chat_id: str, user_id: str, *, pinned: bool) -> None:
        self._set_chat_display(
            project_id,
            chat_id,
            "pinned_user_id = ?, pinned_at = ?",
            (user_id, self.now()) if pinned else (None, None),
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

    def chat_reads(
        self, project_id: str, user_id: str, graph_target: GraphTargetRef
    ) -> dict[str, Any]:
        """A chat without a marker counts as read through the migration that added markers."""
        with self.connection() as connection:
            baseline = connection.execute(
                "SELECT completed_at FROM storage_schema_migrations "
                "WHERE migration_name = 'chat_reads_and_pins_v1'"
            ).fetchone()
            rows = connection.execute(
                "SELECT chat_id, read_through FROM chat_reads "
                "WHERE project_id = ? AND user_id = ? ORDER BY chat_id",
                (project_id, user_id),
            ).fetchall()
            # Finished turns as `AgentTaskRecord.finished` defines them; the task
            # list is bounded, so an old reply is found only here.
            finished = connection.execute(
                """
                SELECT json_extract(runs.request_json, '$.chat_id') AS chat_id,
                       MAX(runs.finished_at) AS finished_at
                FROM graph_runs AS runs
                LEFT JOIN chat_display AS display
                  ON display.project_id = runs.project_id
                 AND display.chat_id = json_extract(runs.request_json, '$.chat_id')
                WHERE runs.project_id = ? AND runs.graph_target_json = ?
                  AND runs.visible = 1
                  AND runs.kind IN ('node_chat', 'project_chat')
                  AND runs.status IN ('succeeded', 'failed', 'interrupted')
                  AND runs.finished_at IS NOT NULL
                  AND json_extract(runs.request_json, '$.chat_id') IS NOT NULL
                  AND display.archived_at IS NULL
                GROUP BY 1
                """,
                (project_id, graph_target.model_dump_json()),
            ).fetchall()
            archived = connection.execute(
                "SELECT chat_id FROM chat_display "
                "WHERE project_id = ? AND archived_at IS NOT NULL ORDER BY chat_id",
                (project_id,),
            ).fetchall()
        return {
            "baseline": baseline["completed_at"],
            "reads": {row["chat_id"]: row["read_through"] for row in rows},
            "latest_finished": {row["chat_id"]: row["finished_at"] for row in finished},
            "archived": [row["chat_id"] for row in archived],
        }

    def mark_chat_read(
        self, project_id: str, chat_id: str, user_id: str, read_through: datetime
    ) -> None:
        """Move the marker forward only, so a late request cannot unread a newer turn.

        Fixed-width UTC text orders like the times it holds, so one conditional
        upsert decides and writes without a read that a second device could race.
        """
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO chat_reads (project_id, chat_id, user_id, read_through) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, chat_id, user_id) "
                "DO UPDATE SET read_through = excluded.read_through "
                "WHERE excluded.read_through > chat_reads.read_through",
                (
                    project_id,
                    chat_id,
                    user_id,
                    read_through.astimezone(UTC).isoformat(timespec="microseconds"),
                ),
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
                "AND title IS NULL AND archived_at IS NULL AND pinned_at IS NULL",
                (project_id, chat_id),
            )
