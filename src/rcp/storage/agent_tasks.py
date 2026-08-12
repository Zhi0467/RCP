from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from statistics import median
from typing import Literal

from pydantic import (
    TypeAdapter,
)

from rcp.artifacts import AgentArtifactDescriptor
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    require_dispatch,
)
from rcp.core.models import (
    AuthorizedHuman,
)
from rcp.limits import (
    AGENT_COMMAND_EVENT_MAX_BYTES,
    AGENT_TASK_ESTIMATE_HISTORY_LIMIT,
    AGENT_TASK_ESTIMATE_SAMPLE_LIMIT,
    AGENT_TASK_EVENT_LIST_DEFAULT_LIMIT,
    AGENT_TASK_EVENT_LIST_MAX_LIMIT,
    AGENT_TASK_EVENT_RETENTION_COUNT,
    AGENT_TASK_LIST_DEFAULT_LIMIT,
    AGENT_TASK_LIST_MAX_LIMIT,
    AGENT_TASK_RECEIPT_LIST_LIMIT,
    AGENT_TASK_RECEIPT_MAX_BYTES,
    AGENT_TASK_RECEIPT_RETENTION_COUNTS,
    AGENT_TASK_RESULT_MAX_BYTES,
    CHAT_ARTIFACT_MAX_COUNT,
    PATCH_OUTPUT_RETENTION_DAYS,
    RUN_TRACE_RETENTION_DAYS,
    WRITING_SESSION_RETENTION_DAYS,
    WRITING_SESSIONS_PER_PROJECT,
)
from rcp.providers import ProviderUsage
from rcp.storage.models import (  # noqa: F401
    _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE,
    _EXPERIMENT_EPISODE_PINNED_FIELDS,
    _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC,
    _PROJECT_ID_TABLES,
    ACTIVE_AGENT_TASK_STATUSES,
    SPACE_NAME_MAX_LENGTH,
    AgentCommandInvocationRecord,
    AgentTaskContractRecord,
    AgentTaskEventRecord,
    AgentTaskKind,
    AgentTaskReceiptRecord,
    AgentTaskReceiptTier,
    AgentTaskRecord,
    AgentTaskStatus,
    AgentUsageCell,
    AgentUsageCountReason,
    AgentUsageMetric,
    AgentUsageRecord,
    AgentUsageSnapshot,
    CampaignActorBinding,
    CampaignActorBusy,
    CampaignBudgetExhausted,
    CampaignBudgetMeter,
    CampaignEnding,
    CampaignInvocationRole,
    CampaignMessageRecord,
    CampaignMessageRole,
    CampaignNotRunning,
    CampaignRecord,
    CampaignRecoveryMode,
    CampaignRecoveryPurpose,
    CampaignRecoveryRecord,
    CampaignRecoveryStatus,
    CampaignReportRecord,
    CampaignStatus,
    ChatSessionContextRecord,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    ExperimentWatcherResourceRecord,
    GraphCondition,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    ProjectRecord,
    ProjectStageRecord,
    ProposalResolvedGraphCondition,
    ProviderSkillInventoryRecord,
    ResultViewConflict,
    ResultViewRecord,
    SpaceKind,
    SpaceUserKind,
    SpaceUserRecord,
    StoredWatcherRecord,
    TeamAuthenticationError,
    TeamInvitationRecord,
    WatcherClaimConflict,
    WatcherContinuation,
    WatcherDeliveryRecord,
    WatcherRecord,
    WatcherStatus,
    WatcherStopRequest,
    _canonical_space_id,
    _canonical_uuid4,
    _discard_failed_team_initialization,
    _experiment_pinned_value,
    _new_enrollment_code,
    _new_member_token,
    _new_session_token,
    _optional_str,
    _parse_enrollment_code,
    _plain_html_name,
    _required_timestamp,
    _result_view_html_bytes,
    _result_view_is_visible,
    _result_view_reference_time,
    _sha256,
    _stored_space_kind,
    _validated_result_view_html,
    normalize_space_name,
    watcher_next_check_at,
)


