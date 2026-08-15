"""Auto-research episode storage and one-way legacy Campaign migration."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Literal

from pydantic import TypeAdapter

from rcp.limits import AUTO_RESEARCH_MAIL_MAX_MESSAGES, WATCHER_ERROR_BACKOFF_SECONDS
from rcp.storage.models import (
    ACTIVE_AGENT_TASK_STATUSES,
    AgentCommandInvocationRecord,
    AgentTaskEventRecord,
    AgentTaskRecord,
    AutoResearchActorBinding,
    AutoResearchActorBusy,
    AutoResearchInvocationRecord,
    AutoResearchMessageRecord,
    AutoResearchRecoveryMode,
    AutoResearchRecoveryRecord,
    AutoResearchRecoveryStatus,
    AutoResearchRole,
    AutoResearchStateRecord,
    EpisodeInvocationCeilingReached,
    EpisodeNotRunning,
    EpisodeRecord,
)

_LEGACY_AUTO_RESEARCH_TABLES: tuple[tuple[str, str], ...] = (
    ("campaign_invocations", "_legacy_campaign_invocations_archive"),
    ("campaign_reports", "_legacy_campaign_reports_archive"),
    ("campaign_messages", "_legacy_campaign_messages_archive"),
    ("campaign_recoveries", "_legacy_campaign_recoveries_archive"),
    ("campaigns", "_legacy_campaigns_archive"),
)


class AutoResearchStoreMixin:
    """Auto-research policy state attached to the generic episode ledger."""

    def create_auto_research_episode_with_root_task(
        self,
        episode: EpisodeRecord,
        state: AutoResearchStateRecord,
        task: AgentTaskRecord,
    ) -> tuple[EpisodeRecord, AgentTaskRecord]:
        """Create the Auto child, parent, first allocation, and root task atomically."""

        self._validate_new_episode(episode)
        if episode.mode != "auto_research" or episode.control_node_id is not None:
            raise ValueError("an Auto-research root requires an Auto-research episode")
        if state.episode_id != episode.episode_id:
            raise ValueError("Auto-research state must belong to its episode")
        if task.episode_id != episode.episode_id or task.project_id != episode.project_id:
            raise ValueError("the Auto-research root task must belong to its episode")
        if task.kind != "auto_research" or task.parent_operation_id is not None:
            raise ValueError("the Auto-research root must be a root Auto-research task")
        if task.status != "queued" or not task.visible:
            raise ValueError("the Auto-research root must be a visible queued task")
        if task.authorized_by != episode.authorized_by:
            raise ValueError("Auto-research tasks retain the root human authorizer snapshot")
        if episode.root_operation_id not in {None, task.operation_id}:
            raise ValueError("the episode root operation does not match its task")

        episode = episode.model_copy(
            update={"root_operation_id": task.operation_id, "status": "running"}
        )
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._insert_episode(connection, episode)
                connection.execute(
                    """
                    INSERT INTO auto_research_episodes (
                        episode_id, starting_instruction, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        state.episode_id,
                        state.starting_instruction,
                        state.created_at,
                        state.updated_at,
                    ),
                )
                actor_operation_id, control_node_id = self._bind_auto_research_actor(
                    connection, episode, task, "orchestrator"
                )
                self._insert_agent_task(connection, task)
                connection.execute(
                    """
                    INSERT INTO episode_invocations (
                        episode_id, operation_id, invocation_number, created_at
                    ) VALUES (?, ?, 1, ?)
                    """,
                    (episode.episode_id, task.operation_id, task.created_at),
                )
                self._insert_auto_research_invocation(
                    connection,
                    episode_id=episode.episode_id,
                    operation_id=task.operation_id,
                    allocation_operation_id=task.operation_id,
                    role="orchestrator",
                    actor_operation_id=actor_operation_id,
                    control_node_id=control_node_id,
                    created_at=task.created_at,
                )
                connection.execute(
                    """
                    UPDATE episodes
                    SET invocations_used = 1, updated_at = ?
                    WHERE episode_id = ? AND invocations_used = 0
                    """,
                    (task.created_at, episode.episode_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Only one live Auto-research episode may run per project.") from exc
        stored_episode = self.episode(episode.episode_id)
        stored_task = self.agent_task(task.operation_id)
        assert stored_episode is not None and stored_task is not None
        return stored_episode, stored_task

    def auto_research_state(self, episode_id: str) -> AutoResearchStateRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auto_research_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return self._auto_research_state_record(row) if row is not None else None

    def create_auto_research_agent_task(
        self,
        record: AgentTaskRecord,
        *,
        role: AutoResearchRole,
    ) -> AgentTaskRecord:
        """Spend one operational invocation and admit one Auto-research task."""

        if record.episode_id is None:
            raise ValueError("an Auto-research task must carry its episode id")
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                episode = self._load_auto_research_episode(connection, record.episode_id)
                self._insert_paid_auto_research_task(connection, episode, record, role)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the Auto-research task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_auto_research_recovery_task(self, record: AgentTaskRecord) -> AgentTaskRecord:
        """Create one exact same-allocation recovery without moving the episode meter."""

        if record.episode_id is None or record.parent_operation_id is None:
            raise ValueError("an Auto-research recovery needs its episode and exact parent")
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                episode = self._load_auto_research_episode(connection, record.episode_id)
                parent = connection.execute(
                    """
                    SELECT run.*, invocation.role, invocation.actor_operation_id,
                           invocation.control_node_id, invocation.allocation_operation_id
                    FROM graph_runs AS run
                    JOIN auto_research_invocations AS invocation
                      ON invocation.operation_id = run.operation_id
                    WHERE run.operation_id = ? AND run.episode_id = ?
                    """,
                    (record.parent_operation_id, record.episode_id),
                ).fetchone()
                if parent is None:
                    raise ValueError("Auto-research recovery parent is outside its episode")
                if parent["status"] not in {"paused", "interrupted", "failed"}:
                    raise ValueError("only a paused, interrupted, or failed task can recover")
                if record.project_id != parent["project_id"] or record.kind != parent["kind"]:
                    raise ValueError("Auto-research recovery must preserve its task scope")
                if record.attempt != int(parent["attempt"]) + 1:
                    raise ValueError("Auto-research recovery must advance its attempt lineage")
                if record.authorized_by != episode.authorized_by:
                    raise ValueError(
                        "Auto-research tasks retain the root human authorizer snapshot"
                    )
                role = TypeAdapter(AutoResearchRole).validate_python(parent["role"])
                if episode.status == "stopping" and parent["status"] != "paused":
                    raise EpisodeNotRunning(
                        "a stopping episode only admits recovery of an explicitly paused turn"
                    )
                if episode.status not in {"running", "stopping"}:
                    raise EpisodeNotRunning("the episode cannot recover after its ending is durable")
                if episode.wrapup_state in {"running", "ready", "failed", "skipped"}:
                    raise EpisodeNotRunning("episode report settlement already closed recovery")

                child = connection.execute(
                    """
                    SELECT 1 FROM graph_runs AS child
                    JOIN auto_research_invocations AS invocation
                      ON invocation.operation_id = child.operation_id
                    WHERE child.parent_operation_id = ? AND child.episode_id = ?
                      AND child.attempt = ?
                    LIMIT 1
                    """,
                    (record.parent_operation_id, record.episode_id, record.attempt),
                ).fetchone()
                if child is not None:
                    raise ValueError("Auto-research task already has a recovery child")
                abandoned = connection.execute(
                    """
                    SELECT 1 FROM graph_run_receipts
                    WHERE operation_id = ?
                      AND category = 'auto_research_recovery_abandoned'
                    LIMIT 1
                    """,
                    (record.parent_operation_id,),
                ).fetchone()
                if abandoned is not None:
                    raise ValueError("episode Stop already abandoned recovery of this task")

                clean_orchestrator_retry = (
                    role == "orchestrator"
                    and record.native_session_id is None
                    and record.request.get("session_id") is None
                    and parent["actor_operation_id"] == episode.root_operation_id
                )
                if clean_orchestrator_retry:
                    if (record.stage_host or "") != (parent["stage_host"] or ""):
                        raise ValueError("a clean Retry must preserve its actor-owned stage host")
                    if record.stage_root != parent["stage_root"]:
                        raise ValueError("a clean Retry must preserve its actor-owned stage")
                elif (
                    not parent["native_session_id"]
                    or not parent["stage_root"]
                    or record.native_session_id != parent["native_session_id"]
                    or (record.stage_host or "") != (parent["stage_host"] or "")
                    or record.stage_root != parent["stage_root"]
                    or record.request.get("session_id") != parent["native_session_id"]
                ):
                    raise ValueError(
                        "Auto-research recovery must preserve its exact saved session and stage"
                    )

                actor_operation_id, control_node_id = self._bind_auto_research_actor(
                    connection,
                    episode,
                    record,
                    role,
                    same_allocation_recovery=True,
                )
                self._insert_agent_task(connection, record)
                self._insert_auto_research_invocation(
                    connection,
                    episode_id=episode.episode_id,
                    operation_id=record.operation_id,
                    allocation_operation_id=str(parent["allocation_operation_id"]),
                    role=role,
                    actor_operation_id=actor_operation_id,
                    control_node_id=control_node_id,
                    created_at=record.created_at,
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the Auto-research recovery task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_auto_research_message_wake_task(
        self,
        record: AgentTaskRecord,
        *,
        role: AutoResearchRole,
        recipient_task_id: str,
        message_ids: list[str],
    ) -> AgentTaskRecord | None:
        """Spend one invocation and claim a bounded mail prefix in one transaction."""

        if record.episode_id is None:
            raise ValueError("an Auto-research mail wake must carry its episode id")
        if not recipient_task_id or not message_ids or len(message_ids) != len(set(message_ids)):
            raise ValueError("an Auto-research mail wake needs one recipient and unique messages")
        if len(message_ids) > AUTO_RESEARCH_MAIL_MAX_MESSAGES:
            raise ValueError(
                "an Auto-research mail wake may claim at most "
                f"{AUTO_RESEARCH_MAIL_MAX_MESSAGES} messages"
            )
        placeholders = ",".join("?" for _ in message_ids)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                episode = self._load_auto_research_episode(connection, record.episode_id)
                messages = connection.execute(
                    f"""
                    SELECT message_id, episode_id, recipient_task_id,
                           delivered_at, delivery_operation_id
                    FROM auto_research_messages
                    WHERE message_id IN ({placeholders})
                    """,
                    message_ids,
                ).fetchall()
                if {item["message_id"] for item in messages} != set(message_ids):
                    raise ValueError("Auto-research mail delivery names a missing message")
                if any(
                    item["episode_id"] != record.episode_id
                    or item["recipient_task_id"] != recipient_task_id
                    for item in messages
                ):
                    raise ValueError("Auto-research mail delivery crosses an episode or recipient")
                if any(
                    item["delivered_at"] is not None
                    or item["delivery_operation_id"] is not None
                    for item in messages
                ):
                    return None
                pending_prefix = connection.execute(
                    """
                    SELECT message_id FROM auto_research_messages
                    WHERE episode_id = ? AND recipient_task_id = ?
                      AND delivered_at IS NULL AND delivery_operation_id IS NULL
                    ORDER BY created_at, message_id LIMIT ?
                    """,
                    (record.episode_id, recipient_task_id, len(message_ids)),
                ).fetchall()
                if [item["message_id"] for item in pending_prefix] != message_ids:
                    return None
                self._insert_paid_auto_research_task(connection, episode, record, role)
                connection.execute(
                    f"""
                    UPDATE auto_research_messages
                    SET delivered_at = ?, delivery_operation_id = ?
                    WHERE message_id IN ({placeholders})
                    """,
                    (record.created_at, record.operation_id, *message_ids),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the Auto-research mail wake task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def _insert_paid_auto_research_task(
        self,
        connection: sqlite3.Connection,
        episode: EpisodeRecord,
        record: AgentTaskRecord,
        role: AutoResearchRole,
    ) -> None:
        if episode.status != "running" or episode.ending is not None:
            raise EpisodeNotRunning("the Auto-research episode is not accepting new work")
        if episode.stop_requested_at is not None:
            raise EpisodeNotRunning("the Auto-research episode is stopping")
        if episode.invocations_used >= episode.invocation_ceiling:
            raise EpisodeInvocationCeilingReached(
                "the Auto-research operational invocation ceiling is exhausted"
            )
        if record.episode_id != episode.episode_id or record.project_id != episode.project_id:
            raise ValueError("Auto-research task lineage does not match the episode")
        if record.kind != "auto_research":
            raise ValueError("Auto-research admission requires an Auto-research task")
        if record.authorized_by != episode.authorized_by:
            raise ValueError("Auto-research tasks retain the root human authorizer snapshot")
        actor_operation_id, control_node_id = self._bind_auto_research_actor(
            connection, episode, record, role
        )
        self._insert_agent_task(connection, record)
        invocation_number = episode.invocations_used + 1
        connection.execute(
            """
            INSERT INTO episode_invocations (
                episode_id, operation_id, invocation_number, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (episode.episode_id, record.operation_id, invocation_number, record.created_at),
        )
        self._insert_auto_research_invocation(
            connection,
            episode_id=episode.episode_id,
            operation_id=record.operation_id,
            allocation_operation_id=record.operation_id,
            role=role,
            actor_operation_id=actor_operation_id,
            control_node_id=control_node_id,
            created_at=record.created_at,
        )
        cursor = connection.execute(
            """
            UPDATE episodes
            SET invocations_used = invocations_used + 1, updated_at = ?
            WHERE episode_id = ? AND invocations_used = ? AND status = 'running'
              AND ending IS NULL AND stop_requested_at IS NULL
            """,
            (record.created_at, episode.episode_id, episode.invocations_used),
        )
        if cursor.rowcount != 1:
            raise ValueError("the episode budget changed during task admission")

    def _bind_auto_research_actor(
        self,
        connection: sqlite3.Connection,
        episode: EpisodeRecord,
        record: AgentTaskRecord,
        role: AutoResearchRole,
        *,
        same_allocation_recovery: bool = False,
    ) -> tuple[str, str | None]:
        request = dict(record.request)
        if request.get("episode_id") != episode.episode_id:
            raise ValueError("Auto-research task request must carry its exact episode id")
        if request.get("role") != role:
            raise ValueError("Auto-research task request role does not match its canonical role")
        requested_actor = request.get("actor_operation_id")
        if requested_actor is not None and (
            not isinstance(requested_actor, str) or not requested_actor.strip()
        ):
            raise ValueError("Auto-research actor operation id must be nonblank")
        requested_actor = requested_actor.strip() if isinstance(requested_actor, str) else None
        is_root = (
            record.operation_id == episode.root_operation_id
            and record.parent_operation_id is None
        )
        if is_root:
            if role != "orchestrator" or requested_actor not in {None, record.operation_id}:
                raise ValueError("the Auto-research root is its sole orchestrator actor")
            actor_operation_id = record.operation_id
            control_node_id = None
        else:
            if record.parent_operation_id is None:
                raise ValueError("a non-root Auto-research task must preserve parent lineage")
            parent = connection.execute(
                """
                SELECT run.*, invocation.role, invocation.actor_operation_id,
                       invocation.control_node_id
                FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ? AND run.episode_id = ?
                """,
                (record.parent_operation_id, episode.episode_id),
            ).fetchone()
            if parent is None:
                raise ValueError("an Auto-research continuation has no canonical parent actor")
            parent_role = TypeAdapter(AutoResearchRole).validate_python(parent["role"])
            parent_actor = str(parent["actor_operation_id"])
            if requested_actor is None:
                if role == "orchestrator":
                    actor_operation_id = episode.root_operation_id
                elif parent_role == role:
                    actor_operation_id = parent_actor
                else:
                    actor_operation_id = record.operation_id
            else:
                actor_operation_id = requested_actor
            if actor_operation_id is None:
                raise ValueError("Auto-research actor identity is unavailable")
            if actor_operation_id == record.operation_id:
                if role != "worker" or parent_role != "orchestrator":
                    raise ValueError("only the orchestrator may seat a new worker actor")
                if request.get("wake_cause") is not None:
                    raise ValueError("an Auto-research wake must preserve an existing actor")
                control_node_id = request.get("control_node_id")
            else:
                actor = connection.execute(
                    """
                    SELECT role, control_node_id
                    FROM auto_research_invocations
                    WHERE episode_id = ? AND operation_id = ?
                    """,
                    (episode.episode_id, actor_operation_id),
                ).fetchone()
                if actor is None:
                    raise ValueError("Auto-research continuation names an unknown actor")
                actor_role = TypeAdapter(AutoResearchRole).validate_python(actor["role"])
                if actor_role != role or parent_role != role or parent_actor != actor_operation_id:
                    raise ValueError("Auto-research continuation cannot cross actor lineage")
                control_node_id = actor["control_node_id"]
                if request.get("control_node_id") != control_node_id:
                    raise ValueError("Auto-research continuation cannot change its control seat")
                latest = self._auto_research_actor_latest_row(
                    connection, episode.episode_id, actor_operation_id
                )
                clean_retry = (
                    latest is not None
                    and same_allocation_recovery
                    and role == "orchestrator"
                    and record.native_session_id is None
                    and request.get("session_id") is None
                    and (record.stage_host or "") == (latest["stage_host"] or "")
                    and record.stage_root == latest["stage_root"]
                )
                if (
                    latest is not None
                    and (latest["native_session_id"] is not None or latest["stage_root"] is not None)
                    and not clean_retry
                    and (
                        record.native_session_id != latest["native_session_id"]
                        or (record.stage_host or "") != (latest["stage_host"] or "")
                        or record.stage_root != latest["stage_root"]
                    )
                ):
                    raise ValueError(
                        "Auto-research continuation must preserve its actor session and stage"
                    )

        if role == "orchestrator":
            if actor_operation_id != episode.root_operation_id:
                raise ValueError("Auto-research cannot replace its orchestrator actor")
            if request.get("control_node_id") is not None:
                raise ValueError("the Auto-research orchestrator has no worker control seat")
        elif not isinstance(control_node_id, str) or not control_node_id:
            raise ValueError("an Auto-research worker must retain its control seat")

        request["actor_operation_id"] = actor_operation_id
        record.request = request
        unresolved = connection.execute(
            """
            SELECT run.operation_id
            FROM graph_runs AS run
            JOIN auto_research_invocations AS invocation
              ON invocation.operation_id = run.operation_id
            WHERE invocation.episode_id = ? AND invocation.actor_operation_id = ?
              AND (
                run.status IN ('queued', 'running', 'pausing')
                OR (
                  run.status IN ('paused', 'interrupted', 'failed')
                  AND (? = 0 OR run.operation_id != ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM graph_run_receipts AS receipt
                    WHERE receipt.operation_id = run.operation_id
                      AND receipt.category = 'auto_research_recovery_abandoned'
                  )
                )
              )
              AND NOT EXISTS (
                SELECT 1 FROM graph_runs AS child
                JOIN auto_research_invocations AS child_invocation
                  ON child_invocation.operation_id = child.operation_id
                WHERE child.parent_operation_id = run.operation_id
                  AND child.episode_id = run.episode_id
                  AND child.attempt = run.attempt + 1
                  AND child_invocation.actor_operation_id = invocation.actor_operation_id
              )
            ORDER BY run.rowid DESC LIMIT 1
            """,
            (
                episode.episode_id,
                actor_operation_id,
                int(same_allocation_recovery),
                record.parent_operation_id or "",
            ),
        ).fetchone()
        if unresolved is not None:
            raise AutoResearchActorBusy(actor_operation_id, str(unresolved["operation_id"]))
        return actor_operation_id, control_node_id

    @staticmethod
    def _insert_auto_research_invocation(
        connection: sqlite3.Connection,
        *,
        episode_id: str,
        operation_id: str,
        allocation_operation_id: str,
        role: AutoResearchRole,
        actor_operation_id: str,
        control_node_id: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO auto_research_invocations (
                episode_id, operation_id, allocation_operation_id, role,
                actor_operation_id, control_node_id, handoffs_cleared_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                episode_id,
                operation_id,
                allocation_operation_id,
                role,
                actor_operation_id,
                control_node_id,
                created_at,
            ),
        )

    def _load_auto_research_episode(
        self, connection: sqlite3.Connection, episode_id: str
    ) -> EpisodeRecord:
        row = connection.execute(
            """
            SELECT episode.* FROM episodes AS episode
            JOIN auto_research_episodes AS auto
              ON auto.episode_id = episode.episode_id
            WHERE episode.episode_id = ? AND episode.mode = 'auto_research'
            """,
            (episode_id,),
        ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return self._episode_record(row)

    @staticmethod
    def _auto_research_actor_latest_row(
        connection: sqlite3.Connection,
        episode_id: str,
        actor_operation_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT run.* FROM graph_runs AS run
            JOIN auto_research_invocations AS invocation
              ON invocation.operation_id = run.operation_id
            WHERE invocation.episode_id = ? AND invocation.actor_operation_id = ?
            ORDER BY run.rowid DESC LIMIT 1
            """,
            (episode_id, actor_operation_id),
        ).fetchone()

    def auto_research_invocation(
        self, operation_id: str
    ) -> AutoResearchInvocationRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auto_research_invocations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._auto_research_invocation_record(row) if row is not None else None

    def auto_research_invocation_role(self, operation_id: str) -> AutoResearchRole | None:
        invocation = self.auto_research_invocation(operation_id)
        return invocation.role if invocation is not None else None

    def auto_research_actor_binding(self, operation_id: str) -> AutoResearchActorBinding:
        with self.connection() as connection:
            invocation = connection.execute(
                "SELECT * FROM auto_research_invocations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if invocation is None:
                raise KeyError(operation_id)
            latest = self._auto_research_actor_latest_row(
                connection,
                str(invocation["episode_id"]),
                str(invocation["actor_operation_id"]),
            )
            if latest is None:
                raise RuntimeError("Auto-research actor has no task binding")
        return AutoResearchActorBinding(
            episode_id=str(invocation["episode_id"]),
            actor_operation_id=str(invocation["actor_operation_id"]),
            role=TypeAdapter(AutoResearchRole).validate_python(invocation["role"]),
            control_node_id=invocation["control_node_id"],
            current_operation_id=str(latest["operation_id"]),
            native_session_id=latest["native_session_id"],
            stage_host=latest["stage_host"],
            stage_root=latest["stage_root"],
        )

    def auto_research_tasks(self, episode_id: str) -> list[AgentTaskRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT run.*,
                       EXISTS (
                         SELECT 1 FROM graph_run_receipts AS receipt
                         WHERE receipt.operation_id = run.operation_id
                           AND receipt.category IN (
                             'auto_research_recovery_abandoned',
                             'experiment_recovery_abandoned'
                           )
                       ) AS recovery_abandoned
                FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE invocation.episode_id = ?
                ORDER BY run.created_at, run.operation_id
                """,
                (episode_id,),
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def auto_research_task_history(
        self, episode_id: str, *, limit: int
    ) -> tuple[int, dict[str, int], dict[str, int], list[AgentTaskRecord]]:
        if limit < 1:
            raise ValueError("Auto-research task history limit must be positive")
        with self.connection() as connection:
            status_rows = connection.execute(
                """
                SELECT run.status, COUNT(*) AS count
                FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE invocation.episode_id = ? GROUP BY run.status ORDER BY run.status
                """,
                (episode_id,),
            ).fetchall()
            role_rows = connection.execute(
                """
                SELECT invocation.role, COUNT(*) AS count
                FROM auto_research_invocations AS invocation
                WHERE invocation.episode_id = ? GROUP BY invocation.role ORDER BY invocation.role
                """,
                (episode_id,),
            ).fetchall()
            rows = connection.execute(
                """
                SELECT run.* FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE invocation.episode_id = ?
                ORDER BY run.created_at DESC, run.operation_id DESC LIMIT ?
                """,
                (episode_id, limit),
            ).fetchall()
        status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
        role_counts = {str(row["role"]): int(row["count"]) for row in role_rows}
        total = sum(status_counts.values())
        return total, status_counts, role_counts, [
            self._agent_task_record(row) for row in reversed(rows)
        ]

    def auto_research_event_history(
        self, episode_id: str, *, limit: int
    ) -> tuple[int, list[AgentTaskEventRecord]]:
        if limit < 1:
            raise ValueError("Auto-research event history limit must be positive")
        with self.connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM graph_run_events WHERE episode_id = ?",
                    (episode_id,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT * FROM graph_run_events WHERE episode_id = ?
                ORDER BY event_id DESC LIMIT ?
                """,
                (episode_id, limit),
            ).fetchall()
        return total, [self._agent_task_event_record(row) for row in reversed(rows)]

    def auto_research_handoffs_cleared(self, operation_id: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT invocation.*, run.attempt
                FROM auto_research_invocations AS invocation
                JOIN graph_runs AS run ON run.operation_id = invocation.operation_id
                WHERE invocation.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        self._require_paid_auto_research_allocation(row)
        return row["handoffs_cleared_at"] is not None

    def mark_auto_research_handoffs_cleared(self, operation_id: str) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT invocation.*, run.attempt
                FROM auto_research_invocations AS invocation
                JOIN graph_runs AS run ON run.operation_id = invocation.operation_id
                WHERE invocation.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            self._require_paid_auto_research_allocation(row)
            connection.execute(
                """
                UPDATE auto_research_invocations
                SET handoffs_cleared_at = COALESCE(handoffs_cleared_at, ?)
                WHERE operation_id = ?
                """,
                (now, operation_id),
            )

    @staticmethod
    def _require_paid_auto_research_allocation(row: sqlite3.Row) -> None:
        if row["operation_id"] != row["allocation_operation_id"] or int(row["attempt"]) != 1:
            raise ValueError("handoff clearing requires a paid Auto-research allocation")

    def schedule_auto_research_task_recovery(
        self,
        operation_id: str,
        *,
        failure_kind: str,
        retry_mode: AutoResearchRecoveryMode,
        diagnostic: str,
        max_attempts: int = 3,
    ) -> AutoResearchRecoveryRecord:
        if max_attempts < 1:
            raise ValueError("Auto-research recovery max attempts must be positive")
        detail = " ".join(diagnostic.split())[:2000] or "Auto-research recovery is required."
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invocation = connection.execute(
                """
                SELECT invocation.*, episode.status AS episode_status
                FROM auto_research_invocations AS invocation
                JOIN episodes AS episode ON episode.episode_id = invocation.episode_id
                WHERE invocation.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if invocation is None:
                raise ValueError("Auto-research recovery requires an Auto-research task")
            if invocation["episode_status"] not in {"running", "stopping"}:
                raise EpisodeNotRunning(
                    "the Auto-research episode no longer accepts recovery work"
                )
            recovery_id = f"task:{invocation['allocation_operation_id']}"
            existing = connection.execute(
                "SELECT * FROM auto_research_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if existing is None:
                attempts = 0
                status: AutoResearchRecoveryStatus = (
                    "blocked" if retry_mode == "blocked" else "pending"
                )
                next_attempt_at = (
                    self._auto_research_recovery_next_attempt_at(now, attempts)
                    if status == "pending"
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO auto_research_recoveries (
                        recovery_id, episode_id, operation_id, failure_kind, retry_mode,
                        attempts, max_attempts, status, next_attempt_at, diagnostic,
                        admitted_operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        recovery_id,
                        invocation["episode_id"],
                        operation_id,
                        failure_kind,
                        retry_mode,
                        max_attempts,
                        status,
                        next_attempt_at,
                        detail,
                        now,
                        now,
                    ),
                )
            else:
                attempts = int(existing["attempts"])
                new_failed_attempt = existing["operation_id"] != operation_id
                already_counted = existing["admitted_operation_id"] == operation_id
                if new_failed_attempt and not already_counted:
                    attempts = min(attempts + 1, max_attempts)
                if existing["status"] in {"blocked", "exhausted"}:
                    status = existing["status"]
                elif existing["status"] == "admitted" and not new_failed_attempt:
                    status = "admitted"
                elif retry_mode == "blocked":
                    status = "blocked"
                elif attempts >= max_attempts:
                    status = "exhausted"
                else:
                    status = "pending"
                next_attempt_at = (
                    self._auto_research_recovery_next_attempt_at(now, attempts)
                    if status == "pending"
                    else None
                )
                connection.execute(
                    """
                    UPDATE auto_research_recoveries
                    SET operation_id = ?, failure_kind = ?, retry_mode = ?, attempts = ?,
                        max_attempts = ?, status = ?, next_attempt_at = ?, diagnostic = ?,
                        admitted_operation_id = CASE WHEN ? = 'pending' THEN NULL
                                                     ELSE admitted_operation_id END,
                        updated_at = ?
                    WHERE recovery_id = ?
                    """,
                    (
                        operation_id,
                        failure_kind,
                        retry_mode,
                        attempts,
                        max_attempts,
                        status,
                        next_attempt_at,
                        detail,
                        status,
                        now,
                        recovery_id,
                    ),
                )
            stored = connection.execute(
                "SELECT * FROM auto_research_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        assert stored is not None
        return self._auto_research_recovery_record(stored)

    def due_auto_research_recoveries(
        self, *, as_of: str | None = None
    ) -> list[AutoResearchRecoveryRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT recovery.* FROM auto_research_recoveries AS recovery
                JOIN episodes AS episode ON episode.episode_id = recovery.episode_id
                WHERE recovery.status = 'pending' AND recovery.next_attempt_at <= ?
                  AND episode.status IN ('running', 'stopping')
                ORDER BY recovery.next_attempt_at, recovery.created_at, recovery.recovery_id
                """,
                (as_of or self.now(),),
            ).fetchall()
        return [self._auto_research_recovery_record(row) for row in rows]

    def auto_research_recovery(
        self, recovery_id: str
    ) -> AutoResearchRecoveryRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auto_research_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        return self._auto_research_recovery_record(row) if row is not None else None

    def auto_research_control_recovery(
        self, episode_id: str, operation_id: str
    ) -> AutoResearchRecoveryRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM auto_research_recoveries
                WHERE episode_id = ?
                  AND (operation_id = ? OR admitted_operation_id = ?)
                ORDER BY updated_at DESC, recovery_id DESC LIMIT 1
                """,
                (episode_id, operation_id, operation_id),
            ).fetchone()
        return self._auto_research_recovery_record(row) if row is not None else None

    def auto_research_task_recovery_child(
        self, operation_id: str
    ) -> AgentTaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT child.* FROM graph_runs AS parent
                JOIN graph_runs AS child ON child.parent_operation_id = parent.operation_id
                JOIN auto_research_invocations AS parent_invocation
                  ON parent_invocation.operation_id = parent.operation_id
                JOIN auto_research_invocations AS child_invocation
                  ON child_invocation.operation_id = child.operation_id
                WHERE parent.operation_id = ?
                  AND child.episode_id = parent.episode_id
                  AND child.attempt = parent.attempt + 1
                  AND child_invocation.actor_operation_id = parent_invocation.actor_operation_id
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        return self._agent_task_record(row) if row is not None else None

    def complete_auto_research_recovery(
        self,
        recovery_id: str,
        *,
        admitted_operation_id: str | None = None,
        expected_operation_id: str | None = None,
    ) -> AutoResearchRecoveryRecord:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE auto_research_recoveries
                SET attempts = attempts + 1, status = 'admitted', next_attempt_at = NULL,
                    admitted_operation_id = COALESCE(?, admitted_operation_id), updated_at = ?
                WHERE recovery_id = ? AND status = 'pending' AND attempts < max_attempts
                  AND (? IS NULL OR operation_id = ?)
                """,
                (
                    admitted_operation_id,
                    now,
                    recovery_id,
                    expected_operation_id,
                    expected_operation_id,
                ),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM auto_research_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(recovery_id)
            if updated != 1 and row["status"] != "admitted":
                raise ValueError("Auto-research recovery is no longer pending")
        return self._auto_research_recovery_record(row)

    def defer_auto_research_recovery(
        self, recovery_id: str, *, diagnostic: str
    ) -> AutoResearchRecoveryRecord:
        now = self.now()
        detail = " ".join(diagnostic.split())[:2000] or "Auto-research recovery attempt failed."
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM auto_research_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(recovery_id)
            if row["status"] != "pending":
                return self._auto_research_recovery_record(row)
            attempts = int(row["attempts"]) + 1
            exhausted = attempts >= int(row["max_attempts"])
            next_attempt_at = (
                None
                if exhausted
                else self._auto_research_recovery_next_attempt_at(now, attempts)
            )
            connection.execute(
                """
                UPDATE auto_research_recoveries
                SET attempts = ?, status = ?, next_attempt_at = ?, diagnostic = ?, updated_at = ?
                WHERE recovery_id = ? AND status = 'pending'
                """,
                (
                    attempts,
                    "exhausted" if exhausted else "pending",
                    next_attempt_at,
                    detail,
                    now,
                    recovery_id,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM auto_research_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        assert stored is not None
        return self._auto_research_recovery_record(stored)

    def _auto_research_recovery_next_attempt_at(self, now: str, attempts: int) -> str:
        parsed = self._parse_time(now)
        assert parsed is not None
        delay = WATCHER_ERROR_BACKOFF_SECONDS[
            min(attempts, len(WATCHER_ERROR_BACKOFF_SECONDS) - 1)
        ]
        return (parsed + timedelta(seconds=delay)).isoformat()

    def auto_research_recovery_candidates(self) -> list[AgentTaskRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT run.* FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                JOIN episodes AS episode ON episode.episode_id = invocation.episode_id
                WHERE run.status IN ('failed', 'interrupted')
                  AND invocation.role = 'orchestrator'
                  AND episode.status IN ('running', 'stopping')
                  AND NOT EXISTS (
                    SELECT 1 FROM graph_runs AS child
                    WHERE child.parent_operation_id = run.operation_id
                      AND child.episode_id = run.episode_id
                      AND child.attempt = run.attempt + 1
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM auto_research_recoveries AS recovery
                    WHERE recovery.operation_id = run.operation_id
                  )
                ORDER BY run.created_at, run.operation_id
                """
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def abandon_auto_research_recovery(
        self, operation_id: str, *, diagnostic: str
    ) -> AgentTaskRecord:
        detail = " ".join(diagnostic.split())[:2000]
        if not detail:
            raise ValueError("recovery abandonment requires a diagnostic")
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run.* FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            connection.execute(
                """
                INSERT INTO graph_run_receipts (
                    operation_id, created_at, tier, category, payload_json
                ) SELECT ?, ?, 'diagnostic', 'auto_research_recovery_abandoned', ?
                  WHERE NOT EXISTS (
                    SELECT 1 FROM graph_run_receipts
                    WHERE operation_id = ?
                      AND category = 'auto_research_recovery_abandoned'
                  )
                """,
                (
                    operation_id,
                    now,
                    json.dumps({"diagnostic": detail}, separators=(",", ":")),
                    operation_id,
                ),
            )
            connection.execute(
                """
                UPDATE auto_research_recoveries
                SET status = 'blocked', next_attempt_at = NULL, diagnostic = ?, updated_at = ?
                WHERE operation_id = ? OR admitted_operation_id = ?
                """,
                (detail, now, operation_id, operation_id),
            )
        stored = self.agent_task(operation_id)
        assert stored is not None
        return stored

    def settle_auto_research_watchers(self, episode_id: str) -> int:
        """Retain every current Auto watcher once any parent ending is fenced."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode = self._load_auto_research_episode(connection, episode_id)
            if episode.stop_requested_at is None and episode.ending is None:
                raise EpisodeNotRunning("the episode ending fence must be durable first")
            stopped_at = episode.stop_requested_at or episode.updated_at or now
            if episode.stop_requested_at is not None:
                reason = "Auto-research episode stopped."
            else:
                reason = f"Auto-research episode ended ({episode.ending})."
            return connection.execute(
                """
                UPDATE watchers
                SET status = 'stopped', notified = 1, next_check_at = NULL,
                    stopped_by = COALESCE(stopped_by, 'loop'),
                    stop_reason = COALESCE(stop_reason, ?),
                    stopped_at = COALESCE(stopped_at, ?)
                WHERE episode_id = ? AND origin_task_kind = 'auto_research'
                  AND status IN ('active', 'degraded', 'completed')
                """,
                (reason, stopped_at, episode_id),
            ).rowcount

    def auto_research_is_quiescent(self, episode_id: str) -> bool:
        """Whether all mode tasks are settled enough for the generic ending manager."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT run.operation_id, run.status, invocation.role,
                       EXISTS (
                         SELECT 1 FROM graph_runs AS child
                         WHERE child.parent_operation_id = run.operation_id
                           AND child.episode_id = run.episode_id
                           AND child.attempt = run.attempt + 1
                       ) AS has_recovery_child,
                       EXISTS (
                         SELECT 1 FROM graph_run_receipts AS receipt
                         WHERE receipt.operation_id = run.operation_id
                           AND receipt.category = 'auto_research_recovery_abandoned'
                       ) AS recovery_abandoned,
                       EXISTS (
                         SELECT 1 FROM graph_run_receipts AS receipt
                         WHERE receipt.operation_id = run.operation_id
                           AND receipt.category = 'auto_research_orchestrator_failure'
                       ) AS terminal_failure,
                       (
                         SELECT recovery.status FROM auto_research_recoveries AS recovery
                         WHERE recovery.episode_id = invocation.episode_id
                           AND (recovery.operation_id = run.operation_id
                                OR recovery.admitted_operation_id = run.operation_id)
                         ORDER BY recovery.updated_at DESC LIMIT 1
                       ) AS recovery_status
                FROM graph_runs AS run
                JOIN auto_research_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE invocation.episode_id = ?
                """,
                (episode_id,),
            ).fetchall()
        for row in rows:
            if row["has_recovery_child"] or row["recovery_abandoned"] or row["terminal_failure"]:
                continue
            status = str(row["status"])
            if status in ACTIVE_AGENT_TASK_STATUSES or status == "paused":
                return False
            if (
                status in {"failed", "interrupted"}
                and row["role"] == "orchestrator"
                and row["recovery_status"] not in {"blocked", "exhausted"}
            ):
                return False
        return True

    def record_auto_research_message(
        self, record: AutoResearchMessageRecord
    ) -> AutoResearchMessageRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode = connection.execute(
                """
                SELECT * FROM episodes
                WHERE episode_id = ? AND mode = 'auto_research'
                """,
                (record.episode_id,),
            ).fetchone()
            if episode is None:
                raise KeyError(record.episode_id)
            recipient = connection.execute(
                """
                SELECT 1 FROM auto_research_invocations
                WHERE episode_id = ? AND operation_id = ?
                """,
                (record.episode_id, record.recipient_task_id),
            ).fetchone()
            if recipient is None:
                raise ValueError("Auto-research mail recipient is outside the episode")
            if record.sender_role == "human":
                if episode["status"] != "running" or episode["ending"] is not None:
                    raise EpisodeNotRunning("the episode is not accepting new human mail")
                if record.sender_task_id is not None:
                    raise ValueError("a human Auto-research message cannot claim a task sender")
                if record.authorized_by is None:
                    raise ValueError("a human Auto-research message requires its sender snapshot")
                if record.recipient_task_id != episode["root_operation_id"]:
                    raise ValueError("a human may message only the Auto-research orchestrator")
            else:
                if record.sender_task_id is None:
                    raise ValueError("an agent Auto-research message must name its sender task")
                sender = connection.execute(
                    """
                    SELECT role FROM auto_research_invocations
                    WHERE episode_id = ? AND operation_id = ?
                    """,
                    (record.episode_id, record.sender_task_id),
                ).fetchone()
                if sender is None or sender["role"] != record.sender_role:
                    raise ValueError("Auto-research mail sender role does not match its task")
                if (
                    record.sender_role == "worker"
                    and record.recipient_task_id != episode["root_operation_id"]
                ):
                    raise ValueError("a worker may reply only to the Auto-research orchestrator")
                if record.sender_role == "orchestrator":
                    if (
                        episode["status"] != "running"
                        or episode["ending"] is not None
                        or episode["stop_requested_at"] is not None
                    ):
                        raise EpisodeNotRunning(
                            "the episode is no longer accepting orchestrator mail"
                        )
                    target = connection.execute(
                        """
                        SELECT role FROM auto_research_invocations
                        WHERE episode_id = ? AND operation_id = ?
                        """,
                        (record.episode_id, record.recipient_task_id),
                    ).fetchone()
                    if target is None or target["role"] != "worker":
                        raise ValueError("the orchestrator may address only one of its workers")
            connection.execute(
                """
                INSERT INTO auto_research_messages (
                    message_id, episode_id, sender_role, sender_task_id,
                    authorized_space_id, authorized_user_id, authorized_display_name,
                    recipient_task_id, control_node_id, body, created_at,
                    delivered_at, delivery_operation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_id,
                    record.episode_id,
                    record.sender_role,
                    record.sender_task_id,
                    record.authorized_by.space_id if record.authorized_by is not None else None,
                    record.authorized_by.user_id if record.authorized_by is not None else None,
                    record.authorized_by.display_name if record.authorized_by is not None else None,
                    record.recipient_task_id,
                    record.control_node_id,
                    record.body,
                    record.created_at,
                    record.delivered_at,
                    record.delivery_operation_id,
                ),
            )
        stored = self.auto_research_message(record.message_id)
        assert stored is not None
        return stored

    def auto_research_message(self, message_id: str) -> AutoResearchMessageRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auto_research_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._auto_research_message_record(row) if row is not None else None

    def auto_research_messages(self, episode_id: str) -> list[AutoResearchMessageRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM auto_research_messages WHERE episode_id = ?
                ORDER BY created_at, message_id
                """,
                (episode_id,),
            ).fetchall()
        return [self._auto_research_message_record(row) for row in rows]

    def auto_research_message_history(
        self, episode_id: str, *, limit: int
    ) -> tuple[int, list[AutoResearchMessageRecord]]:
        if limit < 1:
            raise ValueError("Auto-research message history limit must be positive")
        with self.connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM auto_research_messages WHERE episode_id = ?",
                    (episode_id,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT * FROM auto_research_messages WHERE episode_id = ?
                ORDER BY created_at DESC, message_id DESC LIMIT ?
                """,
                (episode_id, limit),
            ).fetchall()
        return total, [self._auto_research_message_record(row) for row in reversed(rows)]

    def pending_auto_research_messages(
        self, episode_id: str, recipient_task_id: str
    ) -> list[AutoResearchMessageRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM auto_research_messages
                WHERE episode_id = ? AND recipient_task_id = ? AND delivered_at IS NULL
                ORDER BY created_at, message_id
                """,
                (episode_id, recipient_task_id),
            ).fetchall()
        return [self._auto_research_message_record(row) for row in rows]

    def mark_auto_research_messages_delivered(
        self, message_ids: list[str], *, operation_id: str
    ) -> None:
        if not message_ids:
            return
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("Auto-research message ids must be unique")
        now = self.now()
        placeholders = ",".join("?" for _ in message_ids)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT message_id FROM auto_research_messages
                WHERE message_id IN ({placeholders}) AND delivered_at IS NULL
                """,
                message_ids,
            ).fetchall()
            if {row["message_id"] for row in rows} != set(message_ids):
                raise ValueError("Auto-research message delivery is stale or already claimed")
            connection.execute(
                f"""
                UPDATE auto_research_messages
                SET delivered_at = ?, delivery_operation_id = ?
                WHERE message_id IN ({placeholders})
                """,
                (now, operation_id, *message_ids),
            )

    def start_agent_command(
        self,
        *,
        operation_id: str,
        command_id: str,
        episode_id: str | None,
        verb: str,
        idempotency_key: str | None,
        payload: dict[str, object],
    ) -> AgentCommandInvocationRecord:
        if not command_id or not verb:
            raise ValueError("command identity and verb must not be blank")
        if idempotency_key is not None and episode_id is None:
            raise ValueError("a mutating command key requires an episode binding")
        now = self.now()
        payload_json = self._bounded_command_payload(payload)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT episode_id FROM graph_runs WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if task is None:
                    raise KeyError(operation_id)
                if task["episode_id"] != episode_id:
                    raise ValueError("command episode binding does not match its task")
                if idempotency_key is not None:
                    existing = self._agent_command_by_key_from_connection(
                        connection, episode_id=episode_id, idempotency_key=idempotency_key
                    )
                    if existing is not None:
                        if existing.verb != verb:
                            raise ValueError("idempotency key was already used for another verb")
                        return existing
                self._insert_agent_command_event(
                    connection,
                    operation_id=operation_id,
                    command_id=command_id,
                    episode_id=episode_id,
                    verb=verb,
                    phase="start",
                    idempotency_key=idempotency_key,
                    payload_json=payload_json,
                    message=f"Agent command {verb} started.",
                    level="info",
                    created_at=now,
                )
        except sqlite3.IntegrityError:
            if episode_id is None or idempotency_key is None:
                raise
            existing = self.agent_command_by_key(episode_id, idempotency_key)
            if existing is None:
                raise
            if existing.verb != verb:
                raise ValueError("idempotency key was already used for another verb") from None
            return existing
        stored = self.agent_command(command_id)
        assert stored is not None
        return stored

    def finish_agent_command(
        self,
        command_id: str,
        *,
        status: Literal["ok", "invalid", "unavailable"],
        payload: dict[str, object],
        message: str,
    ) -> AgentCommandInvocationRecord:
        now = self.now()
        if "status" in payload:
            raise ValueError("command exit payload may not override its status")
        exit_payload: dict[str, object] = {"status": status, **payload}
        payload_json = self._bounded_command_payload(exit_payload)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._agent_command_from_connection(connection, command_id)
            if current is None:
                raise KeyError(command_id)
            if current.exited_at is not None:
                if current.status != status or current.exit_payload != exit_payload:
                    raise ValueError("command exit already recorded with a different result")
                return current
            self._insert_agent_command_event(
                connection,
                operation_id=current.operation_id,
                command_id=command_id,
                episode_id=current.episode_id,
                verb=current.verb,
                phase="exit",
                idempotency_key=current.idempotency_key,
                payload_json=payload_json,
                message=message,
                level="info" if status == "ok" else "warning",
                created_at=now,
            )
        stored = self.agent_command(command_id)
        assert stored is not None
        return stored

    def agent_command(self, command_id: str) -> AgentCommandInvocationRecord | None:
        with self.connection() as connection:
            return self._agent_command_from_connection(connection, command_id)

    def agent_command_by_key(
        self, episode_id: str, idempotency_key: str
    ) -> AgentCommandInvocationRecord | None:
        with self.connection() as connection:
            return self._agent_command_by_key_from_connection(
                connection, episode_id=episode_id, idempotency_key=idempotency_key
            )

    @staticmethod
    def _agent_command_by_key_from_connection(
        connection: sqlite3.Connection,
        *,
        episode_id: str,
        idempotency_key: str,
    ) -> AgentCommandInvocationRecord | None:
        row = connection.execute(
            """
            SELECT command_id FROM graph_run_events
            WHERE event_kind = 'command' AND command_phase = 'start'
              AND episode_id = ? AND idempotency_key = ?
            ORDER BY event_id LIMIT 1
            """,
            (episode_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return AutoResearchStoreMixin._agent_command_from_connection(
            connection, row["command_id"]
        )

    @staticmethod
    def _agent_command_from_connection(
        connection: sqlite3.Connection, command_id: str
    ) -> AgentCommandInvocationRecord | None:
        rows = connection.execute(
            """
            SELECT * FROM graph_run_events
            WHERE event_kind = 'command' AND command_id = ?
            ORDER BY event_id
            """,
            (command_id,),
        ).fetchall()
        if not rows:
            return None
        starts = [row for row in rows if row["command_phase"] == "start"]
        exits = [row for row in rows if row["command_phase"] == "exit"]
        if len(starts) != 1 or len(exits) > 1:
            raise RuntimeError("agent command ledger is inconsistent")
        start = starts[0]
        exit_row = exits[0] if exits else None
        exit_payload = json.loads(exit_row["payload_json"]) if exit_row else None
        status = exit_payload.get("status") if isinstance(exit_payload, dict) else None
        return AgentCommandInvocationRecord(
            command_id=command_id,
            episode_id=start["episode_id"],
            operation_id=start["operation_id"],
            verb=start["command_verb"],
            idempotency_key=start["idempotency_key"],
            started_at=start["created_at"],
            start_payload=json.loads(start["payload_json"]),
            exited_at=exit_row["created_at"] if exit_row else None,
            status=status,
            exit_payload=exit_payload,
        )

    @staticmethod
    def _insert_agent_command_event(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        command_id: str,
        episode_id: str | None,
        verb: str,
        phase: Literal["start", "exit"],
        idempotency_key: str | None,
        payload_json: str,
        message: str,
        level: Literal["info", "warning", "error"],
        created_at: str,
    ) -> None:
        detail = " ".join(message.split())[:2000]
        if not detail:
            raise ValueError("command event message must not be blank")
        connection.execute(
            """
            INSERT INTO graph_run_events (
                operation_id, created_at, level, message, event_kind,
                command_id, episode_id, command_verb, command_phase,
                idempotency_key, payload_json
            ) VALUES (?, ?, ?, ?, 'command', ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                created_at,
                level,
                detail,
                command_id,
                episode_id,
                verb,
                phase,
                idempotency_key,
                payload_json,
            ),
        )

    def request_auto_research_worker_pause(
        self, operation_id: str, episode_id: str
    ) -> AgentTaskRecord:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET status = 'pausing', updated_at = ?, last_activity_at = ?,
                    phase = 'pausing', status_message = 'Pausing at the current checkpoint.'
                WHERE operation_id = ? AND episode_id = ?
                  AND status IN ('queued', 'running')
                  AND EXISTS (
                    SELECT 1 FROM auto_research_invocations AS invocation
                    WHERE invocation.operation_id = graph_runs.operation_id
                      AND invocation.role = 'worker'
                  )
                  AND EXISTS (
                    SELECT 1 FROM episodes AS episode
                    WHERE episode.episode_id = graph_runs.episode_id
                      AND episode.mode = 'auto_research'
                      AND episode.status = 'running'
                      AND episode.ending IS NULL
                      AND episode.stop_requested_at IS NULL
                  )
                """,
                (now, now, operation_id, episode_id),
            )
            if cursor.rowcount == 0:
                raise EpisodeNotRunning(
                    "the episode is no longer accepting worker-control commands"
                )
            self._insert_agent_task_event(
                connection,
                operation_id,
                "Pause requested by the Auto-research orchestrator.",
                level="info",
                created_at=now,
            )
        record = self.agent_task(operation_id)
        assert record is not None
        return record

    @staticmethod
    def _auto_research_state_record(row: sqlite3.Row) -> AutoResearchStateRecord:
        return AutoResearchStateRecord.model_validate(dict(row))

    @staticmethod
    def _auto_research_invocation_record(row: sqlite3.Row) -> AutoResearchInvocationRecord:
        data = dict(row)
        data.pop("handoffs_cleared_at", None)
        return AutoResearchInvocationRecord.model_validate(data)

    @staticmethod
    def _auto_research_recovery_record(row: sqlite3.Row) -> AutoResearchRecoveryRecord:
        return AutoResearchRecoveryRecord.model_validate(dict(row))

    def _auto_research_message_record(self, row: sqlite3.Row) -> AutoResearchMessageRecord:
        data = dict(row)
        data["authorized_by"] = self._authorized_human_snapshot(data)
        data.pop("authorized_space_id", None)
        data.pop("authorized_user_id", None)
        data.pop("authorized_display_name", None)
        return AutoResearchMessageRecord.model_validate(data)


def migrate_legacy_auto_research(connection: sqlite3.Connection) -> None:
    """Copy legacy Campaign mode data once, then move all sources out of live names."""

    if not _table_exists(connection, "campaigns"):
        return
    for live, archive in _LEGACY_AUTO_RESEARCH_TABLES:
        if _table_exists(connection, archive):
            raise RuntimeError(
                f"legacy Auto-research migration found both {live} and its archive {archive}"
            )

    campaigns = connection.execute(
        "SELECT * FROM campaigns ORDER BY created_at, campaign_id"
    ).fetchall()
    for campaign in campaigns:
        episode_id = str(campaign["campaign_id"])
        connection.execute(
            """
            INSERT INTO auto_research_episodes (
                episode_id, starting_instruction, created_at, updated_at
            ) VALUES (?, ?, ?, ?) ON CONFLICT(episode_id) DO NOTHING
            """,
            (
                episode_id,
                campaign["starting_instruction"],
                campaign["created_at"],
                campaign["updated_at"],
            ),
        )
        invocation_rows = (
            connection.execute(
                """
                SELECT invocation.*, run.request_json, run.parent_operation_id, run.attempt
                FROM campaign_invocations AS invocation
                JOIN graph_runs AS run ON run.operation_id = invocation.operation_id
                WHERE invocation.campaign_id = ?
                  AND invocation.role IN ('orchestrator', 'worker')
                ORDER BY invocation.created_at, invocation.rowid
                """,
                (episode_id,),
            ).fetchall()
            if _table_exists(connection, "campaign_invocations")
            else []
        )
        for invocation in invocation_rows:
            request = _json_object(invocation["request_json"])
            actor_operation_id = request.get("actor_operation_id")
            if not isinstance(actor_operation_id, str) or not actor_operation_id:
                actor_operation_id = str(invocation["operation_id"])
            allocation_operation_id = _legacy_allocation_operation_id(
                connection,
                episode_id,
                str(invocation["operation_id"]),
                invocation["parent_operation_id"],
            )
            connection.execute(
                """
                INSERT INTO auto_research_invocations (
                    episode_id, operation_id, allocation_operation_id, role,
                    actor_operation_id, control_node_id, handoffs_cleared_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (
                    episode_id,
                    invocation["operation_id"],
                    allocation_operation_id,
                    invocation["role"],
                    actor_operation_id,
                    request.get("control_node_id"),
                    _legacy_handoffs_cleared_at(connection, invocation["operation_id"]),
                    invocation["created_at"],
                ),
            )

    if _table_exists(connection, "campaign_messages"):
        connection.execute(
            """
            INSERT INTO auto_research_messages (
                message_id, episode_id, sender_role, sender_task_id,
                authorized_space_id, authorized_user_id, authorized_display_name,
                recipient_task_id, control_node_id, body, created_at,
                delivered_at, delivery_operation_id
            )
                SELECT message_id, campaign_id, sender_role, sender_task_id,
                       authorized_space_id, authorized_user_id, authorized_display_name,
                       recipient_task_id, control_node_id, body, created_at,
                       delivered_at, delivery_operation_id
                FROM campaign_messages
                WHERE 1
                ON CONFLICT(message_id) DO NOTHING
            """
        )
    if _table_exists(connection, "campaign_recoveries"):
        recovery_rows = connection.execute(
            """
            SELECT * FROM campaign_recoveries
            WHERE purpose = 'task' AND operation_id IS NOT NULL
            ORDER BY created_at, recovery_id
            """
        ).fetchall()
        for recovery in recovery_rows:
            retry_mode = recovery["retry_mode"]
            if retry_mode not in {"exact", "clean", "blocked"}:
                retry_mode = "blocked"
            connection.execute(
                """
                INSERT INTO auto_research_recoveries (
                    recovery_id, episode_id, operation_id, failure_kind, retry_mode,
                    attempts, max_attempts, status, next_attempt_at, diagnostic,
                    admitted_operation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recovery_id) DO NOTHING
                """,
                (
                    recovery["recovery_id"],
                    recovery["campaign_id"],
                    recovery["operation_id"],
                    recovery["failure_kind"],
                    retry_mode,
                    recovery["attempts"],
                    recovery["max_attempts"],
                    recovery["status"],
                    recovery["next_attempt_at"],
                    recovery["diagnostic"],
                    recovery["admitted_operation_id"],
                    recovery["created_at"],
                    recovery["updated_at"],
                ),
            )

    for index in (
        "campaigns_project",
        "campaigns_one_live_project",
        "campaign_invocations_campaign",
        "campaign_reports_campaign",
        "campaign_messages_campaign",
        "campaign_recoveries_due",
    ):
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    for live, archive in _LEGACY_AUTO_RESEARCH_TABLES:
        if _table_exists(connection, live):
            # A data-only archive deliberately carries no live foreign keys or
            # indexes. It is never consulted by runtime behavior and cannot
            # hold canonical task/project deletion hostage after migration.
            connection.execute(f"CREATE TABLE {archive} AS SELECT * FROM {live}")
            connection.execute(f"DROP TABLE {live}")


def _legacy_allocation_operation_id(
    connection: sqlite3.Connection,
    episode_id: str,
    operation_id: str,
    parent_operation_id: object,
) -> str:
    candidate = operation_id
    parent = parent_operation_id if isinstance(parent_operation_id, str) else None
    while True:
        allocated = connection.execute(
            """
            SELECT 1 FROM episode_invocations
            WHERE episode_id = ? AND operation_id = ?
            """,
            (episode_id, candidate),
        ).fetchone()
        if allocated is not None:
            return candidate
        if not parent:
            raise RuntimeError(
                f"legacy Auto-research task {operation_id} has no metered allocation"
            )
        row = connection.execute(
            """
            SELECT operation_id, parent_operation_id FROM graph_runs
            WHERE operation_id = ? AND episode_id = ?
            """,
            (parent, episode_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"legacy Auto-research task {operation_id} has broken recovery lineage"
            )
        candidate = str(row["operation_id"])
        parent = row["parent_operation_id"]


def _legacy_handoffs_cleared_at(
    connection: sqlite3.Connection, operation_id: object
) -> object:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(graph_runs)")}
    if "campaign_worker_handoffs_cleared_at" not in columns:
        return None
    row = connection.execute(
        """
        SELECT campaign_worker_handoffs_cleared_at FROM graph_runs
        WHERE operation_id = ?
        """,
        (operation_id,),
    ).fetchone()
    return row[0] if row is not None else None


def _json_object(value: object) -> dict[str, object]:
    try:
        data = json.loads(value) if isinstance(value, str) else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("legacy Auto-research task request is invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("legacy Auto-research task request is not an object")
    return data


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


__all__ = ["AutoResearchStoreMixin", "migrate_legacy_auto_research"]
