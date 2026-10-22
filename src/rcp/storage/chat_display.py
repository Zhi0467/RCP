"""Display choices for conversations: archived or renamed per project, pinned per user.

Names persist independently of archive and pin choices.
Pins and read markers are per user; a marker records the newest turn end seen.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphTargetRef
from rcp.providers.browser_grant import BrowserOwnerKey, BrowserTurnStatus
from rcp.storage.chat_forks import ChatForkRecord
from rcp.storage.chat_names import ChatNameError, initialize_chat_name, normalize_chat_name
from rcp.storage.mixin_base import StoreMixinBase

if TYPE_CHECKING:
    from rcp.storage import AppStore


class ChatDisplayStoreMixin(StoreMixinBase):
    def chat_inventory(self, project_id: str, chat_id: str | None = None) -> list[dict[str, Any]]:
        """Project-wide newest chat tasks, with durable episode ownership and no recency cap."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                WITH chats AS (
                    SELECT project_id, kind, request_json, graph_target_json, updated_at,
                           json_extract(request_json, '$.chat_id') AS chat_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY json_extract(request_json, '$.chat_id')
                               ORDER BY created_at DESC, operation_id DESC
                           ) AS position
                    FROM graph_runs
                    WHERE project_id = ? AND visible = 1
                      AND kind IN ('node_chat', 'project_chat')
                      AND json_extract(request_json, '$.chat_id') IS NOT NULL
                      AND (? IS NULL OR json_extract(request_json, '$.chat_id') = ?)
                ),
                episode_chats AS (
                    SELECT DISTINCT json_extract(run.request_json, '$.chat_id') AS chat_id,
                           run.graph_target_json
                    FROM graph_runs AS run
                    JOIN episodes AS episode ON episode.episode_id = run.episode_id
                     AND episode.project_id = run.project_id
                    WHERE run.project_id = ? AND episode.mode = 'experiment_loop'
                ),
                edits AS (
                    SELECT json_extract(request_json, '$.chat_id') AS chat_id,
                           MAX(updated_at) AS updated_at
                    FROM graph_runs
                    WHERE project_id = ? AND visible = 1 AND kind = 'artifact_edit'
                      AND json_extract(request_json, '$.chat_id') IS NOT NULL
                      AND (? IS NULL OR json_extract(request_json, '$.chat_id') = ?)
                    GROUP BY 1
                )
                -- An artifact edit only refreshes a conversation its turns already hold.
                SELECT display.title, chats.chat_id, chats.kind, chats.request_json, chats.graph_target_json,
                       MAX(chats.updated_at, COALESCE(edits.updated_at, '')) AS updated_at,
                       episode_chats.chat_id IS NOT NULL AS is_episode,
                       COALESCE(worker.episode_id, child.auto_research_episode_id)
                           AS orchestrator_episode_id
                FROM chats
                JOIN chat_display AS display ON display.project_id = chats.project_id
                  AND display.chat_id = chats.chat_id
                LEFT JOIN episode_chats
                  ON episode_chats.chat_id = chats.chat_id
                 AND episode_chats.graph_target_json = chats.graph_target_json
                LEFT JOIN edits ON edits.chat_id = chats.chat_id
                LEFT JOIN auto_research_child_work AS worker
                  ON worker.worker_id = chats.chat_id AND worker.project_id = chats.project_id
                LEFT JOIN auto_research_child_experiments AS child
                  ON child.child_episode_id = chats.chat_id AND child.project_id = chats.project_id
                WHERE chats.position = 1
                """,
                (project_id, chat_id, chat_id, project_id, project_id, chat_id, chat_id),
            ).fetchall()
            orchestrators = {
                summary["chat_id"]: summary
                for summary in self._orchestrator_chat_summaries(connection, project_id, chat_id)
            }
            summaries = []
            for row in rows:
                if (orchestrator_card := orchestrators.get(row["chat_id"])) is not None:
                    # A human turn after the chain ends continues the orchestrator's one card.
                    orchestrator_card["updated_at"] = max(
                        orchestrator_card["updated_at"], row["updated_at"]
                    )
                    continue
                request = json.loads(row["request_json"])
                target = json.loads(row["graph_target_json"])
                orchestrator = row["orchestrator_episode_id"]
                summaries.append(
                    {
                        "chat_id": row["chat_id"],
                        "graph_target": target,
                        "graph_title": "Main"
                        if target["kind"] == "main"
                        else target["branch_id"][:8],
                        "kind": row["kind"],
                        "node_id": request.get("node_id") or request.get("control_node_id"),
                        "title": row["title"],
                        "updated_at": row["updated_at"],
                        "message_count": 0,
                        "last_message_preview": "",
                        "conversation_kind": (
                            "auto_research_child"
                            if orchestrator
                            else "episode"
                            if row["is_episode"]
                            else "chat"
                        ),
                        "orchestrator_episode_id": orchestrator,
                        "human_turn_refusal": cast("AppStore", self)._child_human_turn_refusal(
                            connection, project_id, row["chat_id"]
                        ),
                    }
                )
            summaries.extend(orchestrators.values())
            by_id = {summary["chat_id"]: summary for summary in summaries}
            for row in connection.execute(
                "SELECT fork.record_json, display.title, source.title AS source_title "
                "FROM chat_forks AS fork "
                "JOIN chat_display AS display USING (project_id, chat_id) "
                "LEFT JOIN chat_display AS source ON source.project_id = fork.project_id "
                "AND source.chat_id = json_extract(fork.record_json, '$.origin.source_chat_id') "
                "WHERE fork.project_id = ? AND (? IS NULL OR fork.chat_id = ?)",
                (project_id, chat_id, chat_id),
            ).fetchall():
                fork = ChatForkRecord.model_validate_json(row["record_json"])
                # The origin link names the source as it is called now; the title
                # captured at fork time stands in only once the source is gone.
                origin = fork.origin.model_dump(mode="json")
                if row["source_title"]:
                    origin["source_title"] = row["source_title"]
                if fork.chat_id in by_id:
                    by_id[fork.chat_id]["origin"] = origin
                else:
                    summaries.append(
                        {
                            "chat_id": fork.chat_id,
                            "graph_target": fork.graph_target.model_dump(),
                            "kind": fork.kind,
                            "node_id": fork.node_id,
                            "title": row["title"],
                            "updated_at": fork.created_at,
                            "message_count": 0,
                            "last_message_preview": "",
                            "conversation_kind": "chat",
                            "orchestrator_episode_id": None,
                            "human_turn_refusal": None,
                            "origin": origin,
                        }
                    )
        return summaries

    def _orchestrator_chat_summaries(
        self, connection: Any, project_id: str, chat_id: str | None
    ) -> list[dict[str, Any]]:
        """One conversation per Auto-research chain, with its stored name."""
        roots = connection.execute(
            "SELECT e.episode_id, e.graph_target_json, display.title FROM episodes e "
            "JOIN chat_display display ON display.project_id=e.project_id "
            "AND display.chat_id=e.episode_id "
            "JOIN graph_branches b ON b.project_id=e.project_id AND b.owner_episode_id=e.episode_id "
            "AND b.branch_id=json_extract(e.graph_target_json, '$.branch_id') "
            "WHERE e.project_id = ? AND e.mode = 'auto_research' "
            "AND (? IS NULL OR e.episode_id = ?)",
            (project_id, chat_id, chat_id),
        ).fetchall()
        summaries = []
        for root in roots:
            updated_at, messages = self._orchestrator_chat_turns(
                connection, project_id, root["episode_id"]
            )
            if updated_at is None:
                continue
            target = json.loads(root["graph_target_json"])
            summaries.append(
                {
                    "chat_id": root["episode_id"],
                    "graph_target": target,
                    "graph_title": target["branch_id"][:8],
                    "kind": "project_chat",
                    "node_id": None,
                    "title": root["title"],
                    "updated_at": updated_at,
                    "message_count": len(messages),
                    "last_message_preview": "",
                    "conversation_kind": "auto_research",
                    "orchestrator_episode_id": root["episode_id"],
                    "human_turn_refusal": cast("AppStore", self)._child_human_turn_refusal(
                        connection, project_id, root["episode_id"]
                    ),
                }
            )
        return summaries

    def orchestrator_chat_messages(
        self, project_id: str, root_episode_id: str
    ) -> list[dict[str, Any]]:
        """The chain's orchestrator turns as chat messages; empty when it is no chain root."""
        with self.connection() as connection:
            return self._orchestrator_chat_turns(connection, project_id, root_episode_id)[1]

    def _orchestrator_chat_turns(
        self, connection: Any, project_id: str, root_episode_id: str
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """The chain's newest turn update, and each orchestrator turn's human input and answers.

        Every chain member writes the root's branch and shares its conversation.
        An artifact edit replying to the chain is one of its turns. The human input
        is the starting instruction or the human mail the turn delivered; a wake with
        neither has no human line.
        """
        runs = connection.execute(
            "SELECT run.operation_id, run.request_json, run.result_json, run.created_at, "
            "run.updated_at, run.finished_at FROM episodes AS root "
            "JOIN graph_branches b ON b.project_id=root.project_id AND b.owner_episode_id=root.episode_id "
            "AND b.branch_id=json_extract(root.graph_target_json, '$.branch_id') "
            "JOIN graph_runs AS run ON run.project_id = root.project_id "
            "AND run.graph_target_json = root.graph_target_json "
            "WHERE root.project_id = ? AND root.episode_id = ? AND root.mode = 'auto_research' "
            "AND run.visible = 1 AND ((run.kind = 'auto_research' "
            "AND json_extract(run.request_json, '$.role') = 'orchestrator') "
            "OR (run.kind = 'artifact_edit' "
            "AND json_extract(run.request_json, '$.artifact_edit.reply_episode_id') IS NOT NULL)) "
            "ORDER BY run.created_at, run.operation_id",
            (project_id, root_episode_id),
        ).fetchall()
        mail: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT mail.delivery_operation_id, mail.body FROM auto_research_messages AS mail "
            "JOIN episodes AS member ON member.episode_id = mail.episode_id "
            "JOIN episodes AS root ON root.project_id = member.project_id "
            "AND root.graph_target_json = member.graph_target_json "
            "WHERE root.project_id = ? AND root.episode_id = ? AND mail.sender_role = 'human' "
            "AND mail.delivery_operation_id IS NOT NULL ORDER BY mail.created_at, mail.message_id",
            (project_id, root_episode_id),
        ):
            mail.setdefault(row["delivery_operation_id"], []).append(row["body"])
        messages: list[dict[str, Any]] = []
        for run in runs:
            request = json.loads(run["request_json"])
            result = json.loads(run["result_json"]) if run["result_json"] else {}
            human = [request["instruction"]] if request.get("instruction") else []
            human += mail.get(run["operation_id"], [])
            answers = [
                text
                for text in (result.get("messages") or [])
                if isinstance(text, str) and text.strip()
            ]
            common = {
                "operation_id": run["operation_id"],
                "provider": request.get("provider"),
                "model": request.get("model"),
                "reasoning": request.get("reasoning"),
                "execution_machine": request.get("run_on"),
                "trigger": "human" if human else "orchestrator",
            }
            lines = [("user", "\n\n".join(human), run["created_at"])] if human else []
            lines += [
                ("assistant", text, run["finished_at"] or run["updated_at"]) for text in answers
            ]
            messages += [
                {
                    **common,
                    "message_id": f"{run['operation_id']}:{index}",
                    "role": role,
                    "text": text,
                    "timestamp": timestamp,
                }
                for index, (role, text, timestamp) in enumerate(lines)
            ]
        return max((run["updated_at"] for run in runs), default=None), messages

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

    def chat_archived(self, project_id: str, chat_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM chat_display WHERE project_id = ? AND chat_id = ? "
                "AND archived_at IS NOT NULL",
                (project_id, chat_id),
            ).fetchone()
        return row is not None

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
        """Replace the canonical name and retain human ownership of it."""
        title = normalize_chat_name(title)
        self._set_chat_display(
            project_id,
            chat_id,
            "title = ?, titled_user_id = ?",
            (title, user_id),
        )

    def chat_name(self, project_id: str, chat_id: str, message: str) -> str:
        """Read the canonical name, seeding transcript-only conversations once."""
        with self.connection() as connection:
            query = "SELECT title FROM chat_display WHERE project_id = ? AND chat_id = ?"
            row = connection.execute(query, (project_id, chat_id)).fetchone()
            if row is None or row["title"] is None:
                initialize_chat_name(connection, project_id, chat_id, message)
                row = connection.execute(query, (project_id, chat_id)).fetchone()
        return row["title"]

    def agent_name_state(self, project_id: str, chat_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT title, titled_user_id, self_named FROM chat_display "
                "WHERE project_id = ? AND chat_id = ?",
                (project_id, chat_id),
            ).fetchone()
        if row is None or row["title"] is None:
            raise ChatNameError("chat_name_not_found")
        return {
            "name": row["title"],
            "can_self_name": row["titled_user_id"] is None and not row["self_named"],
        }

    def self_name_chat_for_task(self, operation_id: str, name: str) -> dict[str, Any]:
        from rcp.bulletin import bulletin_actor_id

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if task is None or task["status"] != "running":
                raise ChatNameError("chat_name_forbidden")
            actor = bulletin_actor_id(connection, task)
            if actor is None:
                raise ChatNameError("chat_name_forbidden")
            return self._self_name_chat(
                connection, task["project_id"], actor.split(":", 1)[1], name
            )

    def _self_name_chat(
        self, connection: Any, project_id: str, chat_id: str, name: str
    ) -> dict[str, Any]:
        name = normalize_chat_name(name)
        result = connection.execute(
            "UPDATE chat_display SET title = ?, self_named = 1 "
            "WHERE project_id = ? AND chat_id = ? AND title IS NOT NULL "
            "AND titled_user_id IS NULL AND self_named = 0",
            (name, project_id, chat_id),
        )
        if result.rowcount != 1:
            raise ChatNameError("chat_name_not_available")
        return {"name": name, "can_self_name": False}

    def chat_reads(
        self, project_id: str, user_id: str, graph_target: GraphTargetRef | None
    ) -> dict[str, Any]:
        """Read markers and finishes for one target, or the whole project when target is None."""
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
            # An orchestrator turn belongs to its chain's conversation, which
            # its branch identifies.
            finished = connection.execute(
                """
                SELECT turns.chat_id, MAX(turns.finished_at) AS finished_at
                FROM (
                    SELECT json_extract(request_json, '$.chat_id') AS chat_id, finished_at
                    FROM graph_runs
                    WHERE project_id = :project AND kind IN ('node_chat', 'project_chat')
                      AND (:target IS NULL OR graph_target_json = :target)
                      AND visible = 1 AND finished_at IS NOT NULL
                      AND status IN ('succeeded', 'failed', 'interrupted')
                    UNION ALL
                    SELECT json_extract(graph_target_json, '$.branch_id'), finished_at
                    FROM graph_runs
                    WHERE project_id = :project AND kind = 'auto_research'
                      AND json_extract(request_json, '$.role') = 'orchestrator'
                      AND (:target IS NULL OR graph_target_json = :target)
                      AND visible = 1 AND finished_at IS NOT NULL
                      AND status IN ('succeeded', 'failed', 'interrupted')
                ) AS turns
                LEFT JOIN chat_display AS display
                  ON display.project_id = :project AND display.chat_id = turns.chat_id
                WHERE turns.chat_id IS NOT NULL AND display.archived_at IS NULL
                GROUP BY 1
                """,
                {
                    "project": project_id,
                    "target": graph_target_json(graph_target) if graph_target else None,
                },
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