class AgentTaskStoreMixin:
    """Agent task lifecycle, chat sessions, usage, receipts, and pruning."""

    def agent_task_profile(self, operation_id: str) -> Literal["ordinary", "orchestrator"]:
        """Resolve the one semantic profile canonically bound to a task."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT run.operation_id, invocation.role
                FROM graph_runs AS run
                LEFT JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return "orchestrator" if row["role"] == "orchestrator" else "ordinary"

    def create_agent_task(self, record: AgentTaskRecord) -> AgentTaskRecord:
        if record.campaign_id is not None:
            raise ValueError("campaign tasks must spend from their campaign pot atomically")
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._has_active_chat_overlap(connection, record):
                    raise ValueError("Another task is already active in this conversation.")
                self._insert_agent_task(connection, record)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the agent task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def _insert_agent_task(
        self,
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        self._validate_dispatch_authority_insert(connection, record)
        self._bind_chat_stage(connection, record)
        self._validate_experiment_task_insert(connection, record)
        connection.execute(
            """
            INSERT INTO graph_runs (
                operation_id, project_id, campaign_id, kind, status, request_json,
                created_at, updated_at, started_at, finished_at,
                status_message, error, applied_revision, result_json, attempt,
                parent_operation_id, native_session_id, stage_host,
                stage_root, estimate_seconds, estimate_samples, phase,
                last_activity_at, dispatch_authority_json, authorized_space_id,
                authorized_user_id, authorized_display_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.operation_id,
                record.project_id,
                record.campaign_id,
                record.kind,
                record.status,
                json.dumps(record.request, separators=(",", ":")),
                record.created_at,
                record.updated_at,
                record.started_at,
                record.finished_at,
                record.status_message,
                record.error,
                record.applied_revision,
                self._bounded_result_json(record.result),
                record.attempt,
                record.parent_operation_id,
                record.native_session_id,
                record.stage_host,
                record.stage_root,
                record.estimate_seconds,
                record.estimate_samples,
                record.phase,
                record.last_activity_at,
                (
                    record.dispatch_authority.model_dump_json()
                    if record.dispatch_authority is not None
                    else None
                ),
                record.authorized_by.space_id if record.authorized_by is not None else None,
                record.authorized_by.user_id if record.authorized_by is not None else None,
                record.authorized_by.display_name if record.authorized_by is not None else None,
            ),
        )

    @staticmethod
    def _validate_dispatch_authority_insert(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        """Keep a recovery or continuation on its parent's admitted authority."""

        if record.kind == "campaign":
            if record.campaign_id is None:
                raise ValueError("A campaign task requires its exact campaign identity.")
            request = record.request
            role = TypeAdapter(CampaignInvocationRole).validate_python(request.get("role"))
            raw_actor = request.get("actor_operation_id")
            if not isinstance(raw_actor, str) or not raw_actor.strip():
                raise ValueError("A campaign task requires its canonical actor identity.")
            actor_operation_id = raw_actor.strip()
            is_root = record.parent_operation_id is None

            if role == "report":
                if is_root:
                    raise ValueError("A campaign report cannot be the campaign root actor.")
                if record.dispatch_authority is not None:
                    raise ValueError("A campaign report cannot carry graph dispatch authority.")
            else:
                expected = AgentDispatchAuthority(
                    profile="orchestrator" if role == "orchestrator" else "ordinary",
                    task_contract="orchestrate" if role == "orchestrator" else "work_auto",
                    scope=AgentDispatchScope(
                        run_truth_scope=sorted(set(request.get("run_truth_scope") or ())),
                        campaign_id=record.campaign_id,
                        patch_kind="work",
                    ),
                )
                require_dispatch(expected)
                if record.dispatch_authority != expected:
                    raise ValueError(
                        "A campaign task must carry its exact server-owned dispatch authority."
                    )

            if is_root:
                if role != "orchestrator" or actor_operation_id != record.operation_id:
                    raise ValueError(
                        "A campaign root must be its sole canonical orchestrator actor."
                    )
                return

            parent = connection.execute(
                """
                SELECT run.*, invocation.role AS campaign_role
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ? AND run.campaign_id = ?
                """,
                (record.parent_operation_id, record.campaign_id),
            ).fetchone()
            if (
                parent is None
                or parent["project_id"] != record.project_id
                or parent["kind"] != record.kind
            ):
                raise ValueError(
                    "An agent task continuation must preserve its parent's project and task kind."
                )

            if role == "report":
                parent_request = json.loads(parent["request_json"])
                parent_actor = parent_request.get("actor_operation_id") or parent["operation_id"]
                parent_role = TypeAdapter(CampaignInvocationRole).validate_python(
                    parent["campaign_role"]
                )
                origin = connection.execute(
                    """
                    SELECT invocation.role
                    FROM graph_runs AS run
                    JOIN campaign_invocations AS invocation
                      ON invocation.operation_id = run.operation_id
                    WHERE run.operation_id = ? AND run.campaign_id = ?
                    """,
                    (actor_operation_id, record.campaign_id),
                ).fetchone()
                if (
                    actor_operation_id == record.operation_id
                    or parent_actor != actor_operation_id
                    or parent_role not in {"orchestrator", "report"}
                    or origin is None
                    or origin["role"] != "orchestrator"
                ):
                    raise ValueError(
                        "A campaign report must retain the sole orchestrator actor lineage."
                    )
                return

            if actor_operation_id == record.operation_id:
                parent_role = TypeAdapter(CampaignInvocationRole).validate_python(
                    parent["campaign_role"]
                )
                if role != "worker" or parent_role != "orchestrator":
                    raise ValueError(
                        "Only the campaign orchestrator may admit a new ordinary worker actor."
                    )
                parent_json = parent["dispatch_authority_json"]
                if parent_json is None:
                    raise ValueError(
                        "A new campaign worker requires its orchestrator's durable authority."
                    )
                parent_authority = AgentDispatchAuthority.model_validate_json(parent_json)
                assert record.dispatch_authority is not None
                if (
                    parent_authority.profile != "orchestrator"
                    or parent_authority.task_contract != "orchestrate"
                    or record.dispatch_authority.scope.campaign_id
                    != parent_authority.scope.campaign_id
                    or record.dispatch_authority.scope.run_truth_scope
                    != parent_authority.scope.run_truth_scope
                ):
                    raise ValueError(
                        "A campaign worker must inherit its orchestrator's project-wide scope."
                    )
                return

            origin = connection.execute(
                """
                SELECT run.dispatch_authority_json, invocation.role AS campaign_role
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ? AND run.campaign_id = ?
                """,
                (actor_operation_id, record.campaign_id),
            ).fetchone()
            if origin is None:
                raise ValueError("A campaign continuation requires its canonical actor origin.")
            origin_role = TypeAdapter(CampaignInvocationRole).validate_python(
                origin["campaign_role"]
            )
            if origin_role != role:
                raise ValueError("A campaign continuation cannot change its canonical actor role.")
            origin_json = origin["dispatch_authority_json"]
            if origin_json is not None:
                origin_authority = AgentDispatchAuthority.model_validate_json(origin_json)
                if record.dispatch_authority != origin_authority:
                    raise ValueError(
                        "A campaign continuation must preserve its actor-origin dispatch authority."
                    )
                return

            # Migration-only: a same-allocation Resume/Retry of an actor recorded before
            # dispatch authority existed may bind today's closed contract. Paid continuations,
            # wakes, and reauthorization may not use this exception.
            parent_request = json.loads(parent["request_json"])
            parent_actor = parent_request.get("actor_operation_id") or parent["operation_id"]
            if not (
                record.attempt == int(parent["attempt"]) + 1
                and parent_actor == actor_operation_id
                and parent["dispatch_authority_json"] is None
            ):
                raise ValueError(
                    "A campaign continuation cannot invent authority for an unbound actor."
                )
            return

        if record.parent_operation_id is None:
            return
        parent = connection.execute(
            """
            SELECT project_id, kind, dispatch_authority_json
            FROM graph_runs WHERE operation_id = ?
            """,
            (record.parent_operation_id,),
        ).fetchone()
        if parent is None:
            raise ValueError("An agent task continuation requires its existing parent task.")
        if parent["project_id"] != record.project_id or parent["kind"] != record.kind:
            raise ValueError(
                "An agent task continuation must preserve its parent's project and task kind."
            )
        if parent["dispatch_authority_json"] is None:
            # A task recorded before dispatch authority existed carries none. An
            # authorization that never happened cannot be invented retroactively,
            # and refusing here would strand every pre-upgrade Resume and Retry.
            # The child still resolves and gates its own binding at dispatch.
            return
        parent_authority = AgentDispatchAuthority.model_validate_json(
            parent["dispatch_authority_json"]
        )
        if record.dispatch_authority != parent_authority:
            raise ValueError(
                "An agent task continuation must preserve its parent's dispatch authority."
            )

    @staticmethod
    def _bind_chat_stage(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        """Keep one exact scratch directory bound to a conversation.

        Every later task in the same chat inherits the prior host/root pair
        while it is inserted under the same write transaction. This makes the
        task ledger authoritative even when project identity adoption rewrites
        ``graph_runs.project_id``; a provider's saved cwd is never renamed or
        re-derived. Multiple saved pairs mean the durable conversation binding
        is already ambiguous, so continuing would risk resuming a native
        session in the wrong directory.
        """

        if record.kind not in {"node_chat", "project_chat"}:
            return
        # Resume, Retry, provider handoff, and Experiment recovery already carry
        # an exact server-owned stage. They are authoritative and may
        # deliberately replace an older binding; only a missing binding is
        # recovered from the durable conversation ledger here.
        if record.stage_root is not None:
            return
        chat_id = record.request.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return
        session_id = record.request.get("session_id")
        watcher_ids = record.request.get("watcher_ids")
        if isinstance(session_id, str) and session_id:
            rows = connection.execute(
                """
                SELECT DISTINCT COALESCE(stage_host, '') AS host, stage_root AS root
                FROM graph_runs
                WHERE project_id = ? AND kind = ?
                  AND json_extract(request_json, '$.chat_id') = ?
                  AND native_session_id = ?
                  AND stage_root IS NOT NULL AND stage_root != ''
                """,
                (record.project_id, record.kind, chat_id, session_id),
            ).fetchall()
        elif (
            record.request.get("trigger") == "watcher"
            and isinstance(watcher_ids, list)
            and watcher_ids
            and all(isinstance(item, str) and item for item in watcher_ids)
        ):
            placeholders = ",".join("?" for _ in watcher_ids)
            rows = connection.execute(
                f"""
                SELECT DISTINCT COALESCE(run.stage_host, '') AS host,
                                run.stage_root AS root
                FROM watchers AS watcher
                JOIN graph_runs AS run
                  ON run.operation_id = watcher.origin_operation_id
                WHERE watcher.watcher_id IN ({placeholders})
                  AND watcher.project_id = ?
                  AND watcher.origin_task_kind = ?
                  AND watcher.chat_id = ?
                  AND run.stage_root IS NOT NULL AND run.stage_root != ''
                """,
                (*watcher_ids, record.project_id, record.kind, chat_id),
            ).fetchall()
        else:
            return
        bindings = {(str(row["host"]), str(row["root"])) for row in rows}
        if len(bindings) > 1:
            raise ValueError(
                "This conversation has conflicting saved workspace bindings and cannot "
                "continue safely."
            )
        if not bindings:
            return
        saved_host, saved_root = next(iter(bindings))
        record.stage_host = saved_host or None
        record.stage_root = saved_root

    @staticmethod
    def _has_active_chat_overlap(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> bool:
        if record.kind not in {"node_chat", "project_chat"}:
            return False
        chat_id = record.request.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return False
        active = connection.execute(
            """
            SELECT 1 FROM graph_runs
            WHERE project_id = ? AND kind = ?
              AND json_extract(request_json, '$.chat_id') = ?
              AND status IN ('queued', 'running', 'pausing')
            LIMIT 1
            """,
            (record.project_id, record.kind, chat_id),
        ).fetchone()
        return active is not None

    def agent_task(self, operation_id: str) -> AgentTaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT graph_runs.*,
                       EXISTS (
                           SELECT 1 FROM graph_run_receipts AS receipt
                           WHERE receipt.operation_id = graph_runs.operation_id
                             AND receipt.category IN (
                                 'experiment_recovery_abandoned',
                                 'campaign_recovery_abandoned'
                             )
                       ) AS recovery_abandoned
                FROM graph_runs WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        return self._agent_task_record(row) if row else None

    def agent_task_authorizer(self, operation_id: str) -> AuthorizedHuman | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT authorized_space_id, authorized_user_id, authorized_display_name
                FROM graph_runs
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._authorized_human_snapshot(row)

    def agent_task_authority(
        self,
        project_id: str,
        operation_id: str,
    ) -> AgentTaskAuthority:
        """Resolve one direct task only inside the project applying its Patch."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT operation_id, project_id, campaign_id, dispatch_authority_json,
                       authorized_space_id, authorized_user_id, authorized_display_name
                FROM graph_runs
                WHERE project_id = ? AND operation_id = ?
                """,
                (project_id, operation_id),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        dispatch_json = row["dispatch_authority_json"]
        return AgentTaskAuthority(
            operation_id=str(row["operation_id"]),
            project_id=str(row["project_id"]),
            authorized_by=self._authorized_human_snapshot(row),
            campaign_id=row["campaign_id"],
            dispatch_authority=(
                AgentDispatchAuthority.model_validate_json(dispatch_json)
                if dispatch_json is not None
                else None
            ),
        )

    def claim_agent_task_graph_repair(self, operation_id: str) -> AgentTaskRecord:
        """Atomically consume one rejected Work result's manual repair eligibility."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            data = dict(row)
            request = json.loads(data["request_json"])
            result = json.loads(data["result_json"]) if data.get("result_json") else None
            graph_update = result.get("graph_update") if isinstance(result, dict) else None
            eligible = (
                data["status"] == "succeeded"
                and data["kind"] in {"node_chat", "project_chat"}
                and isinstance(request, dict)
                and request.get("mode") == "work"
                and bool(data.get("native_session_id"))
                and bool(data.get("stage_root"))
                and isinstance(graph_update, dict)
                and graph_update.get("status") == "rejected"
                and graph_update.get("repairable") is True
            )
            if not eligible:
                raise ValueError(
                    "This task has no repairable graph update. Start a new Work turn instead."
                )
            if request.get("patch_kind") == "experiment_loop":
                control_node_id = request.get("control_node_id")
                episode_id = request.get("control_episode_id")
                invocation = request.get("control_invocation")
                if (
                    not isinstance(control_node_id, str)
                    or not isinstance(episode_id, str)
                    or not isinstance(invocation, int)
                ):
                    raise ValueError("The Experiment graph repair lost its control binding.")
                self._validate_current_experiment_graph_repair(
                    connection,
                    project_id=data["project_id"],
                    control_node_id=control_node_id,
                    episode_id=episode_id,
                    invocation=invocation,
                    operation_id=operation_id,
                )
            assert isinstance(result, dict)
            assert isinstance(graph_update, dict)
            graph_update = {**graph_update, "repairable": False}
            claimed_result = {**result, "graph_update": graph_update}
            claimed_json = self._bounded_result_json(claimed_result)
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET result_json = ?, updated_at = ?
                WHERE operation_id = ? AND result_json = ?
                """,
                (claimed_json, self.now(), operation_id, data["result_json"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("This graph update repair was already claimed.")
        claimed = self.agent_task(operation_id)
        assert claimed is not None
        return claimed

    def restore_agent_task_graph_repair(self, operation_id: str) -> None:
        """Undo an unconsumed claim only when no repair child was created."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM graph_runs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or not row["result_json"]:
                return
            child = connection.execute(
                "SELECT 1 FROM graph_runs WHERE parent_operation_id = ? LIMIT 1",
                (operation_id,),
            ).fetchone()
            if child is not None:
                return
            result = json.loads(row["result_json"])
            graph_update = result.get("graph_update") if isinstance(result, dict) else None
            if (
                not isinstance(graph_update, dict)
                or graph_update.get("status") != "rejected"
                or graph_update.get("repairable") is not False
            ):
                return
            restored = {
                **result,
                "graph_update": {**graph_update, "repairable": True},
            }
            connection.execute(
                "UPDATE graph_runs SET result_json = ?, updated_at = ? WHERE operation_id = ?",
                (self._bounded_result_json(restored), self.now(), operation_id),
            )

    def agent_tasks(
        self, project_id: str, *, limit: int = AGENT_TASK_LIST_DEFAULT_LIMIT
    ) -> list[AgentTaskRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT graph_runs.*,
                       EXISTS (
                           SELECT 1 FROM graph_run_receipts AS receipt
                           WHERE receipt.operation_id = graph_runs.operation_id
                             AND receipt.category IN (
                                 'experiment_recovery_abandoned',
                                 'campaign_recovery_abandoned'
                             )
                       ) AS recovery_abandoned
                FROM graph_runs
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, max(1, min(limit, AGENT_TASK_LIST_MAX_LIMIT))),
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def has_active_chat_task(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
    ) -> bool:
        """Return whether one exact chat already owns an active task."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND kind = ?
                  AND json_extract(request_json, '$.chat_id') = ?
                  AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                (project_id, kind, chat_id),
            ).fetchone()
        return row is not None

    def has_chat_native_session_origin(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
        provider: str,
        execution_machine: str,
        native_session_id: str,
    ) -> bool:
        """Prove that RCP previously observed this session on the exact chat binding."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND kind = ?
                  AND json_extract(request_json, '$.chat_id') = ?
                  AND json_extract(request_json, '$.node_id') IS ?
                  AND json_extract(request_json, '$.provider') = ?
                  AND json_extract(request_json, '$.run_on') = ?
                  AND native_session_id = ?
                LIMIT 1
                """,
                (
                    project_id,
                    kind,
                    chat_id,
                    node_id,
                    provider,
                    execution_machine,
                    native_session_id,
                ),
            ).fetchone()
        return row is not None

    def chat_session_context(
        self,
        provider: str,
        execution_machine: str,
        native_session_id: str,
    ) -> ChatSessionContextRecord | None:
        """Read the durable baseline for one exact native provider session."""

        with self.connection() as connection:
            row = self._chat_session_context_row(
                connection,
                provider,
                execution_machine,
                native_session_id,
            )
        return self._chat_session_context_record(row) if row is not None else None

    def validate_chat_session_context_binding(
        self,
        provider: str,
        execution_machine: str,
        native_session_id: str,
        *,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
    ) -> ChatSessionContextRecord | None:
        """Return an existing baseline only when its complete binding matches."""

        with self.connection() as connection:
            row = self._chat_session_context_row(
                connection,
                provider,
                execution_machine,
                native_session_id,
            )
            if row is None:
                return None
            self._validate_chat_session_context_binding(
                row,
                project_id=project_id,
                kind=kind,
                chat_id=chat_id,
                node_id=node_id,
            )
        return self._chat_session_context_record(row)

    def commit_chat_session_context(
        self,
        *,
        provider: str,
        execution_machine: str,
        native_session_id: str,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
        protocol_version: int,
        snapshot_json: str,
        snapshot_sha256: str,
        committed_operation_id: str,
        expected_snapshot_sha256: str | None,
    ) -> ChatSessionContextRecord:
        """CAS one session baseline, inserting only when no prior digest is expected."""

        now = self.now()
        ChatSessionContextRecord.model_validate(
            {
                "provider": provider,
                "execution_machine": execution_machine,
                "native_session_id": native_session_id,
                "project_id": project_id,
                "kind": kind,
                "chat_id": chat_id,
                "node_id": node_id,
                "protocol_version": protocol_version,
                "snapshot_json": snapshot_json,
                "snapshot_sha256": snapshot_sha256,
                "committed_operation_id": committed_operation_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        try:
            json.loads(snapshot_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Chat session context snapshot must be valid JSON.") from exc
        actual_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        if snapshot_sha256 != actual_sha256:
            raise ValueError("Chat session context snapshot SHA-256 does not match its JSON.")

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._chat_session_context_row(
                    connection,
                    provider,
                    execution_machine,
                    native_session_id,
                )
                if row is None:
                    if expected_snapshot_sha256 is not None:
                        raise ValueError(
                            "Chat session context compare-and-swap failed: prior baseline is missing."
                        )
                    connection.execute(
                        """
                        INSERT INTO chat_session_contexts (
                            provider, execution_machine, native_session_id,
                            project_id, kind, chat_id, node_id, protocol_version,
                            snapshot_json, snapshot_sha256, committed_operation_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            provider,
                            execution_machine,
                            native_session_id,
                            project_id,
                            kind,
                            chat_id,
                            node_id,
                            protocol_version,
                            snapshot_json,
                            snapshot_sha256,
                            committed_operation_id,
                            now,
                            now,
                        ),
                    )
                else:
                    self._validate_chat_session_context_binding(
                        row,
                        project_id=project_id,
                        kind=kind,
                        chat_id=chat_id,
                        node_id=node_id,
                    )
                    if expected_snapshot_sha256 != row["snapshot_sha256"]:
                        raise ValueError(
                            "Chat session context compare-and-swap failed: prior digest changed."
                        )
                    changed = connection.execute(
                        """
                        UPDATE chat_session_contexts
                        SET protocol_version = ?, snapshot_json = ?, snapshot_sha256 = ?,
                            committed_operation_id = ?, updated_at = ?
                        WHERE provider = ? AND execution_machine = ? AND native_session_id = ?
                          AND snapshot_sha256 = ?
                        """,
                        (
                            protocol_version,
                            snapshot_json,
                            snapshot_sha256,
                            committed_operation_id,
                            now,
                            provider,
                            execution_machine,
                            native_session_id,
                            expected_snapshot_sha256,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ValueError(
                            "Chat session context compare-and-swap failed: prior digest changed."
                        )
            except Exception:
                connection.rollback()
                raise

        stored = self.chat_session_context(provider, execution_machine, native_session_id)
        assert stored is not None
        return stored

    def record_agent_usage(self, operation_id: str, usage: ProviderUsage) -> AgentUsageRecord:
        """Persist one provider usage report and mark duplicate reports excluded."""

        task = self.agent_task(operation_id)
        if task is None:
            raise ValueError(f"Cannot attribute provider usage to unknown task {operation_id!r}")
        usage_id = str(uuid.uuid4())
        now = self.now()
        with self.connection() as connection:
            duplicate = connection.execute(
                """
                SELECT 1 FROM agent_usage
                WHERE operation_id = ? AND provider_profile = ? AND dedupe_key = ?
                    AND counted = 1
                LIMIT 1
                """,
                (operation_id, usage.provider_profile, usage.dedupe_key),
            ).fetchone()
            counted = duplicate is None
            count_reason: AgentUsageCountReason = "counted" if counted else "duplicate"
            connection.execute(
                """
                INSERT INTO agent_usage (
                    usage_id, project_id, operation_id, provider, model,
                    task_kind, provider_profile, provider_event_type, dedupe_key, counted,
                    count_reason, created_at, processed_input_tokens,
                    generated_tokens, cached_input_tokens,
                    cache_creation_input_tokens, cache_write_input_tokens,
                    reasoning_output_tokens, reported_input_tokens,
                    reported_output_tokens, reported_total_tokens,
                    provider_fields_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    task.project_id,
                    operation_id,
                    task.request.get("provider") or "unknown",
                    task.request.get("model"),
                    task.kind,
                    usage.provider_profile,
                    usage.provider_event_type,
                    usage.dedupe_key,
                    int(counted),
                    count_reason,
                    now,
                    usage.processed_input_tokens,
                    usage.generated_tokens,
                    usage.cached_input_tokens,
                    usage.cache_creation_input_tokens,
                    usage.cache_write_input_tokens,
                    usage.reasoning_output_tokens,
                    usage.reported_input_tokens,
                    usage.reported_output_tokens,
                    usage.reported_total_tokens,
                    json.dumps(usage.provider_fields, separators=(",", ":")),
                ),
            )
        record = self.agent_usage_record(usage_id)
        assert record is not None
        return record

    def agent_usage_record(self, usage_id: str) -> AgentUsageRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_usage WHERE usage_id = ?", (usage_id,)
            ).fetchone()
        return self._agent_usage_record(row) if row else None

    def agent_usage(self, project_id: str) -> list[AgentUsageRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_usage
                WHERE project_id = ?
                ORDER BY created_at ASC, usage_id ASC
                """,
                (project_id,),
            ).fetchall()
        return [self._agent_usage_record(row) for row in rows]

    def agent_usage_snapshot(self, project_id: str) -> AgentUsageSnapshot:
        records = self.agent_usage(project_id)
        input_processed, generated, counted_records, excluded_records = self._agent_usage_metrics(
            records
        )
        return AgentUsageSnapshot(
            project_id=project_id,
            input_processed=input_processed,
            generated=generated,
            counted_records=counted_records,
            excluded_records=excluded_records,
            records=records,
        )

    def _agent_usage_metrics(
        self,
        records: list[AgentUsageRecord],
    ) -> tuple[AgentUsageMetric, AgentUsageMetric, int, int]:
        counted = [record for record in records if record.counted]
        # Input reports describe the full context of one request. For a resumed
        # native session, later reports supersede earlier context sizes; generated
        # output is newly produced content and remains additive.
        latest_input_by_session: dict[tuple[str, str], AgentUsageRecord] = {}
        input_cells: dict[tuple[AgentTaskKind, str], AgentUsageCell] = {}
        generated_cells: dict[tuple[AgentTaskKind, str], AgentUsageCell] = {}
        tasks: dict[str, AgentTaskRecord | None] = {}
        for record in counted:
            if record.operation_id not in tasks:
                tasks[record.operation_id] = self.agent_task(record.operation_id)
            task = tasks[record.operation_id]
            if task is None:
                continue
            native_session_id = task.native_session_id or task.request.get("session_id")
            session_key = (
                (record.provider, native_session_id)
                if isinstance(native_session_id, str) and native_session_id
                else (record.provider, f"usage:{record.usage_id}")
            )
            previous = latest_input_by_session.get(session_key)
            if previous is None or (record.created_at, record.usage_id) > (
                previous.created_at,
                previous.usage_id,
            ):
                latest_input_by_session[session_key] = record

            key = (task.kind, record.provider)
            generated_cell = generated_cells.setdefault(
                key,
                AgentUsageCell(task_kind=task.kind, provider=record.provider),
            )
            generated_cell.generated_tokens += record.generated_tokens
            generated_cell.counted_records += 1

        for record in latest_input_by_session.values():
            task = tasks[record.operation_id]
            if task is None:
                continue
            key = (task.kind, record.provider)
            input_cell = input_cells.setdefault(
                key,
                AgentUsageCell(task_kind=task.kind, provider=record.provider),
            )
            input_cell.processed_input_tokens += record.processed_input_tokens
            input_cell.cached_input_tokens += record.cached_input_tokens
            input_cell.counted_records += 1

        input_total = sum(cell.processed_input_tokens for cell in input_cells.values())
        generated_total = sum(cell.generated_tokens for cell in generated_cells.values())
        cached_total = sum(cell.cached_input_tokens for cell in input_cells.values())
        return (
            AgentUsageMetric(
                total_tokens=input_total,
                cached_tokens=cached_total,
                cache_share=cached_total / input_total if input_total else 0.0,
                block_tokens=input_total / 20 if input_total else 0.0,
                cells=sorted(
                    input_cells.values(),
                    key=lambda cell: (cell.task_kind, cell.provider),
                ),
            ),
            AgentUsageMetric(
                total_tokens=generated_total,
                block_tokens=generated_total / 20 if generated_total else 0.0,
                cells=sorted(
                    generated_cells.values(),
                    key=lambda cell: (cell.task_kind, cell.provider),
                ),
            ),
            len(counted),
            len(records) - len(counted),
        )

    def has_resumable_paused_chat_task(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
    ) -> bool:
        """Whether this conversation has a paused attempt awaiting a decision.

        A Resume or Retry creates a child operation immediately. Once that child
        exists, the paused parent no longer blocks a later ordinary turn; if the
        child itself pauses, it is independently found by this query.
        """

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM graph_runs AS paused
                WHERE paused.project_id = ?
                    AND paused.kind = ?
                    AND paused.status = 'paused'
                    AND paused.native_session_id IS NOT NULL
                    AND (paused.stage_host IS NULL OR paused.stage_host = ''
                         OR paused.stage_root IS NOT NULL)
                    AND json_extract(paused.request_json, '$.chat_id') = ?
                    AND NOT EXISTS (
                        SELECT 1
                        FROM graph_runs AS child
                        WHERE child.parent_operation_id = paused.operation_id
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM graph_run_receipts AS receipt
                        WHERE receipt.operation_id = paused.operation_id
                          AND receipt.category = 'experiment_recovery_abandoned'
                    )
                LIMIT 1
                """,
                (project_id, kind, chat_id),
            ).fetchone()
        return row is not None

    def has_any_active_agent_task(self) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def has_active_agent_task(self, project_id: str) -> bool:
        with self.connection() as connection:
            canonical_project_id = self._resolve_project_id_from_connection(connection, project_id)
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ?
                  AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                (canonical_project_id,),
            ).fetchone()
        return row is not None

    def agent_task_events(
        self, operation_id: str, *, limit: int = AGENT_TASK_EVENT_LIST_DEFAULT_LIMIT
    ) -> list[AgentTaskEventRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM graph_run_events
                WHERE operation_id = ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (operation_id, max(1, min(limit, AGENT_TASK_EVENT_LIST_MAX_LIMIT))),
            ).fetchall()
        return [self._agent_task_event_record(row) for row in rows]

    def record_agent_task_event(
        self,
        operation_id: str,
        message: str,
        *,
        level: Literal["info", "warning", "error"] = "info",
    ) -> None:
        detail = " ".join(message.split())[:2000]
        if not detail:
            return
        with self.connection() as connection:
            self._insert_agent_task_event(
                connection,
                operation_id,
                detail,
                level=level,
                created_at=self.now(),
            )

    @staticmethod
    def _insert_agent_task_event(
        connection: sqlite3.Connection,
        operation_id: str,
        detail: str,
        *,
        level: Literal["info", "warning", "error"],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO graph_run_events (operation_id, created_at, level, message)
            VALUES (?, ?, ?, ?)
            """,
            (operation_id, created_at, level, detail),
        )
        connection.execute(
            """
            DELETE FROM graph_run_events
            WHERE operation_id = ? AND event_kind = 'message' AND event_id NOT IN (
                SELECT event_id FROM graph_run_events
                WHERE operation_id = ? AND event_kind = 'message'
                ORDER BY event_id DESC
                LIMIT ?
            )
            """,
            (operation_id, operation_id, AGENT_TASK_EVENT_RETENTION_COUNT),
        )

    def agent_task_receipts(
        self, operation_id: str, *, limit: int = AGENT_TASK_RECEIPT_LIST_LIMIT
    ) -> list[AgentTaskReceiptRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM graph_run_receipts
                WHERE operation_id = ?
                ORDER BY receipt_id ASC
                LIMIT ?
                """,
                (operation_id, max(1, min(limit, AGENT_TASK_RECEIPT_LIST_LIMIT))),
            ).fetchall()
        receipts = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            receipts.append(AgentTaskReceiptRecord.model_validate(data))
        return receipts

    def agent_task_continuation_cause(self, operation_id: str) -> str | None:
        """Return the durable launch cause for one task attempt.

        Recovery must preserve patch-only graph-repair semantics instead of
        inferring a full Work turn from the request shape alone.
        """

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM graph_run_receipts
                WHERE operation_id = ? AND category = 'operation_created'
                ORDER BY receipt_id ASC
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        cause = payload.get("continuation_cause") if isinstance(payload, dict) else None
        return cause if isinstance(cause, str) and cause else None

    def record_agent_task_receipt(
        self,
        operation_id: str,
        category: str,
        payload: dict[str, object],
        *,
        tier: AgentTaskReceiptTier = "summary",
    ) -> None:
        safe_category = " ".join(category.split())[:100]
        if not safe_category:
            return
        if tier not in AGENT_TASK_RECEIPT_RETENTION_COUNTS:
            raise ValueError(f"Unknown agent-task receipt tier: {tier}")
        payload_json = self._bounded_receipt_payload(payload)
        with self.connection() as connection:
            self._insert_agent_task_receipt(
                connection,
                operation_id,
                safe_category,
                payload_json,
                tier=tier,
                created_at=self.now(),
            )

    @staticmethod
    def _insert_agent_task_receipt(
        connection: sqlite3.Connection,
        operation_id: str,
        category: str,
        payload_json: str,
        *,
        tier: AgentTaskReceiptTier,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO graph_run_receipts (
                operation_id, created_at, tier, category, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (operation_id, created_at, tier, category, payload_json),
        )
        connection.execute(
            """
            DELETE FROM graph_run_receipts
            WHERE operation_id = ? AND tier = ? AND receipt_id NOT IN (
                SELECT receipt_id FROM graph_run_receipts
                WHERE operation_id = ? AND tier = ?
                ORDER BY receipt_id DESC
                LIMIT ?
            )
            """,
            (
                operation_id,
                tier,
                operation_id,
                tier,
                AGENT_TASK_RECEIPT_RETENTION_COUNTS[tier],
            ),
        )

    def record_agent_task_contract(
        self, operation_id: str, role: str, content: str, sha256: str
    ) -> None:
        """Persist immutable contract content outside bounded diagnostic receipts."""
        safe_role = " ".join(role.split())[:200]
        if not safe_role:
            raise ValueError("agent-task contract role is empty")
        with self.connection() as connection:
            existing = connection.execute(
                """
                SELECT sha256, content FROM graph_run_contracts
                WHERE operation_id = ? AND role = ?
                """,
                (operation_id, safe_role),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != sha256 or existing["content"] != content:
                    raise ValueError("immutable agent-task contract already differs")
                return
            connection.execute(
                """
                INSERT INTO graph_run_contracts (
                    operation_id, role, created_at, sha256, content
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, safe_role, self.now(), sha256, content),
            )

    def agent_task_contract(self, operation_id: str, role: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT content FROM graph_run_contracts
                WHERE operation_id = ? AND role = ?
                """,
                (operation_id, role),
            ).fetchone()
        return str(row["content"]) if row is not None else None

    def agent_task_contracts(self, operation_id: str) -> list[AgentTaskContractRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, role, created_at, sha256, content
                FROM graph_run_contracts
                WHERE operation_id = ?
                ORDER BY rowid
                """,
                (operation_id,),
            ).fetchall()
        return [AgentTaskContractRecord.model_validate(dict(row)) for row in rows]

    @staticmethod
    def _bounded_receipt_payload(payload: dict[str, object]) -> str:
        keys = [str(key)[:80] for key in list(payload)[:32]]
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "omitted": True,
                    "reason": "payload_not_json_serializable",
                    "keys": keys,
                },
                separators=(",", ":"),
            )
        byte_length = len(encoded.encode("utf-8"))
        if byte_length <= AGENT_TASK_RECEIPT_MAX_BYTES:
            return encoded
        return json.dumps(
            {
                "omitted": True,
                "reason": "payload_exceeded_limit",
                "byte_length": byte_length,
                "keys": keys,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _bounded_command_payload(payload: dict[str, object]) -> str:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("agent command event payload is not valid JSON") from exc
        if len(encoded.encode("utf-8")) > AGENT_COMMAND_EVENT_MAX_BYTES:
            raise ValueError("agent command event payload exceeds the configured size limit")
        return encoded

    @staticmethod
    def _bounded_result_json(result: dict[str, object] | None) -> str | None:
        if result is None:
            return None
        raw_artifacts = result.get("artifacts")
        artifacts: list[dict[str, object]] = []
        if isinstance(raw_artifacts, list):
            for raw_artifact in raw_artifacts[:CHAT_ARTIFACT_MAX_COUNT]:
                try:
                    descriptor = AgentArtifactDescriptor.model_validate(raw_artifact)
                except (TypeError, ValueError):
                    continue
                artifacts.append(descriptor.model_dump(mode="json"))
        payload: dict[str, object] = {"messages": []}
        if artifacts:
            payload["artifacts"] = artifacts
        raw_graph_update = result.get("graph_update")
        if isinstance(raw_graph_update, dict) and raw_graph_update.get("status") in {
            "none",
            "applied",
            "rejected",
        }:
            raw_change_summary = raw_graph_update.get("change_summary")
            raw_proposal_ids = raw_graph_update.get("proposal_ids")
            raw_validation_messages = raw_graph_update.get("validation_messages")
            payload["graph_update"] = {
                "status": raw_graph_update["status"],
                "applied_revision": (
                    raw_graph_update.get("applied_revision")
                    if isinstance(raw_graph_update.get("applied_revision"), int)
                    and not isinstance(raw_graph_update.get("applied_revision"), bool)
                    else None
                ),
                "change_summary": [
                    item[:1600]
                    for item in (
                        raw_change_summary[:32] if isinstance(raw_change_summary, list) else []
                    )
                    if isinstance(item, str)
                ],
                "proposal_ids": [
                    item[:400]
                    for item in (
                        raw_proposal_ids[:32] if isinstance(raw_proposal_ids, list) else []
                    )
                    if isinstance(item, str)
                ],
                "validation_messages": [
                    item[:1600]
                    for item in (
                        raw_validation_messages[:8]
                        if isinstance(raw_validation_messages, list)
                        else []
                    )
                    if isinstance(item, str)
                ],
                "correction_rounds": (
                    raw_graph_update.get("correction_rounds")
                    if isinstance(raw_graph_update.get("correction_rounds"), int)
                    and not isinstance(raw_graph_update.get("correction_rounds"), bool)
                    else 0
                ),
                "repairable": raw_graph_update.get("repairable") is True,
            }
        raw_messages = result.get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        bounded: list[str] = []
        for raw_message in messages[:32]:
            if not isinstance(raw_message, str):
                continue
            message = raw_message.strip()
            if not message:
                continue
            bounded.append(message[:16_000])
            payload["messages"] = bounded
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(encoded.encode("utf-8")) > AGENT_TASK_RESULT_MAX_BYTES:
                bounded.pop()
                break
        payload["messages"] = bounded
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def agent_task_patch_output(self, operation_id: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT patch_json FROM graph_run_outputs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return str(row["patch_json"]) if row else None

    def record_agent_task_patch_output(self, operation_id: str, patch_json: str) -> None:
        if len(patch_json.encode("utf-8")) > 2_000_000:
            raise ValueError("direct patch output exceeds the 2 MB recovery limit")
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO graph_run_outputs (operation_id, created_at, patch_json)
                VALUES (?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    patch_json = excluded.patch_json
                """,
                (operation_id, self.now(), patch_json),
            )

    def agent_task_estimate(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: dict[str, object],
    ) -> tuple[float, int]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT request_json, started_at, finished_at
                FROM graph_runs
                WHERE project_id = ? AND kind = ? AND status = 'succeeded'
                    AND started_at IS NOT NULL AND finished_at IS NOT NULL
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (project_id, kind, AGENT_TASK_ESTIMATE_HISTORY_LIMIT),
            ).fetchall()
        durations: list[float] = []
        for row in rows:
            saved_request = json.loads(row["request_json"])
            if saved_request.get("provider") != request.get("provider"):
                continue
            if (saved_request.get("model") or "") != (request.get("model") or ""):
                continue
            try:
                started = datetime.fromisoformat(row["started_at"])
                finished = datetime.fromisoformat(row["finished_at"])
            except (TypeError, ValueError):
                continue
            duration = (finished - started).total_seconds()
            if duration > 0:
                durations.append(duration)
            if len(durations) == AGENT_TASK_ESTIMATE_SAMPLE_LIMIT:
                break
        if durations:
            return max(1.0, float(median(durations))), len(durations)
        return (600.0 if kind == "seed" else 300.0), 0

    def mark_agent_task_running(self, operation_id: str) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'running', started_at = ?, updated_at = ?,
                    last_activity_at = ?, phase = 'preparing',
                    status_message = 'Preparing agent task.'
                WHERE operation_id = ? AND status = 'queued'
                """,
                (now, now, now, operation_id),
            )
        self.record_agent_task_event(operation_id, "Preparing agent task.")

    def update_agent_task_message(
        self,
        operation_id: str,
        message: str,
        *,
        phase: str | None = None,
        event: bool = False,
    ) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status_message = ?, updated_at = ?, last_activity_at = ?,
                    phase = COALESCE(?, phase)
                WHERE operation_id = ? AND status IN ('running', 'pausing')
                """,
                (message, now, now, phase, operation_id),
            )
        if event:
            self.record_agent_task_event(operation_id, message)

    def checkpoint_agent_task(
        self,
        operation_id: str,
        *,
        native_session_id: str | None = None,
        stage_host: str | None = None,
        stage_root: str | None = None,
    ) -> None:
        now = self.now()
        with self.connection() as connection:
            updated = connection.execute(
                """
                UPDATE graph_runs
                SET native_session_id = COALESCE(?, native_session_id),
                    stage_host = COALESCE(?, stage_host),
                    stage_root = COALESCE(?, stage_root),
                    updated_at = ?, last_activity_at = ?
                WHERE operation_id = ?
                  AND (
                      ? IS NULL
                      OR native_session_id IS NULL
                      OR native_session_id = ?
                  )
                """,
                (
                    native_session_id,
                    stage_host,
                    stage_root,
                    now,
                    now,
                    operation_id,
                    native_session_id,
                    native_session_id,
                ),
            ).rowcount
            if updated == 1:
                return
            existing = connection.execute(
                "SELECT native_session_id FROM graph_runs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(operation_id)
            raise ValueError("Agent task native session conflicts with its saved RCP checkpoint.")

    def clear_agent_task_stage(self, operation_id: str) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET stage_host = NULL, stage_root = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (now, operation_id),
            )

    def request_agent_task_pause(
        self, operation_id: str, *, requested_by: Literal["human", "shutdown"] = "human"
    ) -> AgentTaskRecord:
        now = self.now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET status = 'pausing', updated_at = ?, last_activity_at = ?,
                    phase = 'pausing', status_message = 'Pausing at the current checkpoint.'
                WHERE operation_id = ? AND status IN ('queued', 'running')
                """,
                (now, now, operation_id),
            )
        if cursor.rowcount == 0:
            raise ValueError("Only a queued or running operation can be paused.")
        self.record_agent_task_event(
            operation_id,
            (
                "Pause requested by the human."
                if requested_by == "human"
                else "Paused for RCP shutdown or reload."
            ),
        )
        record = self.agent_task(operation_id)
        assert record is not None
        return record

    def pause_agent_task(
        self,
        operation_id: str,
        *,
        detail: str | None = None,
        result: dict[str, object] | None = None,
    ) -> None:
        now = self.now()
        detail = (
            detail or "Paused. Resume from the saved agent session, or retry from the beginning."
        )
        result_json = self._bounded_result_json(result) if result is not None else None
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'paused', updated_at = ?, finished_at = ?,
                    last_activity_at = ?, phase = 'paused', status_message = ?, error = NULL,
                    result_json = COALESCE(?, result_json)
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (now, now, now, detail, result_json, operation_id),
            )
            self._insert_agent_task_event(
                connection,
                operation_id,
                detail,
                level="warning",
                created_at=now,
            )
            self._insert_agent_task_receipt(
                connection,
                operation_id,
                "operation_paused",
                self._bounded_receipt_payload({"status": "paused"}),
                tier="summary",
                created_at=now,
            )

    def complete_agent_task(
        self,
        operation_id: str,
        *,
        applied_revision: int | None,
        result: dict[str, object],
    ) -> None:
        now = self.now()
        result_json = self._bounded_result_json(result)
        graph_update = result.get("graph_update")
        graph_rejected = isinstance(graph_update, dict) and graph_update.get("status") == "rejected"
        status_message = (
            "Completed; graph update rejected." if graph_rejected else "Agent task completed."
        )
        message = (
            f"Project graph updated to revision {applied_revision}."
            if applied_revision is not None
            else "Operational work completed, but its graph update was rejected."
            if graph_rejected
            else "Agent task completed."
        )
        payload: dict[str, object] = {"status": "succeeded"}
        if applied_revision is not None:
            payload["applied_revision"] = applied_revision
        if isinstance(graph_update, dict):
            payload["graph_update_status"] = str(graph_update.get("status") or "none")
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'succeeded', updated_at = ?, finished_at = ?,
                    status_message = ?, error = NULL,
                    applied_revision = ?, result_json = ?,
                    phase = 'complete', last_activity_at = ?
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (
                    now,
                    now,
                    status_message,
                    applied_revision,
                    result_json,
                    now,
                    operation_id,
                ),
            )
            if not graph_rejected:
                connection.execute(
                    "DELETE FROM graph_run_outputs WHERE operation_id = ?",
                    (operation_id,),
                )
            self._insert_agent_task_event(
                connection,
                operation_id,
                message,
                level="info",
                created_at=now,
            )
            self._insert_agent_task_receipt(
                connection,
                operation_id,
                "operation_completed",
                self._bounded_receipt_payload(payload),
                tier="summary",
                created_at=now,
            )

    def fail_agent_task(
        self,
        operation_id: str,
        error: str,
        *,
        status: Literal["failed", "interrupted"] = "failed",
        result: dict[str, object] | None = None,
    ) -> None:
        """Record a failure, keeping any output the task produced before it.

        A chat turn that answered and then had its graph change rejected has
        already earned its reply; failing must not throw that away.
        """
        now = self.now()
        detail = " ".join(error.split())[:2000] or "The background agent task failed."
        self.record_agent_task_event(operation_id, detail, level="error")
        self.record_agent_task_receipt(
            operation_id,
            "operation_failed",
            {"status": status, "error_length": len(detail)},
        )
        result_json = self._bounded_result_json(result) if result is not None else None
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = ?, updated_at = ?, finished_at = ?,
                    status_message = ?, error = ?, phase = ?, last_activity_at = ?,
                    result_json = COALESCE(?, result_json)
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (status, now, now, detail, detail, status, now, result_json, operation_id),
            )

    def interrupt_active_agent_tasks(self) -> None:
        now = self.now()
        detail = (
            "RCP restarted before this operation finished. Resume from its saved session "
            "when available, or retry from the beginning."
        )
        interrupted: list[str] = []
        with self.connection() as connection:
            interrupted = [
                row["operation_id"]
                for row in connection.execute(
                    "SELECT operation_id FROM graph_runs WHERE status IN ('queued', 'running', 'pausing')"
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'interrupted', updated_at = ?, finished_at = ?,
                    status_message = ?, error = ?, phase = 'interrupted', last_activity_at = ?
                WHERE status IN ('queued', 'running', 'pausing')
                """,
                (now, now, detail, detail, now),
            )
        for operation_id in interrupted:
            self.record_agent_task_event(operation_id, detail, level="warning")
            self.record_agent_task_receipt(
                operation_id,
                "operation_interrupted",
                {"status": "interrupted", "reason": "process_restart"},
            )

    def prune_operational_storage(self, *, now: datetime | None = None) -> dict[str, int]:
        """Age out bulky run payloads. `graph_runs` rows are never deleted, so
        resume ancestry (invariant 10b) stays walkable for the life of a project."""

        current = _result_view_reference_time(now)
        inactive = """
            operation_id NOT IN (
                SELECT operation_id FROM graph_runs
                WHERE status IN ('queued', 'running', 'pausing')
            )
        """
        patch_cutoff = (current - timedelta(days=PATCH_OUTPUT_RETENTION_DAYS)).isoformat()
        trace_cutoff = (current - timedelta(days=RUN_TRACE_RETENTION_DAYS)).isoformat()
        with self.connection() as connection:
            expired_result_views = self._delete_expired_result_views_from_connection(
                connection,
                current,
            )
            outputs = connection.execute(
                f"DELETE FROM graph_run_outputs WHERE created_at < ? AND {inactive}",
                (patch_cutoff,),
            ).rowcount
            events = connection.execute(
                f"""
                DELETE FROM graph_run_events
                WHERE event_kind = 'message' AND created_at < ? AND {inactive}
                """,
                (trace_cutoff,),
            ).rowcount
            # Summary receipts carry the resume freshness proof (`operation_created`,
            # `chat_context_assembled`); only the bulky lower tiers age out.
            receipts = connection.execute(
                f"""
                DELETE FROM graph_run_receipts
                WHERE created_at < ? AND tier IN ('diagnostic', 'trace') AND {inactive}
                """,
                (trace_cutoff,),
            ).rowcount

            writing_cutoff = current - timedelta(days=WRITING_SESSION_RETENTION_DAYS)
            writing_rows = connection.execute(
                """
                SELECT native_session_id, project_id, last_resumed_at
                FROM writing_sessions
                ORDER BY project_id, last_resumed_at DESC
                """
            ).fetchall()
            delete_writing: list[str] = []
            writing_by_project: dict[str, list[sqlite3.Row]] = {}
            for row in writing_rows:
                writing_by_project.setdefault(str(row["project_id"]), []).append(row)
            for rows in writing_by_project.values():
                for index, row in enumerate(rows):
                    resumed_at = self._parse_time(row["last_resumed_at"])
                    if (
                        index >= WRITING_SESSIONS_PER_PROJECT
                        and resumed_at is not None
                        and resumed_at < writing_cutoff
                    ):
                        delete_writing.append(str(row["native_session_id"]))
            for session_id in delete_writing:
                connection.execute(
                    "DELETE FROM writing_sessions WHERE native_session_id = ?", (session_id,)
                )

        return {
            "outputs": outputs,
            "events": events,
            "receipts": receipts,
            "writing_sessions": len(delete_writing),
            "result_views": expired_result_views,
        }

    @staticmethod
    def _chat_session_context_row(
        connection: sqlite3.Connection,
        provider: str,
        execution_machine: str,
        native_session_id: str,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT * FROM chat_session_contexts WHERE native_session_id = ?",
            (native_session_id,),
        ).fetchall()
        conflicts = [
            row
            for row in rows
            if row["provider"] != provider or row["execution_machine"] != execution_machine
        ]
        if conflicts:
            raise ValueError(
                "Chat session context provider or execution-machine conflict for native session."
            )
        return next(
            (
                row
                for row in rows
                if row["provider"] == provider and row["execution_machine"] == execution_machine
            ),
            None,
        )

    @staticmethod
    def _validate_chat_session_context_binding(
        row: sqlite3.Row,
        *,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
    ) -> None:
        expected = {
            "project_id": project_id,
            "kind": kind,
            "chat_id": chat_id,
            "node_id": node_id,
        }
        conflicts = [name for name, value in expected.items() if row[name] != value]
        if conflicts:
            raise ValueError(
                "Chat session context immutable binding conflict: " + ", ".join(conflicts)
            )
