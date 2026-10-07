"""Display choices for conversations: archived or renamed per project, pinned per user.

No choice touches a transcript or task; a row goes away once it holds none.
Pins and read markers are per user; a marker records the newest turn end seen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphTargetRef
from rcp.providers.browser_grant import BrowserOwnerKey, BrowserTurnStatus
from rcp.storage.mixin_base import StoreMixinBase


class ChatDisplayStoreMixin(StoreMixinBase):
    def chat_browser_requested(self, project_id: str, chat_id: str) -> bool:
        with self.connection() as connection:
            return self._chat_browser_requested(connection, project_id, chat_id)

    def _chat_browser_requested(self, connection: Any, project_id: str, chat_id: str) -> bool:
        row = connection.execute(
            "SELECT browser_requested FROM chat_browser_preferences "
            "WHERE project_id = ? AND chat_id = ?",
            (project_id, chat_id),
        ).fetchone()
        return bool(row["browser_requested"]) if row is not None else False

    def set_chat_browser_requested(
        self, project_id: str, chat_id: str, *, browser_requested: bool
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO chat_browser_preferences (project_id, chat_id, browser_requested) "
                "VALUES (?, ?, ?) ON CONFLICT(project_id, chat_id) DO UPDATE "
                "SET browser_requested = excluded.browser_requested",
                (project_id, chat_id, browser_requested),
            )

            if browser_requested:
                # Preference-off cleanup is reversible until it reaches the host.
                # Archive and deleted-project cleanup retain their authority.
                connection.execute(
                    "UPDATE browser_owners SET close_requested = 0, delete_profile = 0 "
                    "WHERE project_id = ? AND chat_id = ? AND project_deletion_requested = 0 "
                    "AND EXISTS (SELECT 1 FROM projects WHERE project_id = ?) "
                    "AND NOT EXISTS (SELECT 1 FROM chat_display "
                    "WHERE project_id = ? AND chat_id = ? AND archived_at IS NOT NULL)",
                    (project_id, chat_id, project_id, project_id, chat_id),
                )

    def record_browser_owner(
        self,
        owner: BrowserOwnerKey,
        *,
        execution_host: str | None,
        workspace_dir: str,
        stage_root: str,
        chat_id: str | None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO browser_owners "
                "(owner_token, owner_json, execution_host, workspace_dir, stage_root, chat_id, project_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_token) DO UPDATE SET "
                "execution_host = excluded.execution_host, "
                "workspace_dir = excluded.workspace_dir, stage_root = excluded.stage_root, "
                "chat_id = excluded.chat_id",
                (
                    owner.token(),
                    owner.model_dump_json(),
                    execution_host or "",
                    workspace_dir,
                    stage_root,
                    chat_id,
                    owner.project_id,
                ),
            )

    def browser_owners(self, project_id: str, chat_id: str | None = None) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_owners WHERE project_id = ?"
                + (" AND chat_id = ?" if chat_id is not None else ""),
                (project_id, chat_id) if chat_id is not None else (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_browser_turn_status(self, operation_id: str, status: BrowserTurnStatus) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO browser_turn_status (operation_id, status_json) VALUES (?, ?) "
                "ON CONFLICT(operation_id) DO UPDATE SET status_json = excluded.status_json",
                (operation_id, status.model_dump_json()),
            )

    def browser_turn_status(self, operation_id: str) -> BrowserTurnStatus:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status_json FROM browser_turn_status WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return (
            BrowserTurnStatus.model_validate_json(row["status_json"])
            if row is not None
            else BrowserTurnStatus(status="not_requested")
        )

    def chat_display(self, project_id: str, user_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT chat_id, title, archived_at FROM chat_display "
                "WHERE project_id = ? ORDER BY chat_id",
                (project_id,),
            ).fetchall()
            pinned = connection.execute(
                "SELECT chat_id FROM chat_pins WHERE project_id = ? AND user_id = ? "
                "ORDER BY pinned_at DESC, chat_id",
                (project_id, user_id),
            ).fetchall()
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
        with self.connection() as connection:
            if pinned:
                connection.execute(
                    "INSERT INTO chat_pins (project_id, chat_id, user_id, pinned_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, chat_id, user_id) DO NOTHING",
                    (project_id, chat_id, user_id, self.now()),
                )
            else:
                connection.execute(
                    "DELETE FROM chat_pins WHERE project_id = ? AND chat_id = ? AND user_id = ?",
                    (project_id, chat_id, user_id),
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
                (project_id, graph_target_json(graph_target)),
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

    def chat_read_markers(self, project_id: str, user_id: str) -> dict[str, Any]:
        """Only the member's markers and baseline, without `chat_reads`' turn scan."""
        with self.connection() as connection:
            baseline = connection.execute(
                "SELECT completed_at FROM storage_schema_migrations "
                "WHERE migration_name = 'chat_reads_and_pins_v1'"
            ).fetchone()
            rows = connection.execute(
                "SELECT chat_id, read_through FROM chat_reads WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            ).fetchall()
        return {
            "baseline": baseline["completed_at"] if baseline else None,
            "reads": {row["chat_id"]: row["read_through"] for row in rows},
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
                "AND title IS NULL AND archived_at IS NULL",
                (project_id, chat_id),
            )
