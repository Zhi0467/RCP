from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Literal

from pydantic import (
    TypeAdapter,
)

from rcp.limits import (
    CAMPAIGN_MAIL_MAX_MESSAGES,
    WATCHER_ERROR_BACKOFF_SECONDS,
)
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


class CampaignStoreMixin:
    """Auto-research campaigns: budget, recovery, stop, wrap-up, reports, and mail."""

    def create_campaign_with_root_task(
        self,
        campaign: CampaignRecord,
        task: AgentTaskRecord,
    ) -> tuple[CampaignRecord, AgentTaskRecord]:
        """Create the sole live project campaign and spend its first research unit atomically."""

        if campaign.status not in {"queued", "running"}:
            raise ValueError("a new campaign must start queued or running")
        if campaign.invocations_used != 0:
            raise ValueError("a new campaign budget must be unused")
        if campaign.invocation_ceiling < 2:
            raise ValueError("a campaign needs one research invocation and one report invocation")
        if task.campaign_id != campaign.campaign_id:
            raise ValueError("the campaign root task must carry its campaign id")
        if task.project_id != campaign.project_id or task.kind != "campaign":
            raise ValueError("the campaign root task must belong to the campaign project")
        if task.parent_operation_id is not None:
            raise ValueError("the campaign root task cannot have a parent task")
        if campaign.root_operation_id not in {None, task.operation_id}:
            raise ValueError("the campaign root operation does not match its task")

        campaign = campaign.model_copy(
            update={"root_operation_id": task.operation_id, "status": "running"}
        )
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._insert_campaign(connection, campaign)
                self._insert_campaign_task(connection, campaign, task, "orchestrator")
        except sqlite3.IntegrityError as exc:
            raise ValueError("Only one live auto-research campaign may run per project.") from exc
        stored_campaign = self.campaign(campaign.campaign_id)
        stored_task = self.agent_task(task.operation_id)
        assert stored_campaign is not None and stored_task is not None
        return stored_campaign, stored_task

    def create_campaign_agent_task(
        self,
        record: AgentTaskRecord,
        *,
        role: CampaignInvocationRole,
    ) -> AgentTaskRecord:
        """Admit one provider turn from the shared pot and create its task in one commit."""

        if record.campaign_id is None:
            raise ValueError("a campaign task must carry its campaign id")
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?",
                    (record.campaign_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(record.campaign_id)
                campaign = self._campaign_record(row)
                self._insert_campaign_task(connection, campaign, record, role)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the campaign agent task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_campaign_recovery_task(self, record: AgentTaskRecord) -> AgentTaskRecord:
        """Create one same-allocation recovery without spending another invocation."""

        if record.campaign_id is None or record.parent_operation_id is None:
            raise ValueError("a campaign recovery must name its campaign and exact parent")
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                campaign_row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?",
                    (record.campaign_id,),
                ).fetchone()
                if campaign_row is None:
                    raise KeyError(record.campaign_id)
                campaign = self._campaign_record(campaign_row)
                if campaign.status not in {"running", "stopping", "wrapping_up"}:
                    raise CampaignNotRunning(
                        "the campaign cannot recover an allocation after its ending is durable"
                    )
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
                if parent is None:
                    raise ValueError("campaign recovery parent is outside its exact lineage")
                if parent["status"] not in {"paused", "interrupted", "failed"}:
                    raise ValueError(
                        "only a paused, interrupted, or failed campaign task can recover"
                    )
                if record.project_id != parent["project_id"] or record.kind != parent["kind"]:
                    raise ValueError("campaign recovery must preserve its task scope")
                if record.attempt != int(parent["attempt"]) + 1:
                    raise ValueError("campaign recovery must advance its attempt lineage")
                if record.authorized_by != campaign.authorized_by:
                    raise ValueError("campaign tasks retain the root human authorizer snapshot")
                role = TypeAdapter(CampaignInvocationRole).validate_python(parent["campaign_role"])
                if (
                    role == "worker"
                    and parent["status"] != "paused"
                    and (
                        campaign.status != "running"
                        or campaign.ending is not None
                        or campaign.stop_requested_at is not None
                    )
                ):
                    raise CampaignNotRunning(
                        "the campaign is no longer accepting terminal worker recovery"
                    )
                if (
                    campaign.status == "wrapping_up"
                    and campaign.ending == "failed"
                    and role != "report"
                    and not (role == "worker" and parent["status"] == "paused")
                ):
                    raise CampaignNotRunning(
                        "the campaign terminal failure fence blocks operational recovery"
                    )
                clean_orchestrator_retry = (
                    role == "orchestrator"
                    and record.native_session_id is None
                    and record.request.get("session_id") is None
                )
                if clean_orchestrator_retry:
                    parent_request = json.loads(parent["request_json"])
                    parent_actor = (
                        parent_request.get("actor_operation_id") or parent["operation_id"]
                    )
                    if parent_actor != campaign.root_operation_id:
                        raise ValueError(
                            "only the sole orchestrator may restart a clean native session"
                        )
                    if (record.stage_host or "") != (
                        parent["stage_host"] or ""
                    ) or record.stage_root != parent["stage_root"]:
                        raise ValueError(
                            "a clean orchestrator retry must preserve its actor-owned stage"
                        )
                elif (
                    not parent["native_session_id"]
                    or not parent["stage_root"]
                    or record.native_session_id != parent["native_session_id"]
                    or (record.stage_host or "") != (parent["stage_host"] or "")
                    or record.stage_root != parent["stage_root"]
                    or record.request.get("session_id") != parent["native_session_id"]
                ):
                    raise ValueError(
                        "campaign recovery must preserve its exact saved native session and stage"
                    )
                child = connection.execute(
                    """
                    SELECT child.operation_id
                    FROM graph_runs AS parent
                    JOIN graph_runs AS child
                      ON child.parent_operation_id = parent.operation_id
                    WHERE parent.operation_id = ?
                      AND child.campaign_id = parent.campaign_id
                      AND child.attempt = parent.attempt + 1
                      AND COALESCE(
                          json_extract(child.request_json, '$.actor_operation_id'),
                          child.operation_id
                      ) = COALESCE(
                          json_extract(parent.request_json, '$.actor_operation_id'),
                          parent.operation_id
                      )
                    LIMIT 1
                    """,
                    (record.parent_operation_id,),
                ).fetchone()
                if child is not None:
                    raise ValueError("campaign task already has a recovery child")
                abandoned = connection.execute(
                    """
                    SELECT 1 FROM graph_run_receipts
                    WHERE operation_id = ? AND category = 'campaign_recovery_abandoned'
                    LIMIT 1
                    """,
                    (record.parent_operation_id,),
                ).fetchone()
                if abandoned is not None:
                    raise ValueError("campaign Stop already abandoned recovery of this task")
                if (
                    campaign.status == "wrapping_up"
                    and campaign.ending is not None
                    and role != "report"
                    and self._current_campaign_report_task_row(
                        connection,
                        campaign.campaign_id,
                        campaign.ending,
                    )
                    is not None
                ):
                    raise CampaignNotRunning(
                        "the campaign report already began; operational recovery is closed"
                    )
                self._bind_campaign_actor(
                    connection,
                    campaign,
                    record,
                    role,
                    same_allocation_recovery=True,
                )
                self._insert_agent_task(connection, record)
                connection.execute(
                    """
                    INSERT INTO campaign_invocations(campaign_id, operation_id, role, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (campaign.campaign_id, record.operation_id, role, record.created_at),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the campaign recovery task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def create_campaign_message_wake_task(
        self,
        record: AgentTaskRecord,
        *,
        role: Literal["orchestrator", "worker"],
        recipient_task_id: str,
        message_ids: list[str],
    ) -> AgentTaskRecord | None:
        """Spend one unit and claim one coalesced mail delivery in the same commit."""

        if record.campaign_id is None:
            raise ValueError("a campaign mail wake must carry its campaign id")
        if not recipient_task_id or not message_ids or len(message_ids) != len(set(message_ids)):
            raise ValueError("a campaign mail wake needs one recipient and unique messages")
        if len(message_ids) > CAMPAIGN_MAIL_MAX_MESSAGES:
            raise ValueError(
                f"a campaign mail wake may claim at most {CAMPAIGN_MAIL_MAX_MESSAGES} messages"
            )
        placeholders = ",".join("?" for _ in message_ids)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?",
                    (record.campaign_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(record.campaign_id)
                campaign = self._campaign_record(row)
                messages = connection.execute(
                    f"""
                    SELECT message_id, campaign_id, recipient_task_id,
                           delivered_at, delivery_operation_id
                    FROM campaign_messages
                    WHERE message_id IN ({placeholders})
                    """,
                    message_ids,
                ).fetchall()
                if {item["message_id"] for item in messages} != set(message_ids):
                    raise ValueError("campaign mail delivery names a missing message")
                if any(
                    item["campaign_id"] != record.campaign_id
                    or item["recipient_task_id"] != recipient_task_id
                    for item in messages
                ):
                    raise ValueError("campaign mail delivery crosses a campaign or recipient")
                if any(
                    item["delivered_at"] is not None or item["delivery_operation_id"] is not None
                    for item in messages
                ):
                    return None
                pending_prefix = connection.execute(
                    """
                    SELECT message_id
                    FROM campaign_messages
                    WHERE campaign_id = ? AND recipient_task_id = ?
                      AND delivered_at IS NULL AND delivery_operation_id IS NULL
                    ORDER BY created_at ASC, message_id ASC
                    LIMIT ?
                    """,
                    (record.campaign_id, recipient_task_id, len(message_ids)),
                ).fetchall()
                if [item["message_id"] for item in pending_prefix] != message_ids:
                    return None
                self._insert_campaign_task(connection, campaign, record, role)
                connection.execute(
                    f"""
                    UPDATE campaign_messages
                    SET delivered_at = ?, delivery_operation_id = ?
                    WHERE message_id IN ({placeholders})
                    """,
                    (record.created_at, record.operation_id, *message_ids),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the campaign mail wake task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    @staticmethod
    def _insert_campaign(connection: sqlite3.Connection, record: CampaignRecord) -> None:
        connection.execute(
            """
            INSERT INTO campaigns (
                campaign_id, project_id, root_operation_id, status, starting_instruction,
                invocation_ceiling, invocations_used, authorized_space_id,
                authorized_user_id, authorized_display_name, stop_requested_at,
                ending, error, created_at, updated_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.campaign_id,
                record.project_id,
                record.root_operation_id,
                record.status,
                record.starting_instruction,
                record.invocation_ceiling,
                record.invocations_used,
                record.authorized_by.space_id,
                record.authorized_by.user_id,
                record.authorized_by.display_name,
                record.stop_requested_at,
                record.ending,
                record.error,
                record.created_at,
                record.updated_at,
                record.ended_at,
            ),
        )

    def _insert_campaign_task(
        self,
        connection: sqlite3.Connection,
        campaign: CampaignRecord,
        record: AgentTaskRecord,
        role: CampaignInvocationRole,
    ) -> None:
        if record.campaign_id != campaign.campaign_id or record.project_id != campaign.project_id:
            raise ValueError("campaign task lineage does not match the campaign")
        if record.authorized_by != campaign.authorized_by:
            raise ValueError("campaign tasks retain the root human authorizer snapshot")
        if role == "report":
            if campaign.status != "wrapping_up" or campaign.ending is None:
                raise CampaignNotRunning("a report turn requires a campaign ending in progress")
            if campaign.invocations_used >= campaign.invocation_ceiling:
                raise CampaignBudgetExhausted("the reserved report invocation is unavailable")
            existing_report = self._current_campaign_report_task_row(
                connection,
                campaign.campaign_id,
                campaign.ending,
            )
            if existing_report is not None:
                raise ValueError("the campaign report invocation is already allocated")
        else:
            if campaign.status != "running" or campaign.stop_requested_at is not None:
                raise CampaignNotRunning("the campaign is not admitting new work")
            if campaign.invocations_used >= campaign.invocation_ceiling - 1:
                raise CampaignBudgetExhausted(
                    "the campaign budget is exhausted; one invocation remains reserved for its report"
                )
        if record.parent_operation_id is not None:
            parent = connection.execute(
                "SELECT project_id, campaign_id FROM graph_runs WHERE operation_id = ?",
                (record.parent_operation_id,),
            ).fetchone()
            if (
                parent is None
                or parent["project_id"] != campaign.project_id
                or parent["campaign_id"] != campaign.campaign_id
            ):
                raise ValueError("a campaign child task must keep its campaign lineage")
        self._bind_campaign_actor(connection, campaign, record, role)
        if self._has_active_chat_overlap(connection, record):
            raise ValueError("Another task is already active in this conversation.")
        self._insert_agent_task(connection, record)
        connection.execute(
            """
            INSERT INTO campaign_invocations(campaign_id, operation_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (campaign.campaign_id, record.operation_id, role, record.created_at),
        )
        cursor = connection.execute(
            """
            UPDATE campaigns
            SET invocations_used = invocations_used + 1, updated_at = ?
            WHERE campaign_id = ? AND invocations_used = ?
            """,
            (record.created_at, campaign.campaign_id, campaign.invocations_used),
        )
        if cursor.rowcount != 1:
            raise ValueError("the campaign budget changed during task admission")

    def _bind_campaign_actor(
        self,
        connection: sqlite3.Connection,
        campaign: CampaignRecord,
        record: AgentTaskRecord,
        role: CampaignInvocationRole,
        *,
        same_allocation_recovery: bool = False,
    ) -> str:
        """Validate and persist the immutable actor identity carried by a task request."""

        request = dict(record.request)
        if request.get("role") != role:
            raise ValueError("campaign task request role does not match its canonical role")
        requested_actor = request.get("actor_operation_id")
        if requested_actor is not None and (
            not isinstance(requested_actor, str) or not requested_actor.strip()
        ):
            raise ValueError("campaign actor operation id must be a nonblank string")
        if isinstance(requested_actor, str):
            requested_actor = requested_actor.strip()

        is_root = (
            record.operation_id == campaign.root_operation_id and record.parent_operation_id is None
        )
        if is_root:
            if role != "orchestrator":
                raise ValueError("the campaign root actor must be the orchestrator")
            if requested_actor not in {None, record.operation_id}:
                raise ValueError("the campaign root actor is its root operation")
            actor_operation_id = record.operation_id
            canonical_control_node_id = None
        else:
            if record.parent_operation_id is None:
                raise ValueError("a non-root campaign task must preserve parent lineage")
            parent = connection.execute(
                """
                SELECT run.*, invocation.role AS campaign_role
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ? AND run.campaign_id = ?
                """,
                (record.parent_operation_id, campaign.campaign_id),
            ).fetchone()
            if parent is None:
                raise ValueError("a campaign continuation has no canonical parent actor")
            parent_request = json.loads(parent["request_json"])
            parent_role = TypeAdapter(CampaignInvocationRole).validate_python(
                parent["campaign_role"]
            )
            parent_actor = parent_request.get("actor_operation_id")
            if not isinstance(parent_actor, str) or not parent_actor:
                # This fallback is migration-only. New rows always persist the
                # actor explicitly, but a pre-campaign-hardening root remains its
                # own canonical actor.
                parent_actor = str(parent["operation_id"])

            if role == "report":
                if requested_actor != campaign.root_operation_id:
                    raise ValueError(
                        "a campaign report must retain the sole orchestrator actor identity"
                    )
                if parent_role not in {"orchestrator", "report"}:
                    raise ValueError(
                        "a campaign report must continue the sole orchestrator's lineage"
                    )
                if parent_actor != campaign.root_operation_id:
                    raise ValueError(
                        "a campaign report parent must belong to the sole orchestrator actor"
                    )
                actor_operation_id = campaign.root_operation_id
                canonical_control_node_id = None
                latest = self._campaign_actor_latest_row(
                    connection,
                    campaign.campaign_id,
                    actor_operation_id,
                )
                if latest is None:
                    raise ValueError("a campaign report has no saved orchestrator actor binding")
                if (
                    record.native_session_id != latest["native_session_id"]
                    or (record.stage_host or "") != (latest["stage_host"] or "")
                    or record.stage_root != latest["stage_root"]
                ):
                    raise ValueError(
                        "a campaign report must preserve the orchestrator session and stage"
                    )
                request["actor_operation_id"] = actor_operation_id
                record.request = request
                return actor_operation_id
            elif requested_actor is None:
                if role == "orchestrator":
                    actor_operation_id = campaign.root_operation_id
                elif parent_role == role:
                    actor_operation_id = parent_actor
                else:
                    actor_operation_id = record.operation_id
            else:
                actor_operation_id = requested_actor
            if actor_operation_id is None:
                raise ValueError("campaign actor identity is unavailable")

            if actor_operation_id == record.operation_id:
                if request.get("wake_cause") is not None:
                    raise ValueError("a campaign wake must preserve an existing actor")
                if role == "orchestrator":
                    raise ValueError("a campaign may have only one orchestrator actor")
                if parent_role != "orchestrator":
                    raise ValueError("a new campaign actor must be seated by the orchestrator")
                canonical_control_node_id = request.get("control_node_id")
            else:
                actor = connection.execute(
                    """
                    SELECT run.*, invocation.role AS campaign_role
                    FROM graph_runs AS run
                    JOIN campaign_invocations AS invocation
                      ON invocation.operation_id = run.operation_id
                    WHERE run.operation_id = ? AND run.campaign_id = ?
                    """,
                    (actor_operation_id, campaign.campaign_id),
                ).fetchone()
                if actor is None:
                    raise ValueError("campaign continuation names an unknown actor")
                canonical_role = TypeAdapter(CampaignInvocationRole).validate_python(
                    actor["campaign_role"]
                )
                if (
                    canonical_role != role
                    or parent_role != role
                    or parent_actor != actor_operation_id
                ):
                    raise ValueError("campaign continuation cannot relabel or cross actor lineage")
                actor_request = json.loads(actor["request_json"])
                actor_identity = actor_request.get("actor_operation_id")
                if actor_identity not in {None, actor_operation_id}:
                    raise ValueError("campaign actor identity conflicts with its origin task")
                canonical_control_node_id = actor_request.get("control_node_id")
                if request.get("control_node_id") != canonical_control_node_id:
                    raise ValueError("campaign continuation cannot change its control seat")

                latest = self._campaign_actor_latest_row(
                    connection,
                    campaign.campaign_id,
                    actor_operation_id,
                )
                clean_orchestrator_retry = (
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
                    and (
                        latest["native_session_id"] is not None or latest["stage_root"] is not None
                    )
                    and not clean_orchestrator_retry
                    and (
                        record.native_session_id != latest["native_session_id"]
                        or (record.stage_host or "") != (latest["stage_host"] or "")
                        or record.stage_root != latest["stage_root"]
                    )
                ):
                    raise ValueError(
                        "campaign continuation must preserve its actor session and stage"
                    )

        if role == "orchestrator":
            if actor_operation_id != campaign.root_operation_id:
                raise ValueError("campaign continuation cannot replace the orchestrator actor")
            if request.get("control_node_id") is not None:
                raise ValueError("the campaign orchestrator has no worker control seat")
        elif role == "worker":
            if not isinstance(canonical_control_node_id, str) or not canonical_control_node_id:
                raise ValueError("a campaign worker must retain its control seat")
        elif request.get("control_node_id") is not None:
            raise ValueError("a campaign report has no worker control seat")

        request["actor_operation_id"] = actor_operation_id
        record.request = request
        unresolved = connection.execute(
            """
            SELECT run.operation_id
            FROM graph_runs AS run
            WHERE run.campaign_id = ?
              AND (
                  json_extract(run.request_json, '$.actor_operation_id') = ?
                  OR (
                      run.operation_id = ?
                      AND json_extract(run.request_json, '$.actor_operation_id') IS NULL
                  )
              )
              AND (
                  run.status IN ('queued', 'running', 'pausing')
                  OR (
                      run.status IN ('paused', 'interrupted', 'failed')
                      AND (? = 0 OR run.operation_id != ?)
                      AND NOT EXISTS (
                          SELECT 1 FROM graph_run_receipts AS receipt
                          WHERE receipt.operation_id = run.operation_id
                            AND receipt.category = 'campaign_recovery_abandoned'
                      )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM graph_runs AS child
                  WHERE child.parent_operation_id = run.operation_id
                    AND child.campaign_id = run.campaign_id
                    AND child.attempt = run.attempt + 1
                    AND COALESCE(
                        json_extract(child.request_json, '$.actor_operation_id'),
                        child.operation_id
                    ) = COALESCE(
                        json_extract(run.request_json, '$.actor_operation_id'),
                        run.operation_id
                    )
              )
            ORDER BY run.rowid DESC
            LIMIT 1
            """,
            (
                campaign.campaign_id,
                actor_operation_id,
                actor_operation_id,
                int(same_allocation_recovery),
                record.parent_operation_id or "",
            ),
        ).fetchone()
        if unresolved is not None:
            raise CampaignActorBusy(actor_operation_id, str(unresolved["operation_id"]))
        return actor_operation_id

    @staticmethod
    def _campaign_actor_latest_row(
        connection: sqlite3.Connection,
        campaign_id: str,
        actor_operation_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT * FROM graph_runs
            WHERE campaign_id = ?
              AND (
                  json_extract(request_json, '$.actor_operation_id') = ?
                  OR (
                      operation_id = ?
                      AND json_extract(request_json, '$.actor_operation_id') IS NULL
                  )
              )
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (campaign_id, actor_operation_id, actor_operation_id),
        ).fetchone()

    @staticmethod
    def _current_campaign_report_task_row(
        connection: sqlite3.Connection,
        campaign_id: str,
        ending: CampaignEnding,
    ) -> sqlite3.Row | None:
        """Return the newest report attempt created after the last durable report."""

        return connection.execute(
            """
            SELECT run.*
            FROM graph_runs AS run
            JOIN campaign_invocations AS invocation
              ON invocation.operation_id = run.operation_id
            WHERE run.campaign_id = ? AND invocation.role = 'report'
              AND json_extract(run.request_json, '$.ending') = ?
              AND run.rowid > COALESCE((
                  SELECT MAX(completed_run.rowid)
                  FROM campaign_reports AS report
                  JOIN graph_runs AS completed_run
                    ON completed_run.operation_id = report.operation_id
                  WHERE report.campaign_id = ?
              ), 0)
            ORDER BY run.rowid DESC
            LIMIT 1
            """,
            (campaign_id, ending, campaign_id),
        ).fetchone()

    @staticmethod
    def _campaign_non_report_turns_settled(
        connection: sqlite3.Connection,
        campaign_id: str,
    ) -> bool:
        rows = connection.execute(
            """
            SELECT run.operation_id, run.status, invocation.role,
                   campaign.status AS campaign_status,
                   campaign.ending AS campaign_ending,
                   EXISTS (
                       SELECT 1 FROM graph_runs AS child
                       WHERE child.parent_operation_id = run.operation_id
                         AND child.campaign_id = run.campaign_id
                         AND child.attempt = run.attempt + 1
                         AND COALESCE(
                             json_extract(child.request_json, '$.actor_operation_id'),
                             child.operation_id
                         ) = COALESCE(
                             json_extract(run.request_json, '$.actor_operation_id'),
                             run.operation_id
                         )
                   ) AS has_recovery_child,
                   EXISTS (
                       SELECT 1 FROM graph_run_receipts AS receipt
                       WHERE receipt.operation_id = run.operation_id
                         AND receipt.category = 'campaign_recovery_abandoned'
                   ) AS recovery_abandoned,
                   EXISTS (
                       SELECT 1 FROM graph_run_receipts AS receipt
                       WHERE receipt.operation_id = run.operation_id
                         AND receipt.category = 'campaign_orchestrator_failure'
                         AND json_extract(receipt.payload_json, '$.classification') =
                             'structural_unrecoverable'
                         AND json_extract(receipt.payload_json, '$.recoverable') = 0
                   ) AS structural_terminal_failure,
                   (
                       SELECT recovery.status
                       FROM campaign_recoveries AS recovery
                       WHERE recovery.campaign_id = run.campaign_id
                         AND recovery.purpose = 'task'
                         AND (
                             recovery.operation_id = run.operation_id
                             OR recovery.admitted_operation_id = run.operation_id
                         )
                       ORDER BY recovery.updated_at DESC, recovery.recovery_id DESC
                       LIMIT 1
                   ) AS recovery_status
            FROM graph_runs AS run
            JOIN campaign_invocations AS invocation
              ON invocation.operation_id = run.operation_id
            JOIN campaigns AS campaign ON campaign.campaign_id = run.campaign_id
            WHERE run.campaign_id = ?
              AND (
                  invocation.role != 'report'
                  OR run.rowid > COALESCE((
                      SELECT MAX(completed_run.rowid)
                      FROM campaign_reports AS report
                      JOIN graph_runs AS completed_run
                        ON completed_run.operation_id = report.operation_id
                      WHERE report.campaign_id = run.campaign_id
                  ), 0)
              )
            """,
            (campaign_id,),
        ).fetchall()
        for row in rows:
            if row["has_recovery_child"] or row["recovery_abandoned"]:
                continue
            status = str(row["status"])
            if status in {"queued", "running", "pausing", "paused"}:
                return False
            if status not in {"failed", "interrupted"} or row["role"] == "worker":
                continue
            if (
                row["role"] == "orchestrator"
                and row["campaign_status"] == "wrapping_up"
                and row["campaign_ending"] == "failed"
                and row["structural_terminal_failure"]
            ):
                continue
            if row["recovery_status"] in {"blocked", "exhausted"}:
                continue
            # A recoverable orchestrator/report leaf with no durable terminal
            # recovery decision is a crash window, not settled work. In
            # particular, an admitted child that failed before its next recovery
            # record was scheduled must still hold the report fence closed.
            return False
        return True

    def campaign(self, campaign_id: str) -> CampaignRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        return self._campaign_record(row) if row else None

    def active_campaign(self, project_id: str) -> CampaignRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM campaigns
                WHERE project_id = ?
                  AND status IN ('queued', 'running', 'stopping', 'wrapping_up', 'needs_action')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        return self._campaign_record(row) if row else None

    def campaigns(self, project_id: str, *, limit: int = 50) -> list[CampaignRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM campaigns
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._campaign_record(row) for row in rows]

    def campaigns_awaiting_report(self) -> list[CampaignRecord]:
        """Return fenced endings for restart-safe report reconciliation."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM campaigns
                WHERE status = 'wrapping_up' AND ending IS NOT NULL
                ORDER BY updated_at ASC, campaign_id ASC
                """
            ).fetchall()
        return [self._campaign_record(row) for row in rows]

    def schedule_campaign_task_recovery(
        self,
        operation_id: str,
        *,
        failure_kind: str,
        retry_mode: CampaignRecoveryMode,
        diagnostic: str,
        max_attempts: int = 3,
    ) -> CampaignRecoveryRecord:
        """Persist one bounded same-allocation recovery decision idempotently."""

        if max_attempts < 1:
            raise ValueError("campaign recovery max attempts must be positive")
        detail = " ".join(diagnostic.split())[:2000] or "Campaign task recovery is required."
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT campaign_id, attempt, parent_operation_id, request_json "
                "FROM graph_runs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["campaign_id"] is None:
                raise ValueError("campaign recovery requires a campaign task")
            allocation_operation_id = operation_id
            actor_operation_id = json.loads(row["request_json"]).get("actor_operation_id")
            ancestor = row
            while int(ancestor["attempt"]) > 1 and ancestor["parent_operation_id"]:
                parent = connection.execute(
                    "SELECT operation_id, campaign_id, attempt, parent_operation_id, request_json "
                    "FROM graph_runs WHERE operation_id = ?",
                    (ancestor["parent_operation_id"],),
                ).fetchone()
                if parent is None or parent["campaign_id"] != row["campaign_id"]:
                    break
                parent_actor = json.loads(parent["request_json"]).get("actor_operation_id")
                if parent_actor != actor_operation_id:
                    break
                allocation_operation_id = str(parent["operation_id"])
                ancestor = parent
            recovery_id = f"task:{allocation_operation_id}"
            existing = connection.execute(
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if existing is None:
                attempts = 0
                status: CampaignRecoveryStatus = "blocked" if retry_mode == "blocked" else "pending"
                next_attempt_at = (
                    self._campaign_recovery_next_attempt_at(now, attempts)
                    if status == "pending"
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO campaign_recoveries (
                        recovery_id, campaign_id, operation_id, purpose, failure_kind,
                        retry_mode, attempts, max_attempts, status, next_attempt_at,
                        diagnostic, admitted_operation_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'task', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        recovery_id,
                        row["campaign_id"],
                        operation_id,
                        failure_kind,
                        retry_mode,
                        attempts,
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
                    # The spawned child settled before its admission receipt was stored.
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
                    self._campaign_recovery_next_attempt_at(now, attempts)
                    if status == "pending"
                    else None
                )
                connection.execute(
                    """
                    UPDATE campaign_recoveries
                    SET operation_id = ?, failure_kind = ?, retry_mode = ?, attempts = ?,
                        max_attempts = ?, status = ?, next_attempt_at = ?, diagnostic = ?,
                        admitted_operation_id = CASE
                            WHEN ? = 'pending' THEN NULL
                            ELSE admitted_operation_id
                        END,
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
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        assert stored is not None
        return self._campaign_recovery_record(stored)

    def schedule_campaign_report_reconciliation(
        self,
        campaign_id: str,
        *,
        ending: CampaignEnding,
        diagnostic: str,
        max_attempts: int = 8,
    ) -> CampaignRecoveryRecord:
        """Persist the mandatory report's unbounded, restart-safe admission retry."""

        detail = " ".join(diagnostic.split())[:2000] or "Campaign report admission failed."
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT status, ending FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            if campaign["status"] != "wrapping_up" or campaign["ending"] != ending:
                raise ValueError("campaign report retry does not match its active ending")
            recovery_id = self._campaign_report_recovery_id(connection, campaign_id, ending)
            connection.execute(
                """
                INSERT INTO campaign_recoveries (
                    recovery_id, campaign_id, operation_id, purpose, failure_kind,
                    retry_mode, attempts, max_attempts, status, next_attempt_at,
                    diagnostic, admitted_operation_id, created_at, updated_at
                ) VALUES (?, ?, NULL, 'report_admission', 'report_admission',
                          'report_admission', 0, ?, 'pending', ?, ?, NULL, ?, ?)
                ON CONFLICT(recovery_id) DO UPDATE SET
                    diagnostic = excluded.diagnostic,
                    status = CASE
                        WHEN campaign_recoveries.purpose = 'report_admission'
                         AND campaign_recoveries.status = 'exhausted'
                        THEN 'pending'
                        ELSE campaign_recoveries.status
                    END,
                    next_attempt_at = CASE
                        WHEN campaign_recoveries.purpose = 'report_admission'
                         AND campaign_recoveries.status = 'exhausted'
                        THEN excluded.next_attempt_at
                        WHEN campaign_recoveries.status = 'pending'
                        THEN COALESCE(campaign_recoveries.next_attempt_at, excluded.next_attempt_at)
                        ELSE campaign_recoveries.next_attempt_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    recovery_id,
                    campaign_id,
                    max_attempts,
                    (
                        self._parse_time(now) + timedelta(seconds=WATCHER_ERROR_BACKOFF_SECONDS[0])
                    ).isoformat(),
                    detail,
                    now,
                    now,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
        assert stored is not None
        return self._campaign_recovery_record(stored)

    @staticmethod
    def _campaign_report_recovery_id(
        connection: sqlite3.Connection,
        campaign_id: str,
        ending: CampaignEnding,
    ) -> str:
        """Key one admission recovery to its immutable report generation."""

        completed_reports = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM campaign_reports WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()["count"]
        )
        return f"report:{campaign_id}:{completed_reports + 1}:{ending}"

    def due_campaign_recoveries(self, *, as_of: str | None = None) -> list[CampaignRecoveryRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT recovery.*
                FROM campaign_recoveries AS recovery
                JOIN campaigns AS campaign ON campaign.campaign_id = recovery.campaign_id
                WHERE recovery.status = 'pending' AND recovery.next_attempt_at <= ?
                  AND campaign.status IN ('running', 'stopping', 'wrapping_up')
                ORDER BY recovery.next_attempt_at, recovery.created_at, recovery.recovery_id
                """,
                (as_of or self.now(),),
            ).fetchall()
        return [self._campaign_recovery_record(row) for row in rows]

    def campaign_recovery(self, recovery_id: str) -> CampaignRecoveryRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?", (recovery_id,)
            ).fetchone()
        return self._campaign_recovery_record(row) if row is not None else None

    def campaign_control_recovery(
        self,
        campaign_id: str,
        operation_id: str | None,
        *,
        ending: CampaignEnding | None = None,
    ) -> CampaignRecoveryRecord | None:
        """Return only the durable recovery state governing campaign-parent controls."""

        with self.connection() as connection:
            if operation_id is None and ending is not None:
                recovery_id = self._campaign_report_recovery_id(
                    connection,
                    campaign_id,
                    ending,
                )
                row = connection.execute(
                    """
                    SELECT * FROM campaign_recoveries
                    WHERE recovery_id = ? AND campaign_id = ?
                      AND purpose = 'report_admission'
                    """,
                    (recovery_id, campaign_id),
                ).fetchone()
            elif operation_id is not None:
                row = connection.execute(
                    """
                    SELECT * FROM campaign_recoveries
                    WHERE campaign_id = ? AND purpose = 'task'
                      AND (operation_id = ? OR admitted_operation_id = ?)
                    ORDER BY updated_at DESC, recovery_id DESC
                    LIMIT 1
                    """,
                    (campaign_id, operation_id, operation_id),
                ).fetchone()
            else:
                row = None
        return self._campaign_recovery_record(row) if row is not None else None

    def campaign_task_recovery_child(self, operation_id: str) -> AgentTaskRecord | None:
        """Return the exact same-actor attempt+1 child, if one is already admitted."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT child.*
                FROM graph_runs AS parent
                JOIN graph_runs AS child
                  ON child.parent_operation_id = parent.operation_id
                WHERE parent.operation_id = ?
                  AND child.campaign_id = parent.campaign_id
                  AND child.attempt = parent.attempt + 1
                  AND COALESCE(
                      json_extract(child.request_json, '$.actor_operation_id'),
                      child.operation_id
                  ) = COALESCE(
                      json_extract(parent.request_json, '$.actor_operation_id'),
                      parent.operation_id
                  )
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        return self._agent_task_record(row) if row is not None else None

    def complete_campaign_recovery(
        self,
        recovery_id: str,
        *,
        admitted_operation_id: str | None = None,
        expected_operation_id: str | None = None,
    ) -> CampaignRecoveryRecord:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE campaign_recoveries
                SET attempts = attempts + 1, status = 'admitted', next_attempt_at = NULL,
                    admitted_operation_id = COALESCE(?, admitted_operation_id), updated_at = ?
                WHERE recovery_id = ? AND status = 'pending'
                  AND (purpose = 'report_admission' OR attempts < max_attempts)
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
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?", (recovery_id,)
            ).fetchone()
            if row is None:
                raise KeyError(recovery_id)
            if (
                updated != 1
                and row["status"] != "admitted"
                and row["operation_id"] == expected_operation_id
            ):
                raise ValueError("campaign recovery is no longer pending")
        return self._campaign_recovery_record(row)

    def defer_campaign_recovery(
        self,
        recovery_id: str,
        *,
        diagnostic: str,
    ) -> CampaignRecoveryRecord:
        now = self.now()
        detail = " ".join(diagnostic.split())[:2000] or "Campaign recovery attempt failed."
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?", (recovery_id,)
            ).fetchone()
            if row is None:
                raise KeyError(recovery_id)
            if row["status"] != "pending":
                return self._campaign_recovery_record(row)
            attempts = int(row["attempts"]) + 1
            exhausted = row["purpose"] != "report_admission" and attempts >= int(
                row["max_attempts"]
            )
            next_attempt_at = None
            if not exhausted:
                next_attempt_at = self._campaign_recovery_next_attempt_at(now, attempts)
            connection.execute(
                """
                UPDATE campaign_recoveries
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
                "SELECT * FROM campaign_recoveries WHERE recovery_id = ?", (recovery_id,)
            ).fetchone()
        assert stored is not None
        return self._campaign_recovery_record(stored)

    def _campaign_recovery_next_attempt_at(self, now: str, attempts: int) -> str:
        parsed = self._parse_time(now)
        assert parsed is not None
        delay = WATCHER_ERROR_BACKOFF_SECONDS[min(attempts, len(WATCHER_ERROR_BACKOFF_SECONDS) - 1)]
        return (parsed + timedelta(seconds=delay)).isoformat()

    def campaign_tasks(self, campaign_id: str) -> list[AgentTaskRecord]:
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
                WHERE campaign_id = ?
                ORDER BY created_at ASC, operation_id ASC
                """,
                (campaign_id,),
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def campaign_recovery_candidates(self) -> list[AgentTaskRecord]:
        """Return current failed/interrupted campaign actor leaves lacking a recovery decision."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT run.*
                FROM graph_runs AS run
                JOIN campaigns AS campaign ON campaign.campaign_id = run.campaign_id
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.status IN ('failed', 'interrupted')
                  AND invocation.role IN ('orchestrator', 'report')
                  AND campaign.status IN ('running', 'stopping', 'wrapping_up')
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_runs AS child
                      WHERE child.parent_operation_id = run.operation_id
                        AND child.campaign_id = run.campaign_id
                        AND child.attempt = run.attempt + 1
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM campaign_recoveries AS recovery
                      WHERE recovery.operation_id = run.operation_id
                  )
                ORDER BY run.created_at, run.operation_id
                """
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def campaign_report_task_history(
        self,
        campaign_id: str,
        *,
        limit: int,
    ) -> tuple[int, dict[str, int], dict[str, int], list[AgentTaskRecord]]:
        """Count every campaign turn while loading only the root and newest rows."""

        if limit < 1:
            raise ValueError("campaign report task history limit must be positive")
        with self.connection() as connection:
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM graph_runs
                WHERE campaign_id = ?
                GROUP BY status
                ORDER BY status
                """,
                (campaign_id,),
            ).fetchall()
            role_rows = connection.execute(
                """
                SELECT COALESCE(invocation.role, 'unknown') AS role, COUNT(*) AS count
                FROM graph_runs AS run
                LEFT JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.campaign_id = ?
                GROUP BY COALESCE(invocation.role, 'unknown')
                ORDER BY role
                """,
                (campaign_id,),
            ).fetchall()
            campaign = connection.execute(
                "SELECT root_operation_id FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            root_operation_id = campaign["root_operation_id"]
            root = (
                connection.execute(
                    """
                    SELECT run.*,
                           EXISTS (
                               SELECT 1 FROM graph_run_receipts AS receipt
                               WHERE receipt.operation_id = run.operation_id
                                 AND receipt.category IN (
                                     'experiment_recovery_abandoned',
                                     'campaign_recovery_abandoned'
                                 )
                           ) AS recovery_abandoned
                    FROM graph_runs AS run
                    WHERE run.operation_id = ? AND run.campaign_id = ?
                    """,
                    (root_operation_id, campaign_id),
                ).fetchone()
                if root_operation_id is not None
                else None
            )
            newest = connection.execute(
                """
                SELECT run.*,
                       EXISTS (
                           SELECT 1 FROM graph_run_receipts AS receipt
                           WHERE receipt.operation_id = run.operation_id
                             AND receipt.category IN (
                                 'experiment_recovery_abandoned',
                                 'campaign_recovery_abandoned'
                             )
                       ) AS recovery_abandoned
                FROM graph_runs AS run
                WHERE run.campaign_id = ? AND run.operation_id != COALESCE(?, '')
                ORDER BY run.created_at DESC, run.operation_id DESC
                LIMIT ?
                """,
                (campaign_id, root_operation_id, max(0, limit - (1 if root else 0))),
            ).fetchall()
        status_counts = {str(row["status"]): int(row["count"]) for row in status_rows}
        role_counts = {str(row["role"]): int(row["count"]) for row in role_rows}
        total = sum(status_counts.values())
        selected = ([root] if root is not None else []) + list(reversed(newest))
        return total, status_counts, role_counts, [self._agent_task_record(row) for row in selected]

    def campaign_report_event_history(
        self,
        campaign_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[AgentTaskEventRecord]]:
        """Return an exact event count and the newest bounded campaign event suffix."""

        if limit < 1:
            raise ValueError("campaign report event history limit must be positive")
        with self.connection() as connection:
            total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM graph_run_events AS event
                    JOIN graph_runs AS run ON run.operation_id = event.operation_id
                    WHERE run.campaign_id = ?
                    """,
                    (campaign_id,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT event.*
                FROM graph_run_events AS event
                JOIN graph_runs AS run ON run.operation_id = event.operation_id
                WHERE run.campaign_id = ?
                ORDER BY event.event_id DESC
                LIMIT ?
                """,
                (campaign_id, limit),
            ).fetchall()
        return total, [self._agent_task_event_record(row) for row in reversed(rows)]

    def campaign_invocation_role(self, operation_id: str) -> CampaignInvocationRole | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT role FROM campaign_invocations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return TypeAdapter(CampaignInvocationRole).validate_python(row["role"])

    def campaign_actor_binding(self, operation_id: str) -> CampaignActorBinding:
        """Resolve one task to its immutable actor and newest same-actor continuation."""

        with self.connection() as connection:
            task = connection.execute(
                """
                SELECT run.*, invocation.role AS campaign_role,
                       campaign.root_operation_id AS campaign_root_operation_id
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                JOIN campaigns AS campaign ON campaign.campaign_id = run.campaign_id
                WHERE run.operation_id = ? AND run.campaign_id IS NOT NULL
                """,
                (operation_id,),
            ).fetchone()
            if task is None:
                raise KeyError(operation_id)
            request = json.loads(task["request_json"])
            actor_operation_id = request.get("actor_operation_id")
            if not isinstance(actor_operation_id, str) or not actor_operation_id:
                actor_operation_id = str(task["operation_id"])
            actor = connection.execute(
                """
                SELECT run.request_json, invocation.role AS campaign_role
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ? AND run.campaign_id = ?
                """,
                (actor_operation_id, task["campaign_id"]),
            ).fetchone()
            if actor is None:
                raise ValueError("campaign task has no canonical actor origin")
            task_role = TypeAdapter(CampaignInvocationRole).validate_python(task["campaign_role"])
            role = TypeAdapter(CampaignInvocationRole).validate_python(actor["campaign_role"])
            if task_role == "report":
                if (
                    actor_operation_id != task["campaign_root_operation_id"]
                    or role != "orchestrator"
                ):
                    raise ValueError("campaign report must bind to its sole orchestrator actor")
            elif role != task_role:
                raise ValueError("campaign task role conflicts with its canonical actor")
            actor_request = json.loads(actor["request_json"])
            latest = connection.execute(
                """
                SELECT run.*
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.campaign_id = ? AND invocation.role = ?
                  AND (
                      json_extract(run.request_json, '$.actor_operation_id') = ?
                      OR (
                          run.operation_id = ?
                          AND json_extract(run.request_json, '$.actor_operation_id') IS NULL
                      )
                  )
                ORDER BY run.rowid DESC
                LIMIT 1
                """,
                (str(task["campaign_id"]), role, actor_operation_id, actor_operation_id),
            ).fetchone()
            assert latest is not None
        return CampaignActorBinding(
            campaign_id=str(task["campaign_id"]),
            actor_operation_id=actor_operation_id,
            role=role,
            control_node_id=actor_request.get("control_node_id"),
            current_operation_id=str(latest["operation_id"]),
            native_session_id=latest["native_session_id"],
            stage_host=latest["stage_host"],
            stage_root=latest["stage_root"],
        )

    def campaign_handoffs_cleared(self, operation_id: str) -> bool:
        """Return the durable clear fence for one paid campaign actor allocation."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT run.campaign_id, run.kind, run.attempt,
                       run.campaign_worker_handoffs_cleared_at, invocation.role
                FROM graph_runs AS run
                LEFT JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        self._require_campaign_handoff_allocation(row)
        return row["campaign_worker_handoffs_cleared_at"] is not None

    def mark_campaign_handoffs_cleared(self, operation_id: str) -> None:
        """Fence one paid actor allocation after all prior handoffs were cleared."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run.campaign_id, run.kind, run.attempt, invocation.role
                FROM graph_runs AS run
                LEFT JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                WHERE run.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            self._require_campaign_handoff_allocation(row)
            connection.execute(
                """
                UPDATE graph_runs
                SET campaign_worker_handoffs_cleared_at = COALESCE(
                        campaign_worker_handoffs_cleared_at, ?
                    )
                WHERE operation_id = ?
                """,
                (now, operation_id),
            )

    def campaign_worker_handoffs_cleared(self, operation_id: str) -> bool:
        """Compatibility name for the generalized campaign-allocation fence."""

        return self.campaign_handoffs_cleared(operation_id)

    def mark_campaign_worker_handoffs_cleared(self, operation_id: str) -> None:
        """Compatibility name for the generalized campaign-allocation fence."""

        self.mark_campaign_handoffs_cleared(operation_id)

    @staticmethod
    def _require_campaign_handoff_allocation(row: sqlite3.Row) -> None:
        role = row["role"]
        if (
            row["campaign_id"] is None
            or row["kind"] != "campaign"
            or role not in {"orchestrator", "worker"}
            or int(row["attempt"]) != 1
        ):
            raise ValueError(
                "handoff clearing requires a paid orchestrator or worker campaign allocation"
            )

    def campaign_budget_meter(self, campaign_id: str) -> CampaignBudgetMeter:
        with self.connection() as connection:
            campaign = connection.execute(
                "SELECT invocation_ceiling, invocations_used FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            usage_rows = connection.execute(
                """
                SELECT usage.*
                FROM agent_usage AS usage
                JOIN graph_runs AS run ON run.operation_id = usage.operation_id
                WHERE run.campaign_id = ?
                ORDER BY usage.created_at ASC, usage.usage_id ASC
                """,
                (campaign_id,),
            ).fetchall()
        usage_records = [self._agent_usage_record(row) for row in usage_rows]
        input_processed, generated, _, _ = self._agent_usage_metrics(usage_records)
        ceiling = int(campaign["invocation_ceiling"])
        used = int(campaign["invocations_used"])
        return CampaignBudgetMeter(
            invocation_ceiling=ceiling,
            invocations_used=used,
            invocations_remaining=max(0, ceiling - used),
            observed_input_tokens=input_processed.total_tokens,
            observed_generated_tokens=generated.total_tokens,
        )

    def fence_campaign_exhaustion_if_depleted(self, campaign_id: str) -> CampaignRecord:
        """Atomically fence a depleted research pot while preserving its report unit."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            campaign = self._campaign_record(row)
            if (
                campaign.status == "running"
                and campaign.stop_requested_at is None
                and campaign.ending is None
                and campaign.invocations_used >= campaign.invocation_ceiling - 1
                and self._campaign_non_report_turns_settled(connection, campaign_id)
            ):
                updated = connection.execute(
                    """
                    UPDATE campaigns
                    SET status = 'wrapping_up', ending = 'exhausted', updated_at = ?
                    WHERE campaign_id = ? AND status = 'running'
                      AND stop_requested_at IS NULL
                      AND ending IS NULL
                      AND invocations_used >= invocation_ceiling - 1
                    """,
                    (now, campaign_id),
                ).rowcount
                if updated == 1:
                    self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
            stored = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        assert stored is not None
        return self._campaign_record(stored)

    def request_campaign_stop(self, campaign_id: str) -> CampaignRecord:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            campaign = self._campaign_record(row)
            if campaign.stop_requested_at is not None:
                if campaign.status == "stopping":
                    self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
                    self._settle_campaign_stop(connection, campaign_id)
                stored_row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
                ).fetchone()
                assert stored_row is not None
                return self._campaign_record(stored_row)
            if campaign.status not in {"queued", "running"}:
                raise CampaignNotRunning(
                    "the campaign ending is already durable; Stop was not recorded"
                )
            connection.execute(
                """
                UPDATE campaigns
                SET stop_requested_at = COALESCE(stop_requested_at, ?),
                    status = 'stopping', updated_at = ?
                WHERE campaign_id = ? AND status IN ('queued', 'running')
                """,
                (now, now, campaign_id),
            )
            self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
            self._settle_campaign_stop(connection, campaign_id)
        stored = self.campaign(campaign_id)
        assert stored is not None
        return stored

    def settle_campaign_stop(self, campaign_id: str) -> CampaignRecord:
        """Reconcile one durable Stop after its current/recoverable leaves settle."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            self._settle_campaign_stop(connection, campaign_id)
        stored = self.campaign(campaign_id)
        assert stored is not None
        return stored

    def settle_ready_campaign_stops(self) -> int:
        """Startup/background sweep for every persisted campaign Stop intent."""

        settled = 0
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT campaign_id FROM campaigns
                WHERE stop_requested_at IS NOT NULL AND status = 'stopping'
                ORDER BY created_at, campaign_id
                """
            ).fetchall()
            for row in rows:
                if self._settle_campaign_stop(connection, str(row["campaign_id"])):
                    settled += 1
        return settled

    def abandon_campaign_recovery(
        self,
        operation_id: str,
        *,
        diagnostic: str,
    ) -> AgentTaskRecord:
        """Durably abandon only unusable recovery of one stopped campaign leaf."""

        detail = " ".join(diagnostic.split())[:2000]
        if not detail:
            raise ValueError("campaign recovery abandonment requires an exact diagnostic")
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run.* FROM graph_runs AS run
                JOIN campaigns AS campaign ON campaign.campaign_id = run.campaign_id
                WHERE run.operation_id = ? AND campaign.stop_requested_at IS NOT NULL
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise ValueError("campaign recovery abandonment requires a stopped campaign task")
            if row["status"] not in {"paused", "interrupted", "failed"}:
                raise ValueError("only a recoverable terminal campaign leaf may be abandoned")
            child = connection.execute(
                """
                SELECT 1
                FROM graph_runs AS parent
                JOIN graph_runs AS child
                  ON child.parent_operation_id = parent.operation_id
                WHERE parent.operation_id = ?
                  AND child.campaign_id = parent.campaign_id
                  AND child.attempt = parent.attempt + 1
                  AND COALESCE(
                      json_extract(child.request_json, '$.actor_operation_id'),
                      child.operation_id
                  ) = COALESCE(
                      json_extract(parent.request_json, '$.actor_operation_id'),
                      parent.operation_id
                  )
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if child is not None:
                raise ValueError("campaign recovery abandonment requires the current leaf")
            existing = connection.execute(
                """
                SELECT 1 FROM graph_run_receipts
                WHERE operation_id = ? AND category = 'campaign_recovery_abandoned'
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
            if existing is None:
                self._insert_agent_task_receipt(
                    connection,
                    operation_id,
                    "campaign_recovery_abandoned",
                    self._bounded_receipt_payload(
                        {"campaign_id": row["campaign_id"], "reason": detail}
                    ),
                    tier="summary",
                    created_at=now,
                )
                self._insert_agent_task_event(
                    connection,
                    operation_id,
                    "Campaign Stop abandoned recovery of this terminal task because its saved "
                    "session cannot be continued. The task and its history remain inspectable.",
                    level="warning",
                    created_at=now,
                )
            self._settle_campaign_stop(connection, str(row["campaign_id"]))
        stored = self.agent_task(operation_id)
        assert stored is not None
        return stored

    def _settle_campaign_stop(
        self,
        connection: sqlite3.Connection,
        campaign_id: str,
    ) -> bool:
        campaign_row = connection.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if campaign_row is None or campaign_row["stop_requested_at"] is None:
            return False
        if campaign_row["status"] != "stopping":
            return False
        now = self.now()
        self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
        unresolved = connection.execute(
            """
            SELECT run.operation_id
            FROM graph_runs AS run
            JOIN campaign_invocations AS invocation
              ON invocation.operation_id = run.operation_id
            WHERE run.campaign_id = ?
              AND (
                  run.status IN ('queued', 'running', 'pausing', 'paused')
                  OR (
                      run.status IN ('failed', 'interrupted')
                      AND invocation.role IN ('orchestrator', 'report')
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM graph_runs AS child
                  WHERE child.parent_operation_id = run.operation_id
                    AND child.campaign_id = run.campaign_id
                    AND child.attempt = run.attempt + 1
                    AND COALESCE(
                        json_extract(child.request_json, '$.actor_operation_id'),
                        child.operation_id
                    ) = COALESCE(
                        json_extract(run.request_json, '$.actor_operation_id'),
                        run.operation_id
                    )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM graph_run_receipts AS receipt
                  WHERE receipt.operation_id = run.operation_id
                    AND receipt.category = 'campaign_recovery_abandoned'
              )
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        if unresolved is not None:
            return False
        connection.execute(
            """
            UPDATE watchers
            SET status = 'stopped', notified = 1, next_check_at = NULL,
                stopped_by = COALESCE(stopped_by, 'loop'),
                stopped_at = COALESCE(stopped_at, ?)
            WHERE (
                origin_operation_id IN (
                    SELECT operation_id FROM graph_runs WHERE campaign_id = ?
                )
                OR notification_operation_id IN (
                    SELECT operation_id FROM graph_runs WHERE campaign_id = ?
                )
            )
            """,
            (now, campaign_id, campaign_id),
        )
        connection.execute(
            """
            UPDATE campaigns
            SET status = 'wrapping_up', ending = 'stopped', updated_at = ?
            WHERE campaign_id = ? AND status = 'stopping'
            """,
            (now, campaign_id),
        )
        return True

    @staticmethod
    def _stop_unclaimed_campaign_watchers(
        connection: sqlite3.Connection,
        campaign_id: str,
        stopped_at: str,
    ) -> None:
        connection.execute(
            """
            UPDATE watchers
            SET status = 'stopped', notified = 1, next_check_at = NULL,
                stopped_by = COALESCE(stopped_by, 'loop'),
                stopped_at = COALESCE(stopped_at, ?)
            WHERE origin_operation_id IN (
                SELECT operation_id FROM graph_runs WHERE campaign_id = ?
            )
              AND status IN ('active', 'degraded', 'completed')
              AND notified = 0 AND notification_operation_id IS NULL
            """,
            (stopped_at, campaign_id),
        )

    def begin_campaign_wrapup(
        self,
        campaign_id: str,
        ending: CampaignEnding,
        *,
        error: str | None = None,
    ) -> CampaignRecord:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            current = self._campaign_record(row)
            if current.status == "wrapping_up":
                if current.ending != ending:
                    raise ValueError("campaign wrap-up already has a different ending")
                return current
            if current.status in {"succeeded", "stopped", "failed", "needs_action"}:
                if current.ending != ending:
                    raise ValueError("campaign already ended differently")
                return current
            if current.stop_requested_at is not None and ending != "stopped":
                raise ValueError("a stopped campaign must wrap up as stopped")
            self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
            connection.execute(
                """
                UPDATE campaigns
                SET status = 'wrapping_up', ending = ?, error = ?, updated_at = ?
                WHERE campaign_id = ?
                """,
                (ending, error, now, campaign_id),
            )
        stored = self.campaign(campaign_id)
        assert stored is not None
        return stored

    def finish_campaign_from_orchestrator(
        self,
        campaign_id: str,
        operation_id: str,
    ) -> CampaignRecord:
        """Atomically accept Finish only from the live campaign orchestrator."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT campaign.*, invocation.role AS caller_role,
                       run.request_json AS caller_request_json
                FROM campaigns AS campaign
                JOIN graph_runs AS run
                  ON run.campaign_id = campaign.campaign_id
                JOIN campaign_invocations AS invocation
                  ON invocation.campaign_id = campaign.campaign_id
                 AND invocation.operation_id = run.operation_id
                WHERE campaign.campaign_id = ? AND run.operation_id = ?
                """,
                (campaign_id, operation_id),
            ).fetchone()
            if row is None:
                raise ValueError("campaign Finish caller is outside its campaign")
            request = json.loads(row["caller_request_json"])
            caller_actor = request.get("actor_operation_id") or operation_id
            if (
                row["caller_role"] != "orchestrator"
                or request.get("role") != "orchestrator"
                or caller_actor != row["root_operation_id"]
            ):
                raise ValueError("campaign Finish requires the sole orchestrator actor")
            if (
                row["status"] != "running"
                or row["ending"] is not None
                or row["stop_requested_at"] is not None
            ):
                raise CampaignNotRunning("the campaign is no longer accepting Finish")
            updated = connection.execute(
                """
                UPDATE campaigns
                SET status = 'wrapping_up', ending = 'completed', error = NULL, updated_at = ?
                WHERE campaign_id = ? AND status = 'running'
                  AND ending IS NULL AND stop_requested_at IS NULL
                """,
                (now, campaign_id),
            ).rowcount
            if updated != 1:
                raise CampaignNotRunning("the campaign is no longer accepting Finish")
            self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
            stored = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        assert stored is not None
        return self._campaign_record(stored)

    def fence_campaign_terminal_failure(
        self,
        operation_id: str,
        *,
        diagnostic: str,
    ) -> CampaignRecord | None:
        """Atomically fence one explicitly typed, exactly reportable orchestrator failure."""

        detail = " ".join(diagnostic.split())[:2000] or "Campaign orchestrator failed."
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run.*, invocation.role AS campaign_role,
                       campaign.root_operation_id AS root_operation_id,
                       campaign.status AS campaign_status,
                       campaign.stop_requested_at AS campaign_stop_requested_at,
                       campaign.ending AS campaign_ending,
                       run.campaign_id AS exact_campaign_id,
                       run.native_session_id AS exact_native_session_id,
                       run.stage_host AS exact_stage_host,
                       run.stage_root AS exact_stage_root,
                       run.request_json AS exact_request_json
                FROM graph_runs AS run
                JOIN campaign_invocations AS invocation
                  ON invocation.operation_id = run.operation_id
                JOIN campaigns AS campaign ON campaign.campaign_id = run.campaign_id
                WHERE run.operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None or row["campaign_role"] != "orchestrator":
                raise ValueError("terminal campaign failure requires its orchestrator task")
            campaign_id = str(row["exact_campaign_id"])
            request = json.loads(row["exact_request_json"])
            actor_operation_id = request.get("actor_operation_id") or operation_id
            if actor_operation_id != row["root_operation_id"]:
                raise ValueError("terminal campaign failure must belong to the sole orchestrator")
            if row["campaign_status"] == "wrapping_up" and row["campaign_ending"] == "failed":
                campaign_row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
                ).fetchone()
                assert campaign_row is not None
                return self._campaign_record(campaign_row)
            if row["campaign_status"] != "running" or row["campaign_stop_requested_at"] is not None:
                return None
            latest = self._campaign_actor_latest_row(
                connection,
                campaign_id,
                str(row["root_operation_id"]),
            )
            if (
                latest is None
                or latest["operation_id"] != operation_id
                or not row["exact_native_session_id"]
                or not row["exact_stage_root"]
                or latest["native_session_id"] != row["exact_native_session_id"]
                or (latest["stage_host"] or "") != (row["exact_stage_host"] or "")
                or latest["stage_root"] != row["exact_stage_root"]
            ):
                return None
            connection.execute(
                """
                UPDATE campaigns
                SET status = 'wrapping_up', ending = 'failed', error = ?, updated_at = ?
                WHERE campaign_id = ? AND status = 'running' AND stop_requested_at IS NULL
                """,
                (detail, now, campaign_id),
            )
            self._stop_unclaimed_campaign_watchers(connection, campaign_id, now)
            connection.execute(
                """
                UPDATE watchers
                SET status = 'stopped', notified = 1, next_check_at = NULL,
                    stopped_by = COALESCE(stopped_by, 'loop'),
                    stopped_at = COALESCE(stopped_at, ?)
                WHERE (
                    origin_operation_id IN (
                        SELECT operation_id FROM graph_runs WHERE campaign_id = ?
                    )
                    OR notification_operation_id IN (
                        SELECT operation_id FROM graph_runs WHERE campaign_id = ?
                    )
                )
                """,
                (now, campaign_id, campaign_id),
            )
            campaign_row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
        assert campaign_row is not None
        return self._campaign_record(campaign_row)

    def allocate_campaign_report_task(
        self,
        record: AgentTaskRecord,
        *,
        ending: CampaignEnding,
        error: str | None = None,
    ) -> tuple[CampaignRecord, AgentTaskRecord]:
        """Begin an ending and spend its one reserved report unit atomically.

        A repeated or racing claimant receives the report task already allocated
        for the current ending. A durable report closes that allocation cycle, so
        reauthorization may later allocate a new ending report.
        """

        if record.campaign_id is None:
            raise ValueError("a campaign report task must carry its campaign id")
        if record.request.get("role") != "report" or record.request.get("ending") != ending:
            raise ValueError("campaign report request does not match its ending")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (record.campaign_id,),
            ).fetchone()
            if row is None:
                raise KeyError(record.campaign_id)
            campaign = self._campaign_record(row)
            if (
                record.project_id != campaign.project_id
                or record.kind != "campaign"
                or record.authorized_by != campaign.authorized_by
            ):
                raise ValueError("campaign report task does not match its campaign lineage")
            if campaign.stop_requested_at is not None and ending != "stopped":
                raise ValueError("a stopped campaign must wrap up as stopped")
            if campaign.status == "wrapping_up":
                if campaign.ending != ending:
                    raise ValueError("campaign wrap-up already has a different ending")
                existing = self._current_campaign_report_task_row(
                    connection,
                    campaign.campaign_id,
                    ending,
                )
                if existing is not None:
                    return campaign, self._agent_task_record(existing)
            elif campaign.status in {"succeeded", "stopped", "failed", "needs_action"}:
                raise CampaignNotRunning("the campaign ending is already durable")
            else:
                now = record.created_at
                connection.execute(
                    """
                    UPDATE campaigns
                    SET status = 'wrapping_up', ending = ?, error = ?, updated_at = ?
                    WHERE campaign_id = ?
                    """,
                    (ending, error, now, campaign.campaign_id),
                )
                campaign = campaign.model_copy(
                    update={
                        "status": "wrapping_up",
                        "ending": ending,
                        "error": error,
                        "updated_at": now,
                    }
                )
            if not self._campaign_non_report_turns_settled(
                connection,
                campaign.campaign_id,
            ):
                raise CampaignNotRunning(
                    "the campaign report is waiting for already-admitted turns to settle"
                )
            self._insert_campaign_task(connection, campaign, record, "report")
            stored_row = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id = ?",
                (record.operation_id,),
            ).fetchone()
            updated_campaign_row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (campaign.campaign_id,),
            ).fetchone()
            assert stored_row is not None and updated_campaign_row is not None
            return self._campaign_record(updated_campaign_row), self._agent_task_record(stored_row)

    def finish_campaign_wrapup(
        self,
        report: CampaignReportRecord,
    ) -> tuple[CampaignRecord, CampaignReportRecord]:
        """Atomically capture and finalize one immutable campaign report."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM campaign_reports WHERE operation_id = ?",
                (report.operation_id,),
            ).fetchone()
            if existing is not None:
                stored_report = self._campaign_report_record(existing)
                if (
                    stored_report.campaign_id != report.campaign_id
                    or stored_report.ending != report.ending
                    or stored_report.sha256 != report.sha256
                    or stored_report.html != report.html
                ):
                    raise ValueError("the campaign report invocation already produced other bytes")
                stored_campaign = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?",
                    (report.campaign_id,),
                ).fetchone()
                if stored_campaign is None:
                    raise KeyError(report.campaign_id)
                return self._campaign_record(stored_campaign), stored_report
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (report.campaign_id,)
            ).fetchone()
            if row is None:
                raise KeyError(report.campaign_id)
            campaign = self._campaign_record(row)
            if campaign.status != "wrapping_up" or campaign.ending != report.ending:
                raise ValueError("campaign report does not match the active wrap-up")
            allocation = connection.execute(
                """
                SELECT role FROM campaign_invocations
                WHERE campaign_id = ? AND operation_id = ?
                """,
                (report.campaign_id, report.operation_id),
            ).fetchone()
            if allocation is None or allocation["role"] != "report":
                raise ValueError("campaign report was not produced by its reserved invocation")
            connection.execute(
                """
                INSERT INTO campaign_reports (
                    report_id, campaign_id, operation_id, ending, sha256, html, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.campaign_id,
                    report.operation_id,
                    report.ending,
                    report.sha256,
                    report.html,
                    report.created_at,
                ),
            )
            final_status: CampaignStatus = {
                "completed": "succeeded",
                "exhausted": "needs_action",
                "stopped": "stopped",
                "failed": "failed",
            }[report.ending]
            connection.execute(
                """
                UPDATE campaigns
                SET status = ?, updated_at = ?, ended_at = ?
                WHERE campaign_id = ?
                """,
                (final_status, now, now, report.campaign_id),
            )
            stored_campaign = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (report.campaign_id,),
            ).fetchone()
            stored_report = connection.execute(
                "SELECT * FROM campaign_reports WHERE operation_id = ?",
                (report.operation_id,),
            ).fetchone()
            assert stored_campaign is not None and stored_report is not None
            return (
                self._campaign_record(stored_campaign),
                self._campaign_report_record(stored_report),
            )

    def reauthorize_campaign(self, campaign_id: str, additional_invocations: int) -> CampaignRecord:
        """Extend an exhausted campaign without admitting its continuation yet."""

        result = self._reauthorize_campaign(campaign_id, additional_invocations)
        assert isinstance(result, CampaignRecord)
        return result

    def reauthorize_campaign_with_task(
        self,
        campaign_id: str,
        additional_invocations: int,
        record: AgentTaskRecord,
    ) -> tuple[CampaignRecord, AgentTaskRecord]:
        """Extend an exhausted campaign and spend its first new unit atomically."""

        if record.campaign_id != campaign_id or record.kind != "campaign":
            raise ValueError("campaign reauthorization task has invalid campaign lineage")
        if record.parent_operation_id is None:
            raise ValueError("campaign reauthorization must continue its orchestrator actor")
        result = self._reauthorize_campaign(
            campaign_id,
            additional_invocations,
            task=record,
        )
        assert isinstance(result, tuple)
        return result

    def _reauthorize_campaign(
        self,
        campaign_id: str,
        additional_invocations: int,
        *,
        task: AgentTaskRecord | None = None,
    ) -> CampaignRecord | tuple[CampaignRecord, AgentTaskRecord]:
        if isinstance(additional_invocations, bool) or additional_invocations < 2:
            raise ValueError("reauthorization needs research capacity plus one reserved report")
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            campaign = self._campaign_record(row)
            if campaign.status != "needs_action" or campaign.ending != "exhausted":
                raise ValueError("only an exhausted campaign can be reauthorized")
            connection.execute(
                """
                UPDATE campaigns
                SET invocation_ceiling = invocation_ceiling + ?, status = 'running',
                    ending = NULL, error = NULL, ended_at = NULL, updated_at = ?
                WHERE campaign_id = ?
                """,
                (additional_invocations, now, campaign_id),
            )
            if task is not None:
                updated_row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()
                assert updated_row is not None
                updated_campaign = self._campaign_record(updated_row)
                role = TypeAdapter(CampaignInvocationRole).validate_python(task.request.get("role"))
                if role != "orchestrator":
                    raise ValueError("campaign reauthorization must continue the orchestrator")
                self._insert_campaign_task(connection, updated_campaign, task, role)
        stored_campaign = self.campaign(campaign_id)
        assert stored_campaign is not None
        if task is None:
            return stored_campaign
        stored_task = self.agent_task(task.operation_id)
        assert stored_task is not None
        return stored_campaign, stored_task

    def campaign_reports(self, campaign_id: str) -> list[CampaignReportRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM campaign_reports
                WHERE campaign_id = ?
                ORDER BY created_at ASC, report_id ASC
                """,
                (campaign_id,),
            ).fetchall()
        return [self._campaign_report_record(row) for row in rows]

    def campaign_report_prior_history(
        self,
        campaign_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[CampaignReportRecord]]:
        """Return an exact prior-report count and the newest bounded report suffix."""

        if limit < 1:
            raise ValueError("campaign prior-report history limit must be positive")
        with self.connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM campaign_reports WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT * FROM campaign_reports
                WHERE campaign_id = ?
                ORDER BY created_at DESC, report_id DESC
                LIMIT ?
                """,
                (campaign_id, limit),
            ).fetchall()
        return total, [self._campaign_report_record(row) for row in reversed(rows)]

    def campaign_report(self, report_id: str) -> CampaignReportRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        return self._campaign_report_record(row) if row else None

    def record_campaign_message(self, record: CampaignMessageRecord) -> CampaignMessageRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                """
                SELECT root_operation_id, status, ending, stop_requested_at
                FROM campaigns WHERE campaign_id = ?
                """,
                (record.campaign_id,),
            ).fetchone()
            if campaign is None:
                raise KeyError(record.campaign_id)
            recipient = connection.execute(
                "SELECT campaign_id FROM graph_runs WHERE operation_id = ?",
                (record.recipient_task_id,),
            ).fetchone()
            if recipient is None or recipient["campaign_id"] != record.campaign_id:
                raise ValueError("campaign mail recipient is outside the campaign")
            if record.sender_role == "human":
                if campaign["status"] != "running" or campaign["ending"] is not None:
                    raise CampaignNotRunning("the campaign is not accepting new human mail")
                if record.sender_task_id is not None:
                    raise ValueError("a human campaign message cannot claim a task sender")
                if record.authorized_by is None:
                    raise ValueError("a human campaign message requires its sender snapshot")
                if record.recipient_task_id != campaign["root_operation_id"]:
                    raise ValueError("a human may message only the campaign orchestrator")
            else:
                if record.sender_task_id is None:
                    raise ValueError("an agent campaign message must name its sender task")
                if record.authorized_by is not None:
                    raise ValueError(
                        "an agent campaign message cannot claim a human sender snapshot"
                    )
                sender = connection.execute(
                    """
                    SELECT role FROM campaign_invocations
                    WHERE campaign_id = ? AND operation_id = ?
                    """,
                    (record.campaign_id, record.sender_task_id),
                ).fetchone()
                if sender is None:
                    raise ValueError("campaign mail sender is outside the campaign")
                expected = "orchestrator" if record.sender_role == "orchestrator" else "worker"
                if sender["role"] != expected:
                    raise ValueError("campaign mail sender role does not match its task")
                if (
                    record.sender_role == "worker"
                    and record.recipient_task_id != campaign["root_operation_id"]
                ):
                    raise ValueError("a worker may reply only to the campaign orchestrator")
                if record.sender_role == "orchestrator":
                    if (
                        campaign["status"] != "running"
                        or campaign["ending"] is not None
                        or campaign["stop_requested_at"] is not None
                    ):
                        raise CampaignNotRunning(
                            "the campaign is no longer accepting orchestrator mail"
                        )
                    target = connection.execute(
                        """
                        SELECT role FROM campaign_invocations
                        WHERE campaign_id = ? AND operation_id = ?
                        """,
                        (record.campaign_id, record.recipient_task_id),
                    ).fetchone()
                    if target is None or target["role"] != "worker":
                        raise ValueError("the orchestrator may address only one of its workers")
            connection.execute(
                """
                INSERT INTO campaign_messages (
                    message_id, campaign_id, sender_role, sender_task_id,
                    authorized_space_id, authorized_user_id, authorized_display_name,
                    recipient_task_id, control_node_id, body, created_at,
                    delivered_at, delivery_operation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.message_id,
                    record.campaign_id,
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
        stored = self.campaign_message(record.message_id)
        assert stored is not None
        return stored

    def campaign_message(self, message_id: str) -> CampaignMessageRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaign_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return self._campaign_message_record(row) if row else None

    def campaign_messages(self, campaign_id: str) -> list[CampaignMessageRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM campaign_messages
                WHERE campaign_id = ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (campaign_id,),
            ).fetchall()
        return [self._campaign_message_record(row) for row in rows]

    def campaign_report_message_history(
        self,
        campaign_id: str,
        *,
        limit: int,
    ) -> tuple[int, list[CampaignMessageRecord]]:
        """Return an exact message count and the newest bounded message suffix."""

        if limit < 1:
            raise ValueError("campaign report message history limit must be positive")
        with self.connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM campaign_messages WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT * FROM campaign_messages
                WHERE campaign_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                (campaign_id, limit),
            ).fetchall()
        return total, [self._campaign_message_record(row) for row in reversed(rows)]

    def pending_campaign_messages(
        self,
        campaign_id: str,
        recipient_task_id: str,
    ) -> list[CampaignMessageRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM campaign_messages
                WHERE campaign_id = ? AND recipient_task_id = ? AND delivered_at IS NULL
                ORDER BY created_at ASC, message_id ASC
                """,
                (campaign_id, recipient_task_id),
            ).fetchall()
        return [self._campaign_message_record(row) for row in rows]

    def mark_campaign_messages_delivered(
        self,
        message_ids: list[str],
        *,
        operation_id: str,
    ) -> None:
        if not message_ids:
            return
        now = self.now()
        placeholders = ",".join("?" for _ in message_ids)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT message_id FROM campaign_messages
                WHERE message_id IN ({placeholders}) AND delivered_at IS NULL
                """,
                tuple(message_ids),
            ).fetchall()
            if {row["message_id"] for row in rows} != set(message_ids):
                raise ValueError("campaign message delivery is stale or already claimed")
            connection.execute(
                f"""
                UPDATE campaign_messages
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
        campaign_id: str | None,
        verb: str,
        idempotency_key: str | None,
        payload: dict[str, object],
    ) -> AgentCommandInvocationRecord:
        """Record command start, or return the campaign key's existing invocation."""

        if not command_id or not verb:
            raise ValueError("command identity and verb must not be blank")
        if idempotency_key is not None and campaign_id is None:
            raise ValueError("a mutating command key requires a campaign binding")
        now = self.now()
        payload_json = self._bounded_command_payload(payload)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT campaign_id FROM graph_runs WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if task is None:
                    raise KeyError(operation_id)
                if task["campaign_id"] != campaign_id:
                    raise ValueError("command campaign binding does not match its task")
                if idempotency_key is not None:
                    existing = self._agent_command_by_key_from_connection(
                        connection,
                        campaign_id=campaign_id,
                        idempotency_key=idempotency_key,
                    )
                    if existing is not None:
                        if existing.verb != verb:
                            raise ValueError("idempotency key was already used for another verb")
                        return existing
                self._insert_agent_command_event(
                    connection,
                    operation_id=operation_id,
                    command_id=command_id,
                    campaign_id=campaign_id,
                    verb=verb,
                    phase="start",
                    idempotency_key=idempotency_key,
                    payload_json=payload_json,
                    message=f"Agent command {verb} started.",
                    level="info",
                    created_at=now,
                )
        except sqlite3.IntegrityError:
            if campaign_id is None or idempotency_key is None:
                raise
            existing = self.agent_command_by_key(campaign_id, idempotency_key)
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
        """Record command exit separately; repeated identical completion is harmless."""

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
                campaign_id=current.campaign_id,
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
        self,
        campaign_id: str,
        idempotency_key: str,
    ) -> AgentCommandInvocationRecord | None:
        with self.connection() as connection:
            return self._agent_command_by_key_from_connection(
                connection,
                campaign_id=campaign_id,
                idempotency_key=idempotency_key,
            )

    @staticmethod
    def _agent_command_by_key_from_connection(
        connection: sqlite3.Connection,
        *,
        campaign_id: str,
        idempotency_key: str,
    ) -> AgentCommandInvocationRecord | None:
        row = connection.execute(
            """
            SELECT command_id FROM graph_run_events
            WHERE event_kind = 'command' AND command_phase = 'start'
              AND campaign_id = ? AND idempotency_key = ?
            ORDER BY event_id ASC
            LIMIT 1
            """,
            (campaign_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return CampaignStoreMixin._agent_command_from_connection(connection, row["command_id"])

    @staticmethod
    def _agent_command_from_connection(
        connection: sqlite3.Connection,
        command_id: str,
    ) -> AgentCommandInvocationRecord | None:
        rows = connection.execute(
            """
            SELECT * FROM graph_run_events
            WHERE event_kind = 'command' AND command_id = ?
            ORDER BY event_id ASC
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
            campaign_id=start["campaign_id"],
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
        campaign_id: str | None,
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
                command_id, campaign_id, command_verb, command_phase,
                idempotency_key, payload_json
            ) VALUES (?, ?, ?, ?, 'command', ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                created_at,
                level,
                detail,
                command_id,
                campaign_id,
                verb,
                phase,
                idempotency_key,
                payload_json,
            ),
        )

    def request_campaign_worker_pause(self, operation_id: str, campaign_id: str) -> AgentTaskRecord:
        """Atomically request Pause only while the worker's campaign still admits commands."""

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET status = 'pausing', updated_at = ?, last_activity_at = ?,
                    phase = 'pausing', status_message = 'Pausing at the current checkpoint.'
                WHERE operation_id = ? AND campaign_id = ? AND status IN ('queued', 'running')
                  AND EXISTS (
                      SELECT 1 FROM campaign_invocations AS invocation
                      WHERE invocation.operation_id = graph_runs.operation_id
                        AND invocation.role = 'worker'
                  )
                  AND EXISTS (
                      SELECT 1 FROM campaigns AS campaign
                      WHERE campaign.campaign_id = graph_runs.campaign_id
                        AND campaign.status = 'running'
                        AND campaign.ending IS NULL
                        AND campaign.stop_requested_at IS NULL
                  )
                """,
                (now, now, operation_id, campaign_id),
            )
            if cursor.rowcount == 0:
                raise CampaignNotRunning(
                    "the campaign is no longer accepting worker-control commands"
                )
            self._insert_agent_task_event(
                connection,
                operation_id,
                "Pause requested by the campaign orchestrator.",
                level="info",
                created_at=now,
            )
        record = self.agent_task(operation_id)
        assert record is not None
        return record
