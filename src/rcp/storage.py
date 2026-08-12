from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from rcp.artifacts import AgentArtifactDescriptor, ResultViewDescriptor
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    require_dispatch,
)
from rcp.core.models import (
    DISPLAY_NAME_MAX_LENGTH,
    AuthorizedHuman,
    normalize_display_name,
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
    CAMPAIGN_MAIL_MAX_MESSAGES,
    CHAT_ARTIFACT_MAX_COUNT,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    PATCH_OUTPUT_RETENTION_DAYS,
    RUN_TRACE_RETENTION_DAYS,
    TEAM_CODE_FAILED_ATTEMPT_LIMIT,
    TEAM_ENROLLMENT_CODE_MAX_LENGTH,
    TEAM_INVITATION_TTL_DAYS,
    TEAM_MEMBER_TOKEN_MAX_LENGTH,
    TEAM_SESSION_IDLE_DAYS,
    TEAM_SESSION_TOKEN_MAX_LENGTH,
    WATCHER_ERROR_BACKOFF_SECONDS,
    WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT,
    WATCHER_HEALTHY_INTERVAL_SECONDS,
    WATCHER_SCHEDULE_JITTER_RATIO,
    WRITING_SESSION_RETENTION_DAYS,
    WRITING_SESSIONS_PER_PROJECT,
)
from rcp.providers import ProviderSkill, ProviderUsage
from rcp.skill_registry import SkillReference

if TYPE_CHECKING:
    from rcp.watchers import WatcherBinding


SpaceKind = Literal["personal", "team"]
SpaceUserKind = Literal["local_owner", "team_member"]
SPACE_NAME_MAX_LENGTH = 120


class TeamAuthenticationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SpaceUserRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str
    identity_kind: SpaceUserKind
    display_name: str | None = Field(default=None, max_length=DISPLAY_NAME_MAX_LENGTH)
    created_at: str
    updated_at: str

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        try:
            return _canonical_uuid4(value, label="user identity")
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_display_name(value)


class TeamInvitationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    invitation_id: str
    created_by: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    consumed_by: str | None = None
    failed_attempts: int
    locked_at: str | None = None


class ProjectRecord(BaseModel):
    project_id: str
    home_space_id: str | None = None
    locator: str
    name: str
    state_location: str
    state_remote: bool
    added_at: str
    last_opened_at: str | None = None
    revision: int | None = None
    primary_question: str | None = None
    attention_count: int = 0
    last_refresh_at: str | None = None
    reachable: bool | None = None
    error: str | None = None

    @field_validator("home_space_id")
    @classmethod
    def validate_home_space_id(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return None
        try:
            home_space_id = _canonical_uuid4(value, label="project home space identity")
            _canonical_uuid4(info.data.get("project_id"), label="canonical project identity")
            return home_space_id
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc


class ProjectStageRecord(BaseModel):
    host: str
    root: str


class ProviderSkillInventoryRecord(BaseModel):
    """One durable last-known provider-native skill inventory."""

    provider: str
    host: str
    configured_binary: str
    resolved_binary: str | None = None
    provider_version: str | None = None
    command: list[str] = Field(default_factory=list)
    protocol: str | None = None
    skills: list[ProviderSkill] = Field(default_factory=list)
    inventory_hash: str | None = None
    status: Literal["refreshing", "fresh", "stale", "unavailable"]
    diagnostic: str | None = None
    refreshed_at: str | None = None
    updated_at: str


AgentTaskKind = Literal[
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "campaign",
]
AgentTaskStatus = Literal[
    "queued",
    "running",
    "pausing",
    "paused",
    "succeeded",
    "failed",
    "interrupted",
]
AgentTaskReceiptTier = Literal["summary", "diagnostic", "trace"]

# A task is still moving through these; every other status is terminal. "pausing"
# belongs here because the pause has been requested but not yet observed, so a
# caller that treats it as settled reads a state the task is about to leave.
ACTIVE_AGENT_TASK_STATUSES: frozenset[AgentTaskStatus] = frozenset({"queued", "running", "pausing"})

_EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE = "experiment_episode_context_candidate"
_MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC = (
    "This Experiment-loop turn cannot be resumed or retried because its pre-migration "
    "root has no retained episode context candidate. Use Stop loop and press Run to start "
    "a fresh episode."
)


class AgentTaskEventRecord(BaseModel):
    event_id: int
    operation_id: str
    created_at: str
    level: Literal["info", "warning", "error"]
    message: str
    event_kind: Literal["message", "command"] = "message"
    command_id: str | None = None
    campaign_id: str | None = None
    command_verb: str | None = None
    command_phase: Literal["start", "exit"] | None = None
    idempotency_key: str | None = None
    payload: dict[str, object] | None = None


class AgentTaskReceiptRecord(BaseModel):
    receipt_id: int
    operation_id: str
    created_at: str
    tier: AgentTaskReceiptTier
    category: str
    payload: dict[str, object]


class AgentTaskContractRecord(BaseModel):
    operation_id: str
    role: str
    created_at: str
    sha256: str
    content: str


class ChatSessionContextRecord(BaseModel):
    """Durable RCP context baseline bound to one native provider session."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    execution_machine: str = Field(min_length=1)
    native_session_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    kind: Literal["node_chat", "project_chat"]
    chat_id: str = Field(min_length=1)
    node_id: str | None = None
    protocol_version: int = Field(ge=1)
    snapshot_json: str
    snapshot_sha256: str = Field(min_length=1)
    committed_operation_id: str = Field(min_length=1)
    created_at: str
    updated_at: str


class AgentTaskRecord(BaseModel):
    operation_id: str
    project_id: str
    kind: AgentTaskKind
    status: AgentTaskStatus
    request: dict[str, object]
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    status_message: str
    error: str | None = None
    applied_revision: int | None = None
    result: dict[str, object] | None = None
    attempt: int = 1
    parent_operation_id: str | None = None
    campaign_id: str | None = None
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    estimate_seconds: float = 300.0
    estimate_samples: int = 0
    phase: str = "queued"
    last_activity_at: str | None = None
    authorized_by: AuthorizedHuman | None = None
    dispatch_authority: AgentDispatchAuthority | None = None
    elapsed_seconds: float = 0.0
    progress: float = 0.0
    can_pause: bool = False
    can_resume: bool = False
    can_retry: bool = False


class ResultViewRecord(BaseModel):
    """Private binding and lifecycle metadata for one conversation result view."""

    model_config = ConfigDict(extra="forbid", strict=True)

    view_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    project_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    origin_operation_id: str = Field(min_length=1)
    latest_operation_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str
    reasoning: str
    run_on: str = Field(min_length=1)
    native_session_id: str = Field(min_length=1)
    stage_host: str
    stage_root: str = Field(min_length=1)
    source_name: str = Field(min_length=1, max_length=255)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0, le=CHAT_ARTIFACT_MAX_FILE_BYTES)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    kept_filename: str | None = Field(default=None, min_length=1, max_length=255)
    kept_at: str | None = Field(default=None, min_length=1)

    @field_validator("source_name")
    @classmethod
    def source_name_is_plain_html(cls, value: str) -> str:
        return _plain_html_name(value, label="result view source name")

    @field_validator("kept_filename")
    @classmethod
    def kept_filename_is_plain_html(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _plain_html_name(value, label="kept result view filename")

    @field_validator("created_at", "updated_at", "expires_at", "kept_at")
    @classmethod
    def timestamps_are_parseable(cls, value: str | None) -> str | None:
        if value is not None:
            _required_timestamp(value)
        return value

    @model_validator(mode="after")
    def lifecycle_is_coherent(self) -> ResultViewRecord:
        if (self.kept_filename is None) != (self.kept_at is None):
            raise ValueError("a kept result view requires both its filename and kept_at")
        created_at = _required_timestamp(self.created_at)
        updated_at = _required_timestamp(self.updated_at)
        expires_at = _required_timestamp(self.expires_at)
        if updated_at < created_at:
            raise ValueError("result view updated_at precedes created_at")
        if expires_at < created_at:
            raise ValueError("result view expires_at precedes created_at")
        if self.kept_at is not None and _required_timestamp(self.kept_at) < created_at:
            raise ValueError("result view kept_at precedes created_at")
        return self


CampaignStatus = Literal[
    "queued",
    "running",
    "stopping",
    "wrapping_up",
    "needs_action",
    "succeeded",
    "stopped",
    "failed",
]
CampaignEnding = Literal["completed", "exhausted", "stopped", "failed"]
CampaignInvocationRole = Literal["orchestrator", "worker", "report"]
CampaignMessageRole = Literal["human", "orchestrator", "worker"]
CampaignRecoveryPurpose = Literal["task", "report_admission"]
CampaignRecoveryStatus = Literal["pending", "admitted", "exhausted", "blocked"]
CampaignRecoveryMode = Literal["exact", "clean", "report_admission", "blocked"]


class CampaignRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    project_id: str
    root_operation_id: str | None = None
    status: CampaignStatus
    starting_instruction: str | None = Field(default=None, max_length=16_000)
    invocation_ceiling: int = Field(ge=1)
    invocations_used: int = Field(default=0, ge=0)
    authorized_by: AuthorizedHuman
    stop_requested_at: str | None = None
    ending: CampaignEnding | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    ended_at: str | None = None

    @model_validator(mode="after")
    def budget_is_coherent(self) -> CampaignRecord:
        if self.invocations_used > self.invocation_ceiling:
            raise ValueError("campaign invocations used exceed the authorized ceiling")
        return self

    @property
    def invocations_remaining(self) -> int:
        return max(0, self.invocation_ceiling - self.invocations_used)

    @property
    def research_invocations_remaining(self) -> int:
        return max(0, self.invocation_ceiling - self.invocations_used - 1)


class CampaignBudgetMeter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_ceiling: int = Field(ge=1)
    invocations_used: int = Field(ge=0)
    invocations_remaining: int = Field(ge=0)
    report_units_reserved: Literal[1] = 1
    observed_input_tokens: int = Field(default=0, ge=0)
    observed_generated_tokens: int = Field(default=0, ge=0)


class CampaignRecoveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_id: str
    campaign_id: str
    operation_id: str | None = None
    purpose: CampaignRecoveryPurpose
    failure_kind: str
    retry_mode: CampaignRecoveryMode
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1)
    status: CampaignRecoveryStatus
    next_attempt_at: str | None = None
    diagnostic: str
    admitted_operation_id: str | None = None
    created_at: str
    updated_at: str


class CampaignReportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    campaign_id: str
    operation_id: str
    ending: CampaignEnding
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    html: str
    created_at: str

    @field_validator("html")
    @classmethod
    def html_is_a_bounded_utf8_artifact(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("campaign report HTML contains NUL bytes")
        if len(value.encode("utf-8")) > CHAT_ARTIFACT_MAX_FILE_BYTES:
            raise ValueError("campaign report HTML exceeds the artifact size limit")
        return value


class CampaignMessageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    campaign_id: str
    sender_role: CampaignMessageRole
    sender_task_id: str | None = None
    authorized_by: AuthorizedHuman | None = None
    recipient_task_id: str
    control_node_id: str | None = None
    body: str = Field(min_length=1, max_length=16_000)
    created_at: str
    delivered_at: str | None = None
    delivery_operation_id: str | None = None

    @field_validator("body")
    @classmethod
    def message_body_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("campaign message body must not be blank")
        return stripped

    @model_validator(mode="after")
    def only_human_messages_carry_human_identity(self) -> CampaignMessageRecord:
        if self.sender_role != "human" and self.authorized_by is not None:
            raise ValueError("an agent campaign message cannot claim a human sender snapshot")
        return self


class CampaignActorBinding(BaseModel):
    """Canonical actor identity plus the newest task carrying its native session."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str
    actor_operation_id: str
    role: CampaignInvocationRole
    control_node_id: str | None = None
    current_operation_id: str
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None


class AgentCommandInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    campaign_id: str | None = None
    operation_id: str
    verb: str
    idempotency_key: str | None = None
    started_at: str
    start_payload: dict[str, object]
    exited_at: str | None = None
    status: Literal["ok", "invalid", "unavailable"] | None = None
    exit_payload: dict[str, object] | None = None


class ExperimentEpisodeRecord(BaseModel):
    """One bounded episode's native-session binding and graceful-stop intent.

    The binding is what an automatic watcher wake resumes. It is committed only
    by a mechanically successful joint handoff, so a failed first invocation
    never leaves a session an automatic wake would try to continue. A graph-only
    rejection is still a truthful accepted operational handoff.
    """

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    project_id: str
    control_node_id: str
    provider: str | None = None
    execution_machine: str | None = None
    execution_host: str = ""
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    chat_id: str | None = None
    last_turn_operation_id: str | None = None
    last_turn_invocation: int | None = Field(default=None, ge=1)
    last_graph_result: str | None = None
    last_watcher_ids: list[str] = Field(default_factory=list)
    context_baseline: dict[str, object] = Field(default_factory=dict)
    session_diagnostic: str | None = None
    stop_requested_at: str | None = None
    stop_settled_at: str | None = None
    created_at: str
    updated_at: str

    @property
    def session_bound(self) -> bool:
        """Whether an automatic wake has a complete binding to resume."""

        return bool(
            self.native_session_id
            and self.provider
            and self.execution_machine
            and self.stage_root
            and self.chat_id
        )


class ExperimentLoopRuntime(BaseModel):
    """Operational state of the newest bounded episode for one Experiment."""

    episode_id: str | None = None
    invocations_used: int = Field(default=0, ge=0)
    invocation_ceiling: int | None = Field(default=None, ge=1)
    control_revision: int | None = Field(default=None, ge=0)
    active: bool = False
    paused: bool = False
    task_active: bool = False
    detached_work_active: bool = False
    watcher_degraded: bool = False
    watcher_completion_pending: bool = False
    episode_exited: bool = False
    decision_bundle: list[dict[str, object]] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    stop_requested: bool = False
    stop_settled: bool = False
    session_bound: bool = False
    session_diagnostic: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    execution_host: str | None = None
    run_truth_scope: list[str] | None = None
    chat_id: str | None = None
    current_operation_id: str | None = None
    current_status: str | None = None
    current_phase: str | None = None
    current_status_message: str | None = None
    current_last_activity_at: str | None = None
    current_invocation: int | None = Field(default=None, ge=1)


AgentUsageCountReason = Literal["counted", "duplicate", "invalid"]


class AgentUsageRecord(BaseModel):
    usage_id: str
    project_id: str
    operation_id: str
    task_kind: AgentTaskKind
    provider: str
    model: str | None = None
    provider_profile: str
    provider_event_type: str
    dedupe_key: str
    counted: bool
    count_reason: AgentUsageCountReason
    created_at: str
    processed_input_tokens: int = Field(ge=0)
    generated_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    reported_total_tokens: int | None = Field(default=None, ge=0)
    provider_fields: dict[str, object] = Field(default_factory=dict)


class AgentUsageCell(BaseModel):
    task_kind: AgentTaskKind
    provider: str
    processed_input_tokens: int = 0
    generated_tokens: int = 0
    cached_input_tokens: int = 0
    counted_records: int = 0


class AgentUsageMetric(BaseModel):
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_share: float = 0.0
    block_percent: float = 5.0
    block_tokens: float = 0.0
    cells: list[AgentUsageCell] = Field(default_factory=list)


class AgentUsageSnapshot(BaseModel):
    project_id: str
    input_processed: AgentUsageMetric
    generated: AgentUsageMetric
    counted_records: int = 0
    excluded_records: int = 0
    records: list[AgentUsageRecord] = Field(default_factory=list)


WatcherStatus = Literal["active", "degraded", "completed", "stopped"]


class WatcherClaimConflict(ValueError):
    """A watcher delivery already won the atomic claim."""


class CampaignBudgetExhausted(ValueError):
    """A campaign kept its final authorized unit for the required report."""


class CampaignNotRunning(ValueError):
    """A campaign cannot admit new ordinary work in its current state."""


class CampaignActorBusy(ValueError):
    """One campaign actor already has an unresolved leaf using its native session."""

    def __init__(self, actor_operation_id: str, operation_id: str) -> None:
        self.actor_operation_id = actor_operation_id
        self.operation_id = operation_id
        super().__init__(
            f"Campaign actor {actor_operation_id} already has unresolved task {operation_id}."
        )


class ResultViewConflict(ValueError):
    """A result-view revision was based on bytes that are no longer current."""


class WatcherStopRequest(BaseModel):
    """An Experiment agent's narrow request to retire one staged observer."""

    model_config = ConfigDict(extra="forbid")

    stop_watcher_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("stop_watcher_id", "reason")
    @classmethod
    def is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("watcher stop fields must not be blank")
        return stripped


class WatcherContinuation(BaseModel):
    """RCP-bound policy needed to create a fresh Work wake."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    reasoning: str | None = None
    run_on: str
    run_truth_scope: list[str] | None = None
    patch_kind: Literal["work", "experiment_loop"] = "work"
    control_node_id: str | None = None
    control_revision: int | None = Field(default=None, ge=0)
    control_episode_id: str | None = None
    control_invocation: int | None = Field(default=None, ge=1)
    control_invocation_ceiling: int | None = Field(default=None, ge=1)
    control_decision_bundle: list[dict[str, object]] = Field(default_factory=list)
    control_completion_criteria: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] = Field(default_factory=list)


class NodeStatusGraphCondition(BaseModel):
    """Wake when one canonical node reaches any named status."""

    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str = Field(min_length=1)
    status_in: list[str] = Field(min_length=1)

    @field_validator("node_id")
    @classmethod
    def node_id_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("graph condition node_id must not be blank")
        return stripped

    @field_validator("status_in")
    @classmethod
    def statuses_are_unique_and_not_blank(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("graph condition statuses must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("graph condition statuses must be unique")
        return sorted(normalized)


class ProposalResolvedGraphCondition(BaseModel):
    """Wake when a Proposal related to one canonical node is resolved."""

    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str = Field(min_length=1)
    proposal_resolved: Literal[True]

    @field_validator("proposal_resolved", mode="before")
    @classmethod
    def proposal_resolved_is_literal_true(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("proposal_resolved must be the JSON literal true")
        return value

    @field_validator("node_id")
    @classmethod
    def node_id_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("graph condition node_id must not be blank")
        return stripped


GraphCondition = Annotated[
    NodeStatusGraphCondition | ProposalResolvedGraphCondition,
    Field(union_mode="left_to_right"),
]


class WatcherDeliveryRecord(BaseModel):
    """Durable delivery state shared by external and graph watchers."""

    model_config = ConfigDict(extra="forbid")

    watcher_id: str
    project_id: str
    origin_operation_id: str
    origin_task_kind: Literal["node_chat", "project_chat", "campaign"]
    chat_id: str
    node_id: str | None = None
    experiment_episode_id: str | None = None
    execution_host: str = ""
    continuation: WatcherContinuation
    status: WatcherStatus = "active"
    created_at: str
    completed_at: str | None = None
    notified: bool = False
    notification_operation_id: str | None = None
    stopped_by: Literal["human", "loop", "agent"] | None = None
    stop_reason: str | None = None
    stopped_at: str | None = None
    stop_operation_id: str | None = None


class WatcherRecord(WatcherDeliveryRecord):
    """Durable external observer checked from a fresh login shell."""

    check_command: str
    log_path: str
    cwd: str
    last_checked_at: str | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    next_check_at: str | None = None
    consecutive_error_count: int = Field(default=0, ge=0)
    group_id: str | None = None
    group_label: str | None = None


class GraphWatcherRecord(WatcherDeliveryRecord):
    """Durable canonical-graph condition with no shell-check fields."""

    condition: GraphCondition
    armed_revision: int | None = Field(default=None, ge=0)
    last_evaluated_at: str | None = None
    status: Literal["active", "completed", "stopped"] = "active"

    @property
    def last_checked_at(self) -> str | None:
        return self.last_evaluated_at

    @property
    def last_exit_code(self) -> None:
        return None

    @property
    def last_error(self) -> None:
        return None

    @property
    def next_check_at(self) -> None:
        return None

    @property
    def consecutive_error_count(self) -> int:
        return 0

    @property
    def group_id(self) -> None:
        return None

    @property
    def group_label(self) -> None:
        return None


StoredWatcherRecord = WatcherRecord | GraphWatcherRecord


class ExperimentWatcherResourceRecord(BaseModel):
    """The current node-and-episode owner of one Experiment watcher file."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    control_node_id: str
    episode_id: str
    execution_host: str
    wake_task_kind: Literal["node_chat"]
    wake_chat_id: str
    continuation: WatcherContinuation
    watcher_snapshot_token: str


def watcher_next_check_at(
    watcher_id: str,
    checked_at: str,
    consecutive_error_count: int,
) -> str:
    """Return one durable, identity-jittered watcher due time."""

    if consecutive_error_count < 0:
        raise ValueError("watcher error count cannot be negative")
    try:
        base = datetime.fromisoformat(checked_at)
    except ValueError as exc:
        raise ValueError("watcher check time must be ISO 8601") from exc
    if consecutive_error_count == 0:
        delay = WATCHER_HEALTHY_INTERVAL_SECONDS
    else:
        delay = WATCHER_ERROR_BACKOFF_SECONDS[
            min(consecutive_error_count - 1, len(WATCHER_ERROR_BACKOFF_SECONDS) - 1)
        ]
    fraction = int.from_bytes(hashlib.sha256(watcher_id.encode("utf-8")).digest()[:8], "big")
    unit = fraction / ((1 << 64) - 1)
    jitter = 1 + WATCHER_SCHEDULE_JITTER_RATIO * (2 * unit - 1)
    return (base + timedelta(seconds=delay * jitter)).isoformat()


_EXPERIMENT_EPISODE_PINNED_FIELDS = (
    "run_on",
    "run_truth_scope",
    "chat_id",
    "control_node_id",
    "control_revision",
    "control_episode_id",
    "control_invocation_ceiling",
    "control_decision_bundle",
    "control_completion_criteria",
)


def _experiment_pinned_value(request: dict[str, object], field: str) -> object:
    value = request.get(field)
    if field == "run_truth_scope" and isinstance(value, list):
        return sorted({str(item) for item in value})
    return value


def _canonical_uuid4(value: object, *, label: str) -> str:
    identifier = str(value)
    try:
        parsed = uuid.UUID(identifier)
    except ValueError as exc:
        raise RuntimeError(f"RCP {label} is invalid.") from exc
    if str(parsed) != identifier or parsed.version != 4:
        raise RuntimeError(f"RCP {label} is not a canonical UUIDv4.")
    return identifier


def _canonical_space_id(value: object) -> str:
    return _canonical_uuid4(value, label="space identity")


def _stored_space_kind(value: object) -> SpaceKind:
    if value == "personal" or value == "team":
        return value
    raise RuntimeError("RCP space kind is invalid.")


def normalize_space_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("space name must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError("space name must not be blank")
    if any(character in normalized for character in ("\n", "\r", "\u2028", "\u2029")):
        raise ValueError("space name must be a single line")
    if len(normalized) > SPACE_NAME_MAX_LENGTH:
        raise ValueError(f"space name must be at most {SPACE_NAME_MAX_LENGTH} characters")
    return normalized


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_member_token() -> tuple[str, str]:
    token = f"rcp_{secrets.token_urlsafe(32)}"
    return token, _sha256(token)


def _new_session_token() -> tuple[str, str]:
    token = f"rcp_session_{secrets.token_urlsafe(32)}"
    return token, _sha256(token)


def _new_enrollment_code(kind: Literal["bootstrap", "invite"]) -> tuple[str, str, str]:
    code_id = secrets.token_urlsafe(12)
    secret = secrets.token_urlsafe(32)
    return f"rcp_{kind}_{code_id}.{secret}", code_id, _sha256(secret)


def _parse_enrollment_code(
    code: str,
) -> tuple[Literal["bootstrap", "invite"], str, str] | None:
    if not isinstance(code, str) or len(code) > TEAM_ENROLLMENT_CODE_MAX_LENGTH or "." not in code:
        return None
    public, secret = code.split(".", 1)
    if not secret:
        return None
    for kind in ("bootstrap", "invite"):
        prefix = f"rcp_{kind}_"
        if public.startswith(prefix) and len(public) > len(prefix):
            return kind, public[len(prefix) :], _sha256(secret)
    return None


def _discard_failed_team_initialization(path: Path, expected_space_id: str) -> None:
    """Remove only the unopened team database created by this failed init attempt."""

    if not path.exists():
        return
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            identity = connection.execute(
                "SELECT space_id, space_kind FROM space_identity WHERE singleton = 1"
            ).fetchone()
            user_count = connection.execute("SELECT COUNT(*) FROM space_users").fetchone()[0]
    except (OSError, sqlite3.Error):
        return
    if identity != (expected_space_id, "team") or user_count != 0:
        return
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        candidate.unlink(missing_ok=True)


_PROJECT_ID_TABLES = (
    "projects",
    "paper_drafts",
    "writing_sessions",
    "chat_session_contexts",
    "result_views",
    "graph_runs",
    "campaigns",
    "agent_usage",
    "watchers",
    "experiment_episodes",
)


class AppStore:
    def __init__(self, path: Path, *, space_kind: SpaceKind | None = None) -> None:
        if space_kind is not None and space_kind not in ("personal", "team"):
            raise ValueError("space kind must be 'personal' or 'team'")
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(space_kind)

    @classmethod
    def initialize_team_space(cls, path: Path, name: str) -> tuple[AppStore, str]:
        store = cls.__new__(cls)
        store.path = path
        store.path.parent.mkdir(parents=True, exist_ok=True)
        initial_space_id = str(uuid.uuid4())
        try:
            bootstrap_code = store._initialize(
                "team",
                initial_space_id=initial_space_id,
                initial_space_name=normalize_space_name(name),
                issue_bootstrap=True,
                require_new=True,
            )
        except Exception:
            _discard_failed_team_initialization(path, initial_space_id)
            raise
        if bootstrap_code is None:  # pragma: no cover - guarded by issue_bootstrap
            raise RuntimeError("RCP team bootstrap code was not created.")
        return store, bootstrap_code

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(
        self,
        requested_space_kind: SpaceKind | None,
        *,
        initial_space_id: str | None = None,
        initial_space_name: str | None = None,
        issue_bootstrap: bool = False,
        require_new: bool = False,
    ) -> str | None:
        bootstrap_code: str | None = None
        recovering_team_initialization = False
        with self.connection() as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                # A concurrent first opener may be changing the journal mode.
                # Waiting for a write boundary proves that transaction finished
                # before retrying the same required mode change.
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            identity_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'space_identity'"
                ).fetchone()
                is not None
            )
            if require_new and identity_table_exists:
                identity_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(space_identity)")
                }
                users_table_exists_for_recovery = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'space_users'"
                    ).fetchone()
                    is not None
                )
                existing_identity = (
                    connection.execute(
                        "SELECT space_kind, space_name FROM space_identity WHERE singleton = 1"
                    ).fetchone()
                    if {"space_kind", "space_name"}.issubset(identity_columns)
                    else None
                )
                existing_user_count = (
                    connection.execute("SELECT COUNT(*) FROM space_users").fetchone()[0]
                    if users_table_exists_for_recovery
                    else -1
                )
                recovering_team_initialization = bool(
                    issue_bootstrap
                    and initial_space_name is not None
                    and existing_identity is not None
                    and existing_identity["space_kind"] == "team"
                    and existing_identity["space_name"] == initial_space_name
                    and existing_user_count == 0
                )
                if not recovering_team_initialization:
                    raise ValueError("This RCP data directory already contains a space.")
            if not identity_table_exists:
                legacy_database = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    is not None
                )
                if require_new and legacy_database:
                    raise ValueError("This RCP data directory already contains RCP data.")
                stored_space_kind = (
                    "personal" if legacy_database else requested_space_kind or "personal"
                )
                if requested_space_kind is not None and requested_space_kind != stored_space_kind:
                    raise ValueError(
                        "An existing RCP database migrates to personal; it cannot be opened "
                        f"as {requested_space_kind}."
                    )
                connection.execute(
                    """
                    CREATE TABLE space_identity (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        space_id TEXT NOT NULL UNIQUE,
                        space_kind TEXT NOT NULL CHECK(space_kind IN ('personal', 'team')),
                        space_name TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO space_identity(singleton, space_id, space_kind, space_name)
                    VALUES (1, ?, ?, ?)
                    """,
                    (initial_space_id or str(uuid.uuid4()), stored_space_kind, initial_space_name),
                )
            else:
                identity_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(space_identity)")
                }
                if "space_id" not in identity_columns:
                    raise RuntimeError("RCP space identity schema is invalid.")
                identity = connection.execute(
                    "SELECT space_id FROM space_identity WHERE singleton = 1"
                ).fetchone()
                if identity is None:
                    raise RuntimeError("RCP space identity is unavailable.")
                _canonical_space_id(identity["space_id"])
                if "space_kind" not in identity_columns:
                    connection.execute(
                        """
                        ALTER TABLE space_identity
                        ADD COLUMN space_kind TEXT CHECK(space_kind IN ('personal', 'team'))
                        """
                    )
                    connection.execute(
                        "UPDATE space_identity SET space_kind = 'personal' WHERE singleton = 1"
                    )
                    stored_space_kind = "personal"
                else:
                    identity = connection.execute(
                        "SELECT space_kind FROM space_identity WHERE singleton = 1"
                    ).fetchone()
                    assert identity is not None
                    stored_space_kind = _stored_space_kind(identity["space_kind"])

                if requested_space_kind is not None and requested_space_kind != stored_space_kind:
                    raise ValueError(
                        f"RCP space is {stored_space_kind}; it cannot be opened as "
                        f"{requested_space_kind}."
                    )

            identity = connection.execute(
                "SELECT space_id, space_kind FROM space_identity WHERE singleton = 1"
            ).fetchone()
            if identity is None:
                raise RuntimeError("RCP space identity is unavailable.")
            _canonical_space_id(identity["space_id"])
            stored_space_kind = _stored_space_kind(identity["space_kind"])

            users_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'space_users'"
                ).fetchone()
                is not None
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS space_users (
                    user_id TEXT PRIMARY KEY,
                    identity_kind TEXT NOT NULL
                        CHECK(identity_kind IN ('local_owner', 'team_member')),
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_bootstrap_codes (
                    code_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_invitations (
                    invitation_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_member_tokens (
                    token_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            if not users_table_exists and stored_space_kind == "personal":
                now = self.now()
                owner = SpaceUserRecord(
                    user_id=str(uuid.uuid4()),
                    identity_kind="local_owner",
                    created_at=now,
                    updated_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO space_users (
                        user_id, identity_kind, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        owner.user_id,
                        owner.identity_kind,
                        owner.display_name,
                        owner.created_at,
                        owner.updated_at,
                    ),
                )
            users = self._space_users_from_connection(connection)
            if stored_space_kind == "personal":
                if len(users) != 1 or users[0].identity_kind != "local_owner":
                    raise RuntimeError("A personal RCP space must contain exactly one local owner.")
            elif any(user.identity_kind == "local_owner" for user in users):
                raise RuntimeError("A team RCP space cannot contain a local owner.")

            # S111 stores may already have the earlier trigger that protected
            # only ``space_id``. Replace it atomically so the additive kind is
            # covered as soon as the migration commits.
            connection.execute("DROP TRIGGER IF EXISTS space_identity_immutable")
            connection.execute(
                """
                CREATE TRIGGER space_identity_immutable
                BEFORE UPDATE OF singleton, space_id, space_kind ON space_identity
                BEGIN
                    SELECT RAISE(ABORT, 'space identity is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS space_user_identity_immutable
                BEFORE UPDATE OF user_id, identity_kind ON space_users
                BEGIN
                    SELECT RAISE(ABORT, 'space user identity is immutable');
                END
                """
            )
            connection.commit()
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS paper_drafts (
                    project_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    base_hash TEXT,
                    updated_at TEXT NOT NULL,
                    cursor_state TEXT
                );
                CREATE TABLE IF NOT EXISTS writing_sessions (
                    native_session_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    execution_machine TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    title TEXT,
                    model TEXT NOT NULL,
                    reasoning TEXT,
                    created_at TEXT NOT NULL,
                    last_resumed_at TEXT NOT NULL,
                    introduction_hash_examined TEXT NOT NULL,
                    graph_revision_examined INTEGER NOT NULL,
                    research_md_hash_examined TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS writing_sessions_project
                    ON writing_sessions(project_id, last_resumed_at DESC);
                CREATE TABLE IF NOT EXISTS chat_session_contexts (
                    provider TEXT NOT NULL,
                    execution_machine TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    node_id TEXT,
                    protocol_version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    committed_operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, execution_machine, native_session_id)
                );
                CREATE INDEX IF NOT EXISTS chat_session_contexts_project
                    ON chat_session_contexts(project_id);
                CREATE INDEX IF NOT EXISTS chat_session_contexts_native_session
                    ON chat_session_contexts(native_session_id);
                CREATE TABLE IF NOT EXISTS result_views (
                    view_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    experiment_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    origin_operation_id TEXT NOT NULL,
                    latest_operation_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    run_on TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    stage_host TEXT NOT NULL,
                    stage_root TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    kept_filename TEXT,
                    kept_at TEXT,
                    CHECK((kept_filename IS NULL) = (kept_at IS NULL))
                );
                CREATE INDEX IF NOT EXISTS result_views_project_experiment
                    ON result_views(project_id, experiment_id, updated_at DESC, view_id);
                CREATE INDEX IF NOT EXISTS result_views_project_chat
                    ON result_views(project_id, chat_id, updated_at DESC, view_id);
                CREATE INDEX IF NOT EXISTS result_views_expiry
                    ON result_views(expires_at, kept_filename);
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    home_space_id TEXT,
                    locator TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    state_location TEXT NOT NULL,
                    state_remote INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    revision INTEGER,
                    primary_question TEXT,
                    attention_count INTEGER NOT NULL DEFAULT 0,
                    last_refresh_at TEXT,
                    reachable INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS projects_recent
                    ON projects(last_opened_at DESC, added_at DESC);
                CREATE TABLE IF NOT EXISTS project_aliases (
                    alias_id TEXT PRIMARY KEY,
                    canonical_project_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_aliases_canonical
                    ON project_aliases(canonical_project_id, alias_id);
                CREATE TABLE IF NOT EXISTS provider_skill_inventories (
                    provider TEXT NOT NULL,
                    host TEXT NOT NULL,
                    configured_binary TEXT NOT NULL,
                    resolved_binary TEXT,
                    provider_version TEXT,
                    command_json TEXT NOT NULL DEFAULT '[]',
                    protocol TEXT,
                    skills_json TEXT NOT NULL DEFAULT '[]',
                    inventory_hash TEXT,
                    status TEXT NOT NULL,
                    diagnostic TEXT,
                    refreshed_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, host, configured_binary)
                );
                CREATE TABLE IF NOT EXISTS graph_runs (
                    operation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    campaign_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status_message TEXT NOT NULL,
                    error TEXT,
                    applied_revision INTEGER,
                    result_json TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    parent_operation_id TEXT,
                    native_session_id TEXT,
                    stage_host TEXT,
                    stage_root TEXT,
                    estimate_seconds REAL NOT NULL DEFAULT 300,
                    estimate_samples INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    last_activity_at TEXT,
                    campaign_worker_handoffs_cleared_at TEXT,
                    dispatch_authority_json TEXT,
                    authorized_space_id TEXT,
                    authorized_user_id TEXT,
                    authorized_display_name TEXT
                );
                CREATE INDEX IF NOT EXISTS graph_runs_project
                    ON graph_runs(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    root_operation_id TEXT,
                    status TEXT NOT NULL,
                    starting_instruction TEXT,
                    invocation_ceiling INTEGER NOT NULL CHECK(invocation_ceiling >= 1),
                    invocations_used INTEGER NOT NULL DEFAULT 0
                        CHECK(invocations_used >= 0 AND invocations_used <= invocation_ceiling),
                    authorized_space_id TEXT NOT NULL,
                    authorized_user_id TEXT NOT NULL,
                    authorized_display_name TEXT NOT NULL,
                    stop_requested_at TEXT,
                    ending TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT
                );
                CREATE INDEX IF NOT EXISTS campaigns_project
                    ON campaigns(project_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS campaigns_one_live_project
                    ON campaigns(project_id)
                    WHERE status IN (
                        'queued', 'running', 'stopping', 'wrapping_up', 'needs_action'
                    );
                CREATE TABLE IF NOT EXISTS campaign_invocations (
                    campaign_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(campaign_id, operation_id),
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_invocations_campaign
                    ON campaign_invocations(campaign_id, created_at, operation_id);
                CREATE TABLE IF NOT EXISTS campaign_reports (
                    report_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    ending TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    html TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_reports_campaign
                    ON campaign_reports(campaign_id, created_at, report_id);
                CREATE TABLE IF NOT EXISTS campaign_messages (
                    message_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    sender_role TEXT NOT NULL,
                    sender_task_id TEXT,
                    authorized_space_id TEXT,
                    authorized_user_id TEXT,
                    authorized_display_name TEXT,
                    recipient_task_id TEXT NOT NULL,
                    control_node_id TEXT,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    delivery_operation_id TEXT,
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_messages_campaign
                    ON campaign_messages(campaign_id, created_at, message_id);
                CREATE TABLE IF NOT EXISTS campaign_recoveries (
                    recovery_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    operation_id TEXT,
                    purpose TEXT NOT NULL,
                    failure_kind TEXT NOT NULL,
                    retry_mode TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1),
                    status TEXT NOT NULL,
                    next_attempt_at TEXT,
                    diagnostic TEXT NOT NULL,
                    admitted_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id),
                    FOREIGN KEY(admitted_operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_recoveries_due
                    ON campaign_recoveries(status, next_attempt_at, created_at);
                CREATE TABLE IF NOT EXISTS agent_usage (
                    usage_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    provider_profile TEXT NOT NULL,
                    provider_event_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    counted INTEGER NOT NULL,
                    count_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed_input_tokens INTEGER NOT NULL,
                    generated_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER NOT NULL,
                    cache_creation_input_tokens INTEGER NOT NULL,
                    cache_write_input_tokens INTEGER NOT NULL,
                    reasoning_output_tokens INTEGER NOT NULL,
                    reported_input_tokens INTEGER,
                    reported_output_tokens INTEGER,
                    reported_total_tokens INTEGER,
                    provider_fields_json TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS agent_usage_project
                    ON agent_usage(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS graph_run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    event_kind TEXT NOT NULL DEFAULT 'message',
                    command_id TEXT,
                    campaign_id TEXT,
                    command_verb TEXT,
                    command_phase TEXT,
                    idempotency_key TEXT,
                    payload_json TEXT,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS graph_run_events_operation
                    ON graph_run_events(operation_id, event_id);
                CREATE TABLE IF NOT EXISTS graph_run_receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    category TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS graph_run_receipts_operation
                    ON graph_run_receipts(operation_id, receipt_id);
                CREATE TABLE IF NOT EXISTS graph_run_outputs (
                    operation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    patch_json TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE TABLE IF NOT EXISTS graph_run_contracts (
                    operation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(operation_id, role),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE TABLE IF NOT EXISTS watchers (
                    watcher_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    origin_operation_id TEXT NOT NULL,
                    origin_task_kind TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    node_id TEXT,
                    experiment_episode_id TEXT,
                    execution_host TEXT NOT NULL,
                    check_command TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    graph_condition_json TEXT,
                    armed_revision INTEGER,
                    continuation_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_exit_code INTEGER,
                    last_error TEXT,
                    completed_at TEXT,
                    next_check_at TEXT,
                    consecutive_error_count INTEGER NOT NULL DEFAULT 0,
                    group_id TEXT,
                    group_label TEXT,
                    notified INTEGER NOT NULL DEFAULT 0,
                    notification_operation_id TEXT,
                    stopped_by TEXT,
                    stop_reason TEXT,
                    stopped_at TEXT,
                    stop_operation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS watchers_project
                    ON watchers(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS watchers_pollable
                    ON watchers(status, created_at);
                CREATE INDEX IF NOT EXISTS watchers_delivery
                    ON watchers(project_id, origin_operation_id, notified, completed_at);
                CREATE TABLE IF NOT EXISTS experiment_episodes (
                    episode_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    control_node_id TEXT NOT NULL,
                    provider TEXT,
                    execution_machine TEXT,
                    execution_host TEXT NOT NULL DEFAULT '',
                    native_session_id TEXT,
                    stage_host TEXT,
                    stage_root TEXT,
                    chat_id TEXT,
                    last_turn_operation_id TEXT,
                    last_turn_invocation INTEGER,
                    last_graph_result TEXT,
                    last_watcher_ids_json TEXT NOT NULL DEFAULT '[]',
                    context_baseline_json TEXT NOT NULL DEFAULT '{}',
                    session_diagnostic TEXT,
                    stop_requested_at TEXT,
                    stop_settled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experiment_episodes_control
                    ON experiment_episodes(project_id, control_node_id, created_at DESC);
                """
            )
            # Existing v0.2 databases need additive migration before the index
            # can include the new transitional state.
            self._ensure_column(connection, "projects", "home_space_id", "TEXT")
            self._ensure_column(connection, "space_identity", "space_name", "TEXT")
            self._ensure_column(connection, "paper_drafts", "ancestor_content", "TEXT")
            self._ensure_column(connection, "graph_runs", "attempt", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "graph_runs", "parent_operation_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "native_session_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "stage_host", "TEXT")
            self._ensure_column(connection, "graph_runs", "stage_root", "TEXT")
            self._ensure_column(
                connection, "graph_runs", "estimate_seconds", "REAL NOT NULL DEFAULT 300"
            )
            self._ensure_column(
                connection, "graph_runs", "estimate_samples", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(connection, "graph_runs", "phase", "TEXT NOT NULL DEFAULT 'queued'")
            self._ensure_column(connection, "graph_runs", "last_activity_at", "TEXT")
            self._ensure_column(
                connection,
                "graph_runs",
                "campaign_worker_handoffs_cleared_at",
                "TEXT",
            )
            self._ensure_column(connection, "graph_runs", "result_json", "TEXT")
            self._ensure_column(connection, "graph_runs", "dispatch_authority_json", "TEXT")
            self._ensure_column(connection, "graph_runs", "authorized_space_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "authorized_user_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "authorized_display_name", "TEXT")
            self._ensure_column(connection, "graph_runs", "campaign_id", "TEXT")
            self._ensure_column(connection, "campaign_messages", "authorized_space_id", "TEXT")
            self._ensure_column(connection, "campaign_messages", "authorized_user_id", "TEXT")
            self._ensure_column(
                connection,
                "campaign_messages",
                "authorized_display_name",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "graph_run_events",
                "event_kind",
                "TEXT NOT NULL DEFAULT 'message'",
            )
            self._ensure_column(connection, "graph_run_events", "command_id", "TEXT")
            self._ensure_column(connection, "graph_run_events", "campaign_id", "TEXT")
            self._ensure_column(connection, "graph_run_events", "command_verb", "TEXT")
            self._ensure_column(connection, "graph_run_events", "command_phase", "TEXT")
            self._ensure_column(connection, "graph_run_events", "idempotency_key", "TEXT")
            self._ensure_column(connection, "graph_run_events", "payload_json", "TEXT")
            self._ensure_column(connection, "watchers", "next_check_at", "TEXT")
            self._ensure_column(
                connection,
                "watchers",
                "consecutive_error_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "watchers", "group_id", "TEXT")
            self._ensure_column(connection, "watchers", "group_label", "TEXT")
            self._ensure_column(connection, "watchers", "experiment_episode_id", "TEXT")
            self._ensure_column(connection, "watchers", "stopped_by", "TEXT")
            self._ensure_column(connection, "watchers", "stop_reason", "TEXT")
            self._ensure_column(connection, "watchers", "stopped_at", "TEXT")
            self._ensure_column(connection, "watchers", "stop_operation_id", "TEXT")
            self._ensure_column(connection, "watchers", "graph_condition_json", "TEXT")
            self._ensure_column(connection, "watchers", "armed_revision", "INTEGER")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS team_member_tokens_hash "
                "ON team_member_tokens(token_hash)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS team_member_tokens_active_user "
                "ON team_member_tokens(user_id) WHERE revoked_at IS NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS team_invitations_creator "
                "ON team_invitations(created_by, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS team_sessions_user_expiry "
                "ON team_sessions(user_id, expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS graph_runs_campaign "
                "ON graph_runs(campaign_id, created_at, operation_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS graph_run_events_command "
                "ON graph_run_events(command_id, command_phase, event_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS graph_run_events_command_start_id "
                "ON graph_run_events(command_id) "
                "WHERE event_kind = 'command' AND command_phase = 'start'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS graph_run_events_command_exit_id "
                "ON graph_run_events(command_id) "
                "WHERE event_kind = 'command' AND command_phase = 'exit'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS graph_run_events_campaign_key_start "
                "ON graph_run_events(campaign_id, idempotency_key) "
                "WHERE event_kind = 'command' AND command_phase = 'start' "
                "AND campaign_id IS NOT NULL AND idempotency_key IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_due "
                "ON watchers(status, next_check_at, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_due_unclaimed "
                "ON watchers(status, notified, next_check_at, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_group_members "
                "ON watchers(group_id, created_at, watcher_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_group_delivery_candidates "
                "ON watchers(notified, status, group_id, consecutive_error_count)"
            )
            connection.execute(
                "UPDATE watchers SET experiment_episode_id = "
                "json_extract(continuation_json, '$.control_episode_id') "
                "WHERE experiment_episode_id IS NULL "
                "AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_experiment_episode "
                "ON watchers(project_id, node_id, experiment_episode_id, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_graph_conditions "
                "ON watchers(project_id, status, notified, graph_condition_json)"
            )
            connection.execute("DROP INDEX IF EXISTS graph_runs_active_project")
            connection.execute("DROP INDEX IF EXISTS agent_tasks_active_project")
            if issue_bootstrap:
                if stored_space_kind != "team" or initial_space_name is None:
                    raise ValueError("A bootstrap code requires a named team space.")
                if recovering_team_initialization:
                    if connection.execute("SELECT 1 FROM space_users LIMIT 1").fetchone():
                        raise ValueError("This RCP data directory already contains a space.")
                    connection.execute("DELETE FROM team_bootstrap_codes")
                bootstrap_code, code_id, code_hash = _new_enrollment_code("bootstrap")
                connection.execute(
                    """
                    INSERT INTO team_bootstrap_codes (code_id, code_hash, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (code_id, code_hash, self.now()),
                )
        return bootstrap_code

    @property
    def space_id(self) -> str:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT space_id FROM space_identity WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("RCP space identity is unavailable.")
        return _canonical_space_id(row["space_id"])

    @property
    def space_kind(self) -> SpaceKind:
        with self.connection() as connection:
            return self._space_kind_from_connection(connection)

    @property
    def space_name(self) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT space_name FROM space_identity WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("RCP space identity is unavailable.")
        value = row["space_name"]
        if value is None:
            return None
        try:
            return normalize_space_name(value)
        except ValueError as exc:
            raise RuntimeError("RCP space name is invalid.") from exc

    def space_users(self) -> list[SpaceUserRecord]:
        with self.connection() as connection:
            return self._space_users_from_connection(connection)

    def space_user(self, user_id: str) -> SpaceUserRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM space_users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._space_user_record(row) if row is not None else None

    @property
    def local_owner(self) -> SpaceUserRecord | None:
        if self.space_kind != "personal":
            return None
        users = self.space_users()
        if len(users) != 1 or users[0].identity_kind != "local_owner":
            raise RuntimeError("A personal RCP space must contain exactly one local owner.")
        return users[0]

    def rename_space(self, name: str) -> str:
        normalized = normalize_space_name(name)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._space_kind_from_connection(connection) != "team":
                raise ValueError("Only a team space has a mutable team name.")
            connection.execute(
                "UPDATE space_identity SET space_name = ? WHERE singleton = 1",
                (normalized,),
            )
        return normalized

    def enroll_team_member(self, code: str, display_name: str) -> tuple[SpaceUserRecord, str]:
        parsed = _parse_enrollment_code(code)
        if parsed is None:
            raise TeamAuthenticationError(
                "enrollment_code_invalid", "The enrollment code is invalid."
            )
        kind, code_id, supplied_hash = parsed
        now = self.now()
        error: TeamAuthenticationError | None = None
        member: SpaceUserRecord | None = None
        token: str | None = None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._space_kind_from_connection(connection) != "team":
                raise ValueError("Only a team space accepts enrollment.")
            table = "team_bootstrap_codes" if kind == "bootstrap" else "team_invitations"
            id_column = "code_id" if kind == "bootstrap" else "invitation_id"
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {id_column} = ?",  # noqa: S608
                (code_id,),
            ).fetchone()
            if row is None:
                error = TeamAuthenticationError(
                    "enrollment_code_invalid", "The enrollment code is invalid."
                )
            elif row["consumed_at"] is not None:
                error = TeamAuthenticationError(
                    "enrollment_code_consumed", "The enrollment code has already been used."
                )
            elif row["locked_at"] is not None:
                error = TeamAuthenticationError(
                    "enrollment_code_locked", "The enrollment code is locked."
                )
            elif kind == "invite" and row["expires_at"] <= now:
                error = TeamAuthenticationError(
                    "enrollment_code_expired", "The enrollment code has expired."
                )
            elif not hmac.compare_digest(row["code_hash"], supplied_hash):
                failed_attempts = int(row["failed_attempts"]) + 1
                locked_at = now if failed_attempts >= TEAM_CODE_FAILED_ATTEMPT_LIMIT else None
                connection.execute(
                    f"UPDATE {table} SET failed_attempts = ?, locked_at = ? "  # noqa: S608
                    f"WHERE {id_column} = ?",
                    (failed_attempts, locked_at, code_id),
                )
                error = TeamAuthenticationError(
                    "enrollment_code_locked" if locked_at else "enrollment_code_invalid",
                    "The enrollment code is locked."
                    if locked_at
                    else "The enrollment code is invalid.",
                )
            else:
                if kind == "bootstrap":
                    first_member = connection.execute(
                        "SELECT 1 FROM space_users LIMIT 1"
                    ).fetchone()
                    if first_member is not None:
                        error = TeamAuthenticationError(
                            "enrollment_code_consumed",
                            "The team space has already been claimed.",
                        )
                if error is None:
                    member = SpaceUserRecord(
                        user_id=str(uuid.uuid4()),
                        identity_kind="team_member",
                        display_name=display_name,
                        created_at=now,
                        updated_at=now,
                    )
                    token, token_hash = _new_member_token()
                    connection.execute(
                        """
                        INSERT INTO space_users (
                            user_id, identity_kind, display_name, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            member.user_id,
                            member.identity_kind,
                            member.display_name,
                            member.created_at,
                            member.updated_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO team_member_tokens (
                            token_id, user_id, token_hash, created_at, revoked_at
                        ) VALUES (?, ?, ?, ?, NULL)
                        """,
                        (str(uuid.uuid4()), member.user_id, token_hash, now),
                    )
                    connection.execute(
                        f"UPDATE {table} SET consumed_at = ?, consumed_by = ? "  # noqa: S608
                        f"WHERE {id_column} = ?",
                        (now, member.user_id, code_id),
                    )
        if error is not None:
            raise error
        if member is None or token is None:  # pragma: no cover - exhaustive transition above
            raise RuntimeError("RCP team enrollment did not produce a member credential.")
        return member, token

    def create_team_invitation(
        self,
        created_by: str,
    ) -> tuple[TeamInvitationRecord, str]:
        now = self.now()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(days=TEAM_INVITATION_TTL_DAYS)
        ).isoformat()
        code, invitation_id, code_hash = _new_enrollment_code("invite")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_team_member_from_connection(connection, created_by)
            connection.execute(
                """
                INSERT INTO team_invitations (
                    invitation_id, code_hash, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (invitation_id, code_hash, created_by, now, expires_at),
            )
        return (
            TeamInvitationRecord(
                invitation_id=invitation_id,
                created_by=created_by,
                created_at=now,
                expires_at=expires_at,
                failed_attempts=0,
            ),
            code,
        )

    def team_invitations(self, created_by: str) -> list[TeamInvitationRecord]:
        with self.connection() as connection:
            self._require_team_member_from_connection(connection, created_by)
            rows = connection.execute(
                """
                SELECT invitation_id, created_by, created_at, expires_at,
                       consumed_at, consumed_by, failed_attempts, locked_at
                FROM team_invitations
                WHERE created_by = ?
                ORDER BY created_at DESC, invitation_id
                """,
                (created_by,),
            ).fetchall()
        return [TeamInvitationRecord.model_validate(dict(row)) for row in rows]

    def create_team_session(self, token: str) -> tuple[str, SpaceUserRecord]:
        if (
            not isinstance(token, str)
            or len(token) > TEAM_MEMBER_TOKEN_MAX_LENGTH
            or not token.startswith("rcp_")
        ):
            raise TeamAuthenticationError(
                "team_token_invalid", "The member token is invalid or revoked."
            )
        token_hash = _sha256(token)
        now = self.now()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(days=TEAM_SESSION_IDLE_DAYS)
        ).isoformat()
        session, session_hash = _new_session_token()
        member: SpaceUserRecord | None = None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._space_kind_from_connection(connection) != "team":
                raise ValueError("Only a team space accepts member tokens.")
            row = connection.execute(
                """
                SELECT user_id, token_hash FROM team_member_tokens
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
            if row is not None and hmac.compare_digest(row["token_hash"], token_hash):
                member = self._require_team_member_from_connection(connection, row["user_id"])
                connection.execute(
                    """
                    INSERT INTO team_sessions (
                        session_hash, user_id, created_at, last_seen_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (session_hash, member.user_id, now, now, expires_at),
                )
        if member is None:
            raise TeamAuthenticationError(
                "team_token_invalid", "The member token is invalid or revoked."
            )
        return session, member

    def resolve_team_session(self, session: str | None) -> SpaceUserRecord | None:
        if (
            not session
            or len(session) > TEAM_SESSION_TOKEN_MAX_LENGTH
            or not session.startswith("rcp_session_")
        ):
            return None
        session_hash = _sha256(session)
        now = self.now()
        expires_at = (
            datetime.fromisoformat(now) + timedelta(days=TEAM_SESSION_IDLE_DAYS)
        ).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM team_sessions WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
            if row is None or not hmac.compare_digest(row["session_hash"], session_hash):
                return None
            if row["expires_at"] <= now:
                connection.execute(
                    "DELETE FROM team_sessions WHERE session_hash = ?", (session_hash,)
                )
                return None
            member = self._space_user_from_connection(connection, row["user_id"])
            if member is None or member.identity_kind != "team_member":
                connection.execute(
                    "DELETE FROM team_sessions WHERE session_hash = ?", (session_hash,)
                )
                return None
            connection.execute(
                """
                UPDATE team_sessions SET last_seen_at = ?, expires_at = ?
                WHERE session_hash = ?
                """,
                (now, expires_at, session_hash),
            )
            return member

    def delete_team_session(self, session: str | None) -> None:
        if not session:
            return
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM team_sessions WHERE session_hash = ?", (_sha256(session),)
            )

    def _require_authenticating_team_session(
        self,
        connection: sqlite3.Connection,
        session: str | None,
        user_id: str,
        now: str,
    ) -> None:
        if (
            not session
            or len(session) > TEAM_SESSION_TOKEN_MAX_LENGTH
            or not session.startswith("rcp_session_")
        ):
            raise TeamAuthenticationError(
                "team_session_invalid", "The browser session is invalid or expired."
            )
        session_hash = _sha256(session)
        row = connection.execute(
            "SELECT session_hash, user_id, expires_at FROM team_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
        if (
            row is None
            or not hmac.compare_digest(row["session_hash"], session_hash)
            or row["user_id"] != user_id
            or row["expires_at"] <= now
        ):
            raise TeamAuthenticationError(
                "team_session_invalid", "The browser session is invalid or expired."
            )

    def rotate_team_token(
        self,
        user_id: str,
        *,
        authenticating_session: str | None = None,
    ) -> str:
        now = self.now()
        token, token_hash = _new_member_token()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_team_member_from_connection(connection, user_id)
            if authenticating_session is not None:
                self._require_authenticating_team_session(
                    connection, authenticating_session, user_id, now
                )
            connection.execute(
                "UPDATE team_member_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.execute("DELETE FROM team_sessions WHERE user_id = ?", (user_id,))
            connection.execute(
                """
                INSERT INTO team_member_tokens (
                    token_id, user_id, token_hash, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (str(uuid.uuid4()), user_id, token_hash, now),
            )
        return token

    def revoke_team_token(
        self,
        user_id: str,
        *,
        authenticating_session: str | None = None,
    ) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_team_member_from_connection(connection, user_id)
            if authenticating_session is not None:
                self._require_authenticating_team_session(
                    connection, authenticating_session, user_id, now
                )
            connection.execute(
                "UPDATE team_member_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            connection.execute("DELETE FROM team_sessions WHERE user_id = ?", (user_id,))

    def preprovision_team_member(self, display_name: str | None = None) -> SpaceUserRecord:
        now = self.now()
        member = SpaceUserRecord(
            user_id=str(uuid.uuid4()),
            identity_kind="team_member",
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._space_kind_from_connection(connection) != "team":
                raise ValueError("Only a team space can preprovision team members.")
            connection.execute(
                """
                INSERT INTO space_users (
                    user_id, identity_kind, display_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    member.user_id,
                    member.identity_kind,
                    member.display_name,
                    member.created_at,
                    member.updated_at,
                ),
            )
        return member

    def rename_space_user(
        self,
        user_id: str,
        display_name: str | None,
    ) -> SpaceUserRecord:
        _canonical_uuid4(user_id, label="user identity")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM space_users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown RCP space user {user_id}.")
            current = self._space_user_record(row)
            updated = SpaceUserRecord.model_validate(
                {
                    **current.model_dump(),
                    "display_name": display_name,
                    "updated_at": self.now(),
                }
            )
            connection.execute(
                """
                UPDATE space_users
                SET display_name = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (updated.display_name, updated.updated_at, user_id),
            )
        return updated

    @staticmethod
    def _space_kind_from_connection(connection: sqlite3.Connection) -> SpaceKind:
        row = connection.execute(
            "SELECT space_kind FROM space_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("RCP space identity is unavailable.")
        return _stored_space_kind(row["space_kind"])

    @classmethod
    def _space_users_from_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> list[SpaceUserRecord]:
        rows = connection.execute(
            "SELECT * FROM space_users ORDER BY created_at, user_id"
        ).fetchall()
        return [cls._space_user_record(row) for row in rows]

    @classmethod
    def _space_user_from_connection(
        cls,
        connection: sqlite3.Connection,
        user_id: str,
    ) -> SpaceUserRecord | None:
        row = connection.execute(
            "SELECT * FROM space_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return cls._space_user_record(row) if row is not None else None

    @classmethod
    def _require_team_member_from_connection(
        cls,
        connection: sqlite3.Connection,
        user_id: str,
    ) -> SpaceUserRecord:
        if cls._space_kind_from_connection(connection) != "team":
            raise ValueError("Only a team space has team members.")
        member = cls._space_user_from_connection(connection, user_id)
        if member is None or member.identity_kind != "team_member":
            raise KeyError(f"Unknown RCP team member {user_id}.")
        return member

    @staticmethod
    def _space_user_record(row: sqlite3.Row) -> SpaceUserRecord:
        try:
            return SpaceUserRecord.model_validate(dict(row))
        except (RuntimeError, ValueError) as exc:
            raise RuntimeError("RCP space user record is invalid.") from exc

    def provider_skill_inventory(
        self,
        provider: str,
        host: str,
        configured_binary: str | None,
    ) -> ProviderSkillInventoryRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM provider_skill_inventories
                WHERE provider = ? AND host = ? AND configured_binary = ?
                """,
                (provider, host, configured_binary or ""),
            ).fetchone()
        if row is None:
            return None
        return ProviderSkillInventoryRecord(
            provider=row["provider"],
            host=row["host"],
            configured_binary=row["configured_binary"],
            resolved_binary=row["resolved_binary"],
            provider_version=row["provider_version"],
            command=json.loads(row["command_json"]),
            protocol=row["protocol"],
            skills=[ProviderSkill.model_validate(item) for item in json.loads(row["skills_json"])],
            inventory_hash=row["inventory_hash"],
            status=row["status"],
            diagnostic=row["diagnostic"],
            refreshed_at=row["refreshed_at"],
            updated_at=row["updated_at"],
        )

    def mark_provider_skill_inventory_refreshing(
        self,
        provider: str,
        host: str,
        configured_binary: str | None,
        *,
        updated_at: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_skill_inventories (
                    provider, host, configured_binary, status, updated_at
                ) VALUES (?, ?, ?, 'refreshing', ?)
                ON CONFLICT(provider, host, configured_binary) DO UPDATE SET
                    status = 'refreshing', diagnostic = NULL, updated_at = excluded.updated_at
                """,
                (provider, host, configured_binary or "", updated_at),
            )

    def save_provider_skill_inventory_success(
        self,
        provider: str,
        host: str,
        configured_binary: str | None,
        *,
        resolved_binary: str,
        provider_version: str,
        command: list[str],
        protocol: str,
        skills: list[ProviderSkill],
        inventory_hash: str,
        refreshed_at: str,
    ) -> None:
        skill_payload = [item.model_dump(mode="json") for item in skills]
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_skill_inventories (
                    provider, host, configured_binary, resolved_binary,
                    provider_version, command_json, protocol, skills_json,
                    inventory_hash, status, diagnostic, refreshed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'fresh', NULL, ?, ?)
                ON CONFLICT(provider, host, configured_binary) DO UPDATE SET
                    resolved_binary = excluded.resolved_binary,
                    provider_version = excluded.provider_version,
                    command_json = excluded.command_json,
                    protocol = excluded.protocol,
                    skills_json = excluded.skills_json,
                    inventory_hash = excluded.inventory_hash,
                    status = 'fresh',
                    diagnostic = NULL,
                    refreshed_at = excluded.refreshed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    host,
                    configured_binary or "",
                    resolved_binary,
                    provider_version,
                    json.dumps(command, separators=(",", ":")),
                    protocol,
                    json.dumps(skill_payload, sort_keys=True, separators=(",", ":")),
                    inventory_hash,
                    refreshed_at,
                    refreshed_at,
                ),
            )

    def save_provider_skill_inventory_failure(
        self,
        provider: str,
        host: str,
        configured_binary: str | None,
        *,
        diagnostic: str,
        updated_at: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO provider_skill_inventories (
                    provider, host, configured_binary, status, diagnostic, updated_at
                ) VALUES (?, ?, ?, 'unavailable', ?, ?)
                ON CONFLICT(provider, host, configured_binary) DO UPDATE SET
                    status = CASE
                        WHEN provider_skill_inventories.refreshed_at IS NULL
                        THEN 'unavailable'
                        ELSE 'stale'
                    END,
                    diagnostic = excluded.diagnostic,
                    updated_at = excluded.updated_at
                """,
                (provider, host, configured_binary or "", diagnostic, updated_at),
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        name: str,
        definition: str,
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if name not in columns:
                    raise

    def project_by_locator(self, locator: str) -> ProjectRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE locator = ?", (locator,)
            ).fetchone()
        return self._project_record(row) if row else None

    def project(self, project_id: str) -> ProjectRecord | None:
        with self.connection() as connection:
            canonical_project_id = self._resolve_project_id_from_connection(connection, project_id)
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (canonical_project_id,)
            ).fetchone()
        return self._project_record(row) if row else None

    def resolve_project_id(self, project_id: str) -> str:
        with self.connection() as connection:
            return self._resolve_project_id_from_connection(connection, project_id)

    def project_aliases(self) -> dict[str, str]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT alias_id, canonical_project_id
                FROM project_aliases
                ORDER BY alias_id
                """
            ).fetchall()
        aliases: dict[str, str] = {}
        for row in rows:
            aliases[str(row["alias_id"])] = _canonical_uuid4(
                row["canonical_project_id"], label="canonical project identity"
            )
        return aliases

    def projects(self) -> list[ProjectRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM projects
                ORDER BY added_at DESC, name COLLATE NOCASE, project_id
                """
            ).fetchall()
        return [self._project_record(row) for row in rows]

    def migrate_project_identity(
        self,
        old_project_id: str,
        canonical_project_id: str,
        home_space_id: str,
    ) -> ProjectRecord:
        try:
            canonical_project_id = _canonical_uuid4(
                canonical_project_id, label="canonical project identity"
            )
            home_space_id = _canonical_uuid4(home_space_id, label="project home space identity")
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                alias = connection.execute(
                    "SELECT canonical_project_id FROM project_aliases WHERE alias_id = ?",
                    (old_project_id,),
                ).fetchone()
                if alias is not None and alias["canonical_project_id"] != canonical_project_id:
                    raise ValueError(
                        f"Project alias {old_project_id!r} already resolves to "
                        f"{alias['canonical_project_id']!r}."
                    )
                canonical_alias = connection.execute(
                    "SELECT canonical_project_id FROM project_aliases WHERE alias_id = ?",
                    (canonical_project_id,),
                ).fetchone()
                if canonical_alias is not None:
                    raise ValueError(
                        f"Canonical project id {canonical_project_id!r} is already an alias."
                    )

                old_row = connection.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (old_project_id,)
                ).fetchone()
                canonical_row = connection.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (canonical_project_id,)
                ).fetchone()

                if old_project_id == canonical_project_id:
                    if old_row is None:
                        raise KeyError(old_project_id)
                    stored_home = old_row["home_space_id"]
                    if stored_home is not None and stored_home != home_space_id:
                        raise ValueError(
                            f"Project {canonical_project_id!r} already belongs to {stored_home!r}."
                        )
                    if stored_home is None:
                        connection.execute(
                            "UPDATE projects SET home_space_id = ? WHERE project_id = ?",
                            (home_space_id, canonical_project_id),
                        )
                    row = connection.execute(
                        "SELECT * FROM projects WHERE project_id = ?", (canonical_project_id,)
                    ).fetchone()
                    assert row is not None
                    return self._project_record(row)

                if old_row is None:
                    if alias is None:
                        if canonical_row is not None:
                            raise ValueError(
                                f"Project identity destination {canonical_project_id!r} "
                                "already exists without the requested alias."
                            )
                        raise KeyError(old_project_id)
                    if canonical_row is None:
                        raise KeyError(canonical_project_id)
                    if canonical_row["home_space_id"] != home_space_id:
                        raise ValueError(
                            f"Project {canonical_project_id!r} already belongs to "
                            f"{canonical_row['home_space_id']!r}."
                        )
                    for table in _PROJECT_ID_TABLES:
                        if (
                            connection.execute(
                                f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1",
                                (old_project_id,),
                            ).fetchone()
                            is not None
                        ):
                            raise RuntimeError(
                                f"Project alias {old_project_id!r} still has rows in {table}."
                            )
                    return self._project_record(canonical_row)

                if canonical_row is not None:
                    raise ValueError(
                        f"Project identity destination {canonical_project_id!r} "
                        "already contains a project registration."
                    )
                for table in _PROJECT_ID_TABLES[1:]:
                    if (
                        connection.execute(
                            f"SELECT 1 FROM {table} WHERE project_id = ? LIMIT 1",
                            (canonical_project_id,),
                        ).fetchone()
                        is not None
                    ):
                        raise ValueError(
                            f"Project identity destination {canonical_project_id!r} "
                            f"already contains rows in {table}."
                        )

                connection.execute(
                    """
                    UPDATE projects
                    SET project_id = ?, home_space_id = ?
                    WHERE project_id = ?
                    """,
                    (canonical_project_id, home_space_id, old_project_id),
                )
                for table in _PROJECT_ID_TABLES[1:]:
                    connection.execute(
                        f"UPDATE {table} SET project_id = ? WHERE project_id = ?",
                        (canonical_project_id, old_project_id),
                    )
                if alias is None:
                    connection.execute(
                        """
                        INSERT INTO project_aliases(alias_id, canonical_project_id)
                        VALUES (?, ?)
                        """,
                        (old_project_id, canonical_project_id),
                    )
                row = connection.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (canonical_project_id,)
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        "Canonical project registration disappeared during migration."
                    )
                return self._project_record(row)
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValueError(
                    f"Project identity migration to {canonical_project_id!r} conflicted."
                ) from exc
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _resolve_project_id_from_connection(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> str:
        row = connection.execute(
            "SELECT canonical_project_id FROM project_aliases WHERE alias_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return project_id
        return _canonical_uuid4(row["canonical_project_id"], label="canonical project identity")

    def project_deletion_stages(self, project_id: str) -> list[ProjectStageRecord]:
        """Return the saved scratch stages after proving deletion is currently safe."""
        with self.connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            if (
                connection.execute(
                    """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                    (project_id,),
                ).fetchone()
                is not None
            ):
                raise ValueError("Pause the active agent task before deleting this project.")
            rows = connection.execute(
                """
                SELECT DISTINCT COALESCE(stage_host, '') AS host, stage_root AS root
                FROM graph_runs
                WHERE project_id = ? AND stage_root IS NOT NULL
                """,
                (project_id,),
            ).fetchall()
        return [ProjectStageRecord.model_validate(dict(row)) for row in rows]

    def delete_project_records(self, project_id: str) -> dict[str, int]:
        """Atomically delete every database row owned by one registration.

        The active-task check is repeated under a write lock so a task cannot be
        launched between the catalog's cleanup preflight and the database commit.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if (
                    connection.execute(
                        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                    ).fetchone()
                    is None
                ):
                    raise KeyError(project_id)
                if (
                    connection.execute(
                        """
                    SELECT 1 FROM graph_runs
                    WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                    LIMIT 1
                    """,
                        (project_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("Pause the active agent task before deleting this project.")

                operation_ids = connection.execute(
                    "SELECT operation_id FROM graph_runs WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
                operation_count = len(operation_ids)
                counts = {
                    "paper_drafts": connection.execute(
                        "DELETE FROM paper_drafts WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "writing_sessions": connection.execute(
                        "DELETE FROM writing_sessions WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "chat_session_contexts": connection.execute(
                        "DELETE FROM chat_session_contexts WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "result_views": connection.execute(
                        "DELETE FROM result_views WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "watchers": connection.execute(
                        "DELETE FROM watchers WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "experiment_episodes": connection.execute(
                        "DELETE FROM experiment_episodes WHERE project_id = ?", (project_id,)
                    ).rowcount,
                }
                campaign_ids = connection.execute(
                    "SELECT campaign_id FROM campaigns WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
                if campaign_ids:
                    for table in (
                        "campaign_recoveries",
                        "campaign_messages",
                        "campaign_reports",
                        "campaign_invocations",
                    ):
                        counts[table] = connection.execute(
                            f"""
                            DELETE FROM {table}
                            WHERE campaign_id IN (
                                SELECT campaign_id FROM campaigns WHERE project_id = ?
                            )
                            """,
                            (project_id,),
                        ).rowcount
                counts["campaigns"] = connection.execute(
                    "DELETE FROM campaigns WHERE project_id = ?", (project_id,)
                ).rowcount
                connection.execute("DELETE FROM agent_usage WHERE project_id = ?", (project_id,))
                for table in (
                    "graph_run_outputs",
                    "graph_run_events",
                    "graph_run_receipts",
                    "graph_run_contracts",
                ):
                    counts[table] = connection.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE operation_id IN (
                            SELECT operation_id FROM graph_runs WHERE project_id = ?
                        )
                        """,
                        (project_id,),
                    ).rowcount
                counts["graph_runs"] = connection.execute(
                    "DELETE FROM graph_runs WHERE project_id = ?", (project_id,)
                ).rowcount
                assert counts["graph_runs"] == operation_count
                counts["projects"] = connection.execute(
                    "DELETE FROM projects WHERE project_id = ?", (project_id,)
                ).rowcount
                if counts["projects"] != 1:
                    raise RuntimeError("Project registration disappeared during deletion")
            except Exception:
                connection.rollback()
                raise
        return counts

    def upsert_project(self, record: ProjectRecord) -> ProjectRecord:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, home_space_id, locator, name, state_location, state_remote, added_at,
                    last_opened_at, revision, primary_question, attention_count,
                    last_refresh_at, reachable, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    locator = excluded.locator,
                    name = excluded.name,
                    state_location = excluded.state_location,
                    state_remote = excluded.state_remote
                """,
                (
                    record.project_id,
                    record.home_space_id,
                    record.locator,
                    record.name,
                    record.state_location,
                    int(record.state_remote),
                    record.added_at,
                    record.last_opened_at,
                    record.revision,
                    record.primary_question,
                    record.attention_count,
                    record.last_refresh_at,
                    None if record.reachable is None else int(record.reachable),
                    record.error,
                ),
            )
        stored = self.project(record.project_id)
        assert stored is not None
        return stored

    def update_project_summary(
        self,
        project_id: str,
        *,
        revision: int,
        primary_question: str | None,
        attention_count: int,
        last_refresh_at: str | None,
        reachable: bool,
        error: str | None,
    ) -> ProjectRecord:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET last_opened_at = ?, revision = ?, primary_question = ?,
                    attention_count = ?, last_refresh_at = ?, reachable = ?, error = ?
                WHERE project_id = ?
                """,
                (
                    self.now(),
                    revision,
                    primary_question,
                    attention_count,
                    last_refresh_at,
                    int(reachable),
                    error,
                    project_id,
                ),
            )
        stored = self.project(project_id)
        if stored is None:
            raise KeyError(project_id)
        return stored

    def migrate_legacy_project_data(self, legacy_id: str, project_id: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT home_space_id FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            target_alias = connection.execute(
                "SELECT canonical_project_id FROM project_aliases WHERE alias_id = ?",
                (project_id,),
            ).fetchone()
            space = connection.execute(
                "SELECT space_id FROM space_identity WHERE singleton = 1"
            ).fetchone()
            try:
                _canonical_uuid4(project_id, label="canonical project identity")
            except RuntimeError:
                canonical_target = False
            else:
                canonical_target = True
            if (
                target is None
                or target_alias is not None
                or space is None
                or target["home_space_id"] != space["space_id"]
                or not canonical_target
            ):
                raise ValueError(
                    f"Legacy project data migration target {project_id!r} is not an exact "
                    "canonical project registration."
                )
            if legacy_id == project_id:
                return

            legacy_project = connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?",
                (legacy_id,),
            ).fetchone()
            if legacy_project is not None:
                raise ValueError(
                    f"Legacy project data migration source {legacy_id!r} is already a "
                    "registered canonical project."
                )
            legacy_alias = connection.execute(
                "SELECT canonical_project_id FROM project_aliases WHERE alias_id = ?",
                (legacy_id,),
            ).fetchone()
            if legacy_alias is not None and legacy_alias["canonical_project_id"] != project_id:
                raise ValueError(
                    f"Legacy project data migration source alias {legacy_id!r} belongs to "
                    f"canonical project {legacy_alias['canonical_project_id']!r}, not "
                    f"{project_id!r}."
                )

            connection.execute(
                """
                INSERT OR IGNORE INTO paper_drafts (
                    project_id, content, base_hash, updated_at, cursor_state, ancestor_content
                )
                SELECT ?, content, base_hash, updated_at, cursor_state, ancestor_content
                FROM paper_drafts
                WHERE project_id = ?
                """,
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE writing_sessions SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE chat_session_contexts SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE result_views SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE graph_runs SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE campaigns SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE watchers SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE experiment_episodes SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )

    def create_result_view(self, record: ResultViewRecord) -> ResultViewRecord:
        """Insert one private result-view binding without storing its bytes."""
        record = ResultViewRecord.model_validate(record)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO result_views (
                        view_id, project_id, experiment_id, chat_id,
                        origin_operation_id, latest_operation_id,
                        provider, model, reasoning, run_on,
                        native_session_id, stage_host, stage_root, source_name,
                        content_sha256, size_bytes, created_at, updated_at, expires_at,
                        kept_filename, kept_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.view_id,
                        record.project_id,
                        record.experiment_id,
                        record.chat_id,
                        record.origin_operation_id,
                        record.latest_operation_id,
                        record.provider,
                        record.model,
                        record.reasoning,
                        record.run_on,
                        record.native_session_id,
                        record.stage_host,
                        record.stage_root,
                        record.source_name,
                        record.content_sha256,
                        record.size_bytes,
                        record.created_at,
                        record.updated_at,
                        record.expires_at,
                        record.kept_filename,
                        record.kept_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Result view {record.view_id!r} already exists.") from exc
        return record

    def result_view(
        self,
        view_id: str,
        *,
        include_expired: bool = False,
        as_of: datetime | None = None,
    ) -> ResultViewRecord | None:
        """Return one visible result view, unless diagnostics explicitly include expiry."""
        record = self.result_view_for_diagnostics(view_id)
        if record is None or include_expired or _result_view_is_visible(record, as_of=as_of):
            return record
        return None

    def result_view_for_diagnostics(self, view_id: str) -> ResultViewRecord | None:
        """Return private metadata even after a temporary view expires."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM result_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
        return self._result_view_record(row) if row is not None else None

    def list_result_views(
        self,
        project_id: str,
        *,
        experiment_id: str | None = None,
        chat_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[ResultViewRecord]:
        """List visible views while retaining kept records past scratch expiry."""
        clauses = ["project_id = ?"]
        values: list[str] = [project_id]
        if experiment_id is not None:
            clauses.append("experiment_id = ?")
            values.append(experiment_id)
        if chat_id is not None:
            clauses.append("chat_id = ?")
            values.append(chat_id)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM result_views
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, view_id
                """,
                values,
            ).fetchall()
        records = [self._result_view_record(row) for row in rows]
        return [record for record in records if _result_view_is_visible(record, as_of=as_of)]

    def result_view_descriptor(
        self,
        record: ResultViewRecord,
        *,
        as_of: datetime | None = None,
    ) -> ResultViewDescriptor:
        """Project private storage metadata onto the path-free public contract."""
        record = ResultViewRecord.model_validate(record)
        is_temporary = record.kept_filename is None
        return ResultViewDescriptor(
            view_id=record.view_id,
            chat_id=record.chat_id,
            experiment_id=record.experiment_id,
            name=record.source_name,
            media_type="text/html",
            state="temporary" if is_temporary else "kept",
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            kept_filename=record.kept_filename,
            kept_at=record.kept_at,
            can_revise=is_temporary and _result_view_is_visible(record, as_of=as_of),
        )

    def list_result_view_descriptors(
        self,
        project_id: str,
        *,
        experiment_id: str | None = None,
        chat_id: str | None = None,
        as_of: datetime | None = None,
    ) -> list[ResultViewDescriptor]:
        return [
            self.result_view_descriptor(record, as_of=as_of)
            for record in self.list_result_views(
                project_id,
                experiment_id=experiment_id,
                chat_id=chat_id,
                as_of=as_of,
            )
        ]

    def revise_result_view(
        self,
        view_id: str,
        *,
        expected_content_sha256: str,
        latest_operation_id: str,
        content_sha256: str,
        size_bytes: int,
        updated_at: str,
        expires_at: str,
    ) -> ResultViewRecord:
        """CAS one revision onto the same stable view identity."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM result_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            if row is None:
                raise KeyError(view_id)
            current = self._result_view_record(row)
            if current.kept_filename is not None:
                raise ResultViewConflict("a kept result view cannot be revised")
            if current.content_sha256 != expected_content_sha256:
                raise ResultViewConflict("result view changed before this revision was recorded")
            revised = ResultViewRecord.model_validate(
                {
                    **current.model_dump(mode="python"),
                    "latest_operation_id": latest_operation_id,
                    "content_sha256": content_sha256,
                    "size_bytes": size_bytes,
                    "updated_at": updated_at,
                    "expires_at": expires_at,
                }
            )
            updated = connection.execute(
                """
                UPDATE result_views
                SET latest_operation_id = ?, content_sha256 = ?, size_bytes = ?,
                    updated_at = ?, expires_at = ?
                WHERE view_id = ? AND content_sha256 = ? AND kept_filename IS NULL
                """,
                (
                    revised.latest_operation_id,
                    revised.content_sha256,
                    revised.size_bytes,
                    revised.updated_at,
                    revised.expires_at,
                    view_id,
                    expected_content_sha256,
                ),
            ).rowcount
            if updated != 1:
                raise ResultViewConflict("result view changed before this revision was recorded")
        return revised

    def refresh_result_view_expiry(
        self,
        project_id: str,
        chat_id: str,
        *,
        expires_at: str,
        as_of: datetime | None = None,
    ) -> int:
        """Extend active unkept view retention without reviving expired views."""
        requested_expiry = _required_timestamp(expires_at)
        current = as_of or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("result view refresh time must include a timezone")
        current = current.astimezone(UTC)
        refreshed = 0
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT view_id, expires_at FROM result_views
                WHERE project_id = ? AND chat_id = ? AND kept_filename IS NULL
                """,
                (project_id, chat_id),
            ).fetchall()
            for row in rows:
                current_expiry = _required_timestamp(row["expires_at"])
                if current_expiry <= current or requested_expiry <= current_expiry:
                    continue
                refreshed += connection.execute(
                    """
                    UPDATE result_views SET expires_at = ?
                    WHERE view_id = ? AND expires_at = ? AND kept_filename IS NULL
                    """,
                    (expires_at, row["view_id"], row["expires_at"]),
                ).rowcount
        return refreshed

    def mark_result_view_kept(
        self,
        view_id: str,
        *,
        expected_content_sha256: str,
        kept_filename: str,
        kept_at: str,
    ) -> ResultViewRecord:
        """Remember Keep once, bound to the exact bytes that were copied."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM result_views WHERE view_id = ?",
                (view_id,),
            ).fetchone()
            if row is None:
                raise KeyError(view_id)
            current = self._result_view_record(row)
            if current.kept_filename is not None:
                if current.content_sha256 != expected_content_sha256:
                    raise ResultViewConflict("result view changed before Keep was recorded")
                return current
            if current.content_sha256 != expected_content_sha256:
                raise ResultViewConflict("result view changed before Keep was recorded")
            kept = ResultViewRecord.model_validate(
                {
                    **current.model_dump(mode="python"),
                    "kept_filename": kept_filename,
                    "kept_at": kept_at,
                }
            )
            updated = connection.execute(
                """
                UPDATE result_views
                SET kept_filename = ?, kept_at = ?
                WHERE view_id = ? AND kept_filename IS NULL AND content_sha256 = ?
                """,
                (kept.kept_filename, kept.kept_at, view_id, expected_content_sha256),
            ).rowcount
            if updated != 1:
                raise ResultViewConflict("result view changed before Keep was recorded")
        return kept

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
    def _validate_experiment_task_insert(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        request = record.request
        if request.get("patch_kind") != "experiment_loop":
            return

        recovery_binding_keys = (*_EXPERIMENT_EPISODE_PINNED_FIELDS, "control_invocation")
        node_id = request.get("control_node_id")
        control_revision = request.get("control_revision")
        episode_id = request.get("control_episode_id")
        invocation = request.get("control_invocation")
        ceiling = request.get("control_invocation_ceiling")
        decision_bundle = request.get("control_decision_bundle")
        completion_criteria = request.get("control_completion_criteria")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("A bounded experiment-loop task must name its control node.")
        if not isinstance(control_revision, int) or isinstance(control_revision, bool):
            raise ValueError("A bounded experiment-loop task must pin its control revision.")
        if not isinstance(decision_bundle, list):
            raise ValueError("A bounded experiment-loop task must pin its governing decisions.")
        if not isinstance(completion_criteria, list) or any(
            not isinstance(item, str) for item in completion_criteria
        ):
            raise ValueError("A bounded experiment-loop task must pin its completion criteria.")
        if not isinstance(episode_id, str):
            raise ValueError("A bounded experiment-loop task must name a valid episode id.")
        try:
            uuid.UUID(episode_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "A bounded experiment-loop task must name a valid episode id."
            ) from exc
        if not isinstance(invocation, int) or isinstance(invocation, bool) or invocation < 1:
            raise ValueError("A bounded experiment-loop task must name its invocation number.")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("A bounded experiment-loop task must pin its invocation ceiling.")
        if invocation > ceiling:
            raise ValueError("The experiment-loop invocation exceeds its pinned ceiling.")

        if record.parent_operation_id:
            parent = connection.execute(
                """
                SELECT project_id, kind, status, attempt, request_json, result_json
                FROM graph_runs WHERE operation_id = ?
                """,
                (record.parent_operation_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("An experiment-loop recovery task must have its parent task.")
            if parent["project_id"] != record.project_id or parent["kind"] != record.kind:
                raise ValueError("An experiment-loop recovery task must preserve its task scope.")
            parent_request = json.loads(parent["request_json"])
            if any(
                _experiment_pinned_value(parent_request, key)
                != _experiment_pinned_value(request, key)
                for key in recovery_binding_keys
            ):
                raise ValueError(
                    "An experiment-loop recovery task must preserve its control binding and "
                    "pinned configuration."
                )
            parent_result = json.loads(parent["result_json"]) if parent["result_json"] else None
            graph_update = (
                parent_result.get("graph_update") if isinstance(parent_result, dict) else None
            )
            patch_only_repair = (
                request.get("message") is None
                and parent["status"] == "succeeded"
                and isinstance(graph_update, dict)
                and graph_update.get("status") == "rejected"
                and graph_update.get("repairable") is False
            )
            if not patch_only_repair:
                AppStore._validate_experiment_recovery_claim(
                    connection,
                    record,
                    parent,
                    parent_request,
                )
            else:
                AppStore._validate_current_experiment_graph_repair(
                    connection,
                    project_id=record.project_id,
                    control_node_id=node_id,
                    episode_id=episode_id,
                    invocation=invocation,
                    operation_id=record.parent_operation_id,
                )
            return

        trigger = request.get("trigger")
        if trigger not in {"experiment_run", "watcher"}:
            raise ValueError("A root experiment-loop task must be a Run or watcher invocation.")
        rows = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            """,
            (record.project_id, node_id, episode_id),
        ).fetchall()
        prior = [json.loads(row["request_json"]) for row in rows]
        if any(
            _experiment_pinned_value(item, key) != _experiment_pinned_value(request, key)
            for item in prior
            for key in _EXPERIMENT_EPISODE_PINNED_FIELDS
        ):
            raise ValueError("An experiment-loop episode cannot change its pinned configuration.")
        expected = max((int(item["control_invocation"]) for item in prior), default=0) + 1
        if invocation != expected:
            raise ValueError(
                f"Experiment-loop invocation {invocation} is out of sequence; expected {expected}."
            )
        if invocation == 1 and prior:
            raise ValueError("An experiment-loop episode may have only one first invocation.")
        if trigger == "experiment_run" and invocation != 1:
            raise ValueError("A human Run must start at experiment-loop invocation 1.")
        if trigger == "watcher" and not prior:
            raise ValueError("An automatic watcher wake requires an existing loop episode.")
        if trigger == "watcher":
            AppStore._validate_experiment_wake_binding(connection, record)

    @staticmethod
    def _validate_experiment_wake_binding(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        """Prove the saved native session before an automatic wake spends budget."""

        request = record.request
        episode_id = request.get("control_episode_id")
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("An automatic Experiment wake requires its episode session id.")
        if record.native_session_id != session_id or not record.stage_root:
            raise ValueError(
                "An automatic Experiment wake requires its exact saved session and stage."
            )
        episode = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is None or episode["stop_requested_at"] is not None:
            raise ValueError("The automatic Experiment wake has no active episode binding.")
        binding_task = connection.execute(
            "SELECT request_json FROM graph_runs WHERE operation_id = ?",
            (episode["last_turn_operation_id"],),
        ).fetchone()
        if binding_task is None:
            raise ValueError("The automatic Experiment wake has no active binding task.")
        binding_request = json.loads(binding_task["request_json"])
        expected = {
            "project_id": record.project_id,
            "control_node_id": request.get("control_node_id"),
            "provider": request.get("provider"),
            "execution_machine": request.get("run_on"),
            "native_session_id": session_id,
            "stage_host": record.stage_host or "",
            "stage_root": record.stage_root,
            "chat_id": request.get("chat_id"),
            "model": request.get("model"),
            "reasoning": request.get("reasoning"),
        }
        actual = {
            "project_id": episode["project_id"],
            "control_node_id": episode["control_node_id"],
            "provider": episode["provider"],
            "execution_machine": episode["execution_machine"],
            "native_session_id": episode["native_session_id"],
            "stage_host": episode["stage_host"] or "",
            "stage_root": episode["stage_root"],
            "chat_id": episode["chat_id"],
            "model": binding_request.get("model"),
            "reasoning": binding_request.get("reasoning"),
        }
        mismatched = sorted(key for key, value in expected.items() if actual[key] != value)
        if (episode["execution_host"] or "") != (record.stage_host or ""):
            mismatched.append("execution_host")
        if mismatched:
            raise ValueError(
                "The automatic Experiment wake no longer matches its episode binding: "
                + ", ".join(sorted(set(mismatched)))
            )

    @staticmethod
    def _validate_experiment_recovery_claim(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
        parent: sqlite3.Row,
        parent_request: dict[str, object],
    ) -> None:
        abandoned = connection.execute(
            """
            SELECT 1 FROM graph_run_receipts
            WHERE operation_id = ? AND category = 'experiment_recovery_abandoned'
            LIMIT 1
            """,
            (record.parent_operation_id,),
        ).fetchone()
        if abandoned is not None:
            raise ValueError("Stop loop already abandoned recovery of this Experiment task.")
        if parent["status"] not in {"paused", "interrupted", "failed"}:
            raise ValueError("Only the latest unresolved loop task can be resumed or retried.")
        if record.attempt != int(parent["attempt"]) + 1:
            raise ValueError("A loop recovery task must advance its provider-attempt lineage.")
        child = connection.execute(
            "SELECT 1 FROM graph_runs WHERE parent_operation_id = ? LIMIT 1",
            (record.parent_operation_id,),
        ).fetchone()
        if child is not None:
            raise ValueError("This loop task already has a recovery child.")
        newest_root = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (record.project_id, parent_request["control_node_id"]),
        ).fetchone()
        if newest_root is None:
            raise ValueError("The loop episode root is no longer available.")
        newest_request = json.loads(newest_root["request_json"])
        if newest_request.get("control_episode_id") != parent_request.get(
            "control_episode_id"
        ) or newest_request.get("control_invocation") != parent_request.get("control_invocation"):
            raise ValueError("Only the newest loop episode and invocation can be recovered.")
        newer_attempt = connection.execute(
            """
            SELECT 1 FROM graph_runs
            WHERE project_id = ?
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
              AND json_extract(request_json, '$.control_invocation') = ?
              AND attempt > ?
            LIMIT 1
            """,
            (
                record.project_id,
                parent_request["control_node_id"],
                parent_request["control_episode_id"],
                parent_request["control_invocation"],
                parent["attempt"],
            ),
        ).fetchone()
        if newer_attempt is not None:
            raise ValueError("Only the latest unresolved loop task can be recovered.")

    @staticmethod
    def _validate_current_experiment_graph_repair(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        control_node_id: str,
        episode_id: str,
        invocation: int,
        operation_id: str,
    ) -> None:
        """Keep patch-only repair on the newest episode, invocation, and attempt."""

        newest_root = connection.execute(
            """
            SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
            FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if newest_root is None or newest_root["episode_id"] != episode_id:
            raise ValueError("Only the newest Experiment episode can repair its graph update.")
        stopped = connection.execute(
            "SELECT stop_requested_at FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if stopped is not None and stopped["stop_requested_at"] is not None:
            raise ValueError("A stopped Experiment episode cannot repair an old graph update.")
        latest = connection.execute(
            """
            SELECT operation_id,
                   json_extract(request_json, '$.control_invocation') AS invocation
            FROM graph_runs
            WHERE project_id = ?
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            ORDER BY CAST(json_extract(request_json, '$.control_invocation') AS INTEGER) DESC,
                     attempt DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, episode_id),
        ).fetchone()
        if (
            latest is None
            or latest["invocation"] != invocation
            or latest["operation_id"] != operation_id
        ):
            raise ValueError(
                "Only the newest Experiment invocation and task attempt can repair its graph "
                "update."
            )

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

    def create_watchers(self, records: list[StoredWatcherRecord]) -> list[StoredWatcherRecord]:
        """Insert one validated watch list atomically."""

        records = [self._prepare_watcher_for_insert(record) for record in records]
        self._validate_watch_list(records)
        watcher_ids = [record.watcher_id for record in records]
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                self._insert_watcher(connection, record)
        stored: list[StoredWatcherRecord] = []
        for watcher_id in watcher_ids:
            record = self.watcher(watcher_id)
            assert record is not None
            stored.append(record)
        return stored

    def persist_experiment_watchers_idempotently(
        self,
        records: list[StoredWatcherRecord],
        *,
        stops: list[WatcherStopRequest] | None = None,
        binding: WatcherBinding | None = None,
        expected_watcher_snapshot_token: str | None = None,
    ) -> list[StoredWatcherRecord]:
        """Persist one loop handoff atomically with the episode's graceful stop.

        Deterministic watcher ids make Retry and crash recovery safe. The same
        ``BEGIN IMMEDIATE`` boundary used by Stop loop ensures either the handoff
        lands first and Stop terminalizes it, or the handoff sees stop intent and
        is born stopped. No pollable row can be created after a persisted stop.
        """

        stop_requests = list(stops or [])
        if not records and not stop_requests:
            return []
        records = [self._prepare_watcher_for_insert(record) for record in records]
        if records:
            self._validate_watch_list(records)
        if binding is None:
            raise ValueError("an Experiment handoff requires its bound watcher context")
        continuation = records[0].continuation if records else binding.continuation
        if continuation.patch_kind != "experiment_loop":
            raise ValueError("idempotent Experiment persistence requires loop watchers")
        episode_id = continuation.control_episode_id
        assert episode_id is not None
        if (
            binding is not None
            and records
            and any(
                (
                    record.project_id != binding.project_id
                    or record.origin_operation_id != binding.origin_operation_id
                    or record.origin_task_kind != binding.origin_task_kind
                    or record.chat_id != binding.chat_id
                    or record.node_id != binding.node_id
                    or record.execution_host != binding.execution_host
                    or record.continuation != binding.continuation
                )
                for record in records
            )
        ):
            raise ValueError("Experiment watcher handoff changed its bound continuation context.")
        stop_ids = [item.stop_watcher_id for item in stop_requests]
        if len(stop_ids) != len(set(stop_ids)):
            raise ValueError("Experiment watcher stop ids must be unique")
        watcher_ids = [record.watcher_id for record in records]
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resource = self._admit_experiment_watcher_maintenance(connection, binding)
            if resource is not None:
                if expected_watcher_snapshot_token is None:
                    raise ValueError(
                        "Experiment watcher maintenance requires its staged watcher snapshot."
                    )
                if expected_watcher_snapshot_token != resource.watcher_snapshot_token:
                    raise WatcherClaimConflict(
                        "Experiment watcher state changed after it was staged; inspect the "
                        "current resource before maintaining it."
                    )
            episode = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if episode is not None and (
                episode["project_id"] != (records[0].project_id if records else binding.project_id)
                or episode["control_node_id"] != continuation.control_node_id
            ):
                raise ValueError("This watcher handoff belongs to a different Experiment episode.")
            if stop_requests:
                assert binding is not None
                self._validate_and_apply_agent_watcher_stops(
                    connection,
                    binding,
                    stop_requests,
                    episode,
                )
            stopped = episode is not None and episode["stop_requested_at"] is not None
            existing_rows = []
            if watcher_ids:
                placeholders = ",".join("?" for _ in watcher_ids)
                existing_rows = connection.execute(
                    f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
                    watcher_ids,
                ).fetchall()
            existing_by_id = {
                str(row["watcher_id"]): self._watcher_record(row) for row in existing_rows
            }
            for desired in records:
                existing = existing_by_id.get(desired.watcher_id)
                if existing is not None:
                    self._validate_idempotent_watcher(existing, desired)
                    if stopped and (existing.status != "stopped" or not existing.notified):
                        self._stop_watcher_for_loop(connection, desired.watcher_id)
                    continue
                persisted = (
                    desired.model_copy(
                        update={
                            "status": "stopped",
                            "notified": True,
                            "next_check_at": None,
                            "stopped_by": "loop",
                            "stopped_at": self.now(),
                        }
                    )
                    if stopped
                    else desired
                )
                self._insert_watcher(connection, persisted)
            stored_rows = []
            if watcher_ids:
                placeholders = ",".join("?" for _ in watcher_ids)
                stored_rows = connection.execute(
                    f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
                    watcher_ids,
                ).fetchall()
            stored_by_id = {
                str(row["watcher_id"]): self._watcher_record(row) for row in stored_rows
            }
        return [stored_by_id[watcher_id] for watcher_id in watcher_ids]

    def validate_experiment_agent_watcher_stops(
        self,
        binding: WatcherBinding,
        stops: list[WatcherStopRequest],
    ) -> None:
        """Fail a malformed stop handoff before its Patch can be accepted."""

        if not stops:
            return
        episode_id = binding.continuation.control_episode_id
        with self.connection() as connection:
            self._admit_experiment_watcher_maintenance(connection, binding)
            episode = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            self._validate_and_apply_agent_watcher_stops(
                connection,
                binding,
                stops,
                episode,
                apply=False,
            )

    def experiment_watcher_resources(
        self,
        project_id: str,
        *,
        control_node_ids: set[str] | None = None,
    ) -> list[ExperimentWatcherResourceRecord]:
        """Return live Experiment resources visible within one already-resolved scope."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT json_extract(request_json, '$.control_node_id') AS control_node_id
                FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                """,
                (project_id,),
            ).fetchall()
            resources: list[ExperimentWatcherResourceRecord] = []
            for row in rows:
                control_node_id = row["control_node_id"]
                if not isinstance(control_node_id, str) or not control_node_id:
                    continue
                if control_node_ids is not None and control_node_id not in control_node_ids:
                    continue
                try:
                    resource = self._current_experiment_watcher_resource(
                        connection,
                        project_id,
                        control_node_id,
                    )
                except ValueError:
                    continue
                resources.append(resource)
        return sorted(resources, key=lambda item: item.control_node_id)

    def admit_experiment_watcher_maintenance(
        self,
        binding: WatcherBinding,
    ) -> ExperimentWatcherResourceRecord | None:
        """Authorize one node-attached watcher handoff from its durable Work task.

        A loop turn returns ``None`` before its first episode binding exists. A
        conversation maintenance turn always returns the current resource and
        fails closed when durable node, episode, or session identity is absent.
        """

        with self.connection() as connection:
            return self._admit_experiment_watcher_maintenance(connection, binding)

    def _admit_experiment_watcher_maintenance(
        self,
        connection: sqlite3.Connection,
        binding: WatcherBinding,
    ) -> ExperimentWatcherResourceRecord | None:
        task_row = connection.execute(
            "SELECT project_id, kind, request_json FROM graph_runs WHERE operation_id = ?",
            (binding.origin_operation_id,),
        ).fetchone()
        if task_row is None:
            raise ValueError("Experiment watcher maintenance permission denied: actor is missing.")
        request = json.loads(task_row["request_json"])
        if task_row["project_id"] != binding.project_id:
            raise ValueError(
                "Experiment watcher maintenance permission denied: project scope does not match."
            )
        if request.get("mode") != "work" or task_row["kind"] not in {
            "node_chat",
            "project_chat",
        }:
            raise ValueError(
                "Experiment watcher maintenance permission denied: Work capability is required."
            )
        if (
            request.get("chat_id") != binding.chat_id
            or task_row["kind"] != binding.origin_task_kind
        ):
            raise ValueError(
                "Experiment watcher maintenance permission denied: actor provenance does not match."
            )

        continuation = binding.continuation
        control_node_id = continuation.control_node_id
        episode_id = continuation.control_episode_id
        if continuation.patch_kind != "experiment_loop" or not control_node_id or not episode_id:
            raise ValueError(
                "Experiment watcher maintenance requires an explicit node and episode resource."
            )
        if binding.node_id != control_node_id:
            raise ValueError(
                "Experiment watcher maintenance permission denied: target node does not match."
            )

        actor_patch_kind = request.get("patch_kind")
        if actor_patch_kind == "experiment_loop":
            if (
                request.get("control_node_id") != control_node_id
                or request.get("control_episode_id") != episode_id
            ):
                raise ValueError(
                    "Experiment watcher maintenance permission denied: loop binding does not match."
                )
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if episode_row is not None and (
                episode_row["project_id"] != binding.project_id
                or episode_row["control_node_id"] != control_node_id
            ):
                raise ValueError("Experiment watcher maintenance targets a different episode.")
            return None

        if actor_patch_kind != "work":
            raise ValueError(
                "Experiment watcher maintenance permission denied: captured Patch policy is invalid."
            )
        if task_row["kind"] == "node_chat" and request.get("node_id") != control_node_id:
            raise ValueError(
                "Experiment watcher maintenance permission denied: node scope does not include "
                f"{control_node_id}."
            )
        resource = self._current_experiment_watcher_resource(
            connection,
            binding.project_id,
            control_node_id,
            expected_episode_id=episode_id,
        )
        if binding.execution_host != resource.execution_host:
            raise ValueError("Experiment watcher maintenance must use the episode execution host.")
        if continuation != resource.continuation:
            raise ValueError(
                "Experiment watcher maintenance no longer matches the live episode policy."
            )
        return resource

    def _current_experiment_watcher_resource(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        *,
        expected_episode_id: str | None = None,
    ) -> ExperimentWatcherResourceRecord:
        root_row = connection.execute(
            """
            SELECT kind, request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if root_row is None:
            raise ValueError("Experiment watcher maintenance requires a current live episode.")
        root_request = json.loads(root_row["request_json"])
        episode_id = root_request.get("control_episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the current episode identity."
            )
        if expected_episode_id is not None and expected_episode_id != episode_id:
            raise ValueError("Experiment watcher maintenance targets a stale episode.")
        episode_row = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode_row is None:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode session binding."
            )
        episode = self._experiment_episode_record(episode_row)
        if (
            episode.project_id != project_id
            or episode.control_node_id != control_node_id
            or not episode.session_bound
        ):
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode session binding."
            )
        if episode.stop_requested_at is not None or episode.stop_settled_at is not None:
            raise ValueError("Experiment watcher maintenance requires a live, unstopped episode.")
        exited = connection.execute(
            """
            SELECT 1 FROM graph_run_receipts AS receipt
            JOIN graph_runs AS run ON run.operation_id = receipt.operation_id
            WHERE run.project_id = ?
              AND json_extract(run.request_json, '$.control_episode_id') = ?
              AND receipt.category = 'experiment_loop_exit'
            LIMIT 1
            """,
            (project_id, episode_id),
        ).fetchone()
        if exited is not None:
            raise ValueError("Experiment watcher maintenance requires a live, unexited episode.")
        if not episode.last_turn_operation_id:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode's latest turn."
            )
        turn_row = connection.execute(
            "SELECT request_json FROM graph_runs WHERE operation_id = ? AND project_id = ?",
            (episode.last_turn_operation_id, project_id),
        ).fetchone()
        if turn_row is None:
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode's latest turn."
            )
        turn_request = json.loads(turn_row["request_json"])
        continuation_data = {
            key: turn_request[key]
            for key in WatcherContinuation.model_fields
            if key in turn_request
        }
        for nullable_list in ("workflow_ids", "skill_ids", "resolved_skill_packages"):
            if continuation_data.get(nullable_list) is None:
                continuation_data[nullable_list] = []
        continuation = WatcherContinuation.model_validate(continuation_data)
        if (
            continuation.patch_kind != "experiment_loop"
            or continuation.control_node_id != control_node_id
            or continuation.control_episode_id != episode_id
        ):
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode continuation policy."
            )
        wake_task_kind = root_row["kind"]
        if wake_task_kind != "node_chat":
            raise ValueError("Experiment watcher maintenance has an invalid wake task binding.")
        if not episode.chat_id:
            # The wake target is derived, never guessed: without the episode's own
            # conversation there is nothing to wake, so fail closed with a diagnostic
            # rather than an AssertionError that -O would strip.
            raise ValueError(
                "Experiment watcher maintenance cannot prove the episode's wake conversation."
            )
        return ExperimentWatcherResourceRecord(
            project_id=project_id,
            control_node_id=control_node_id,
            episode_id=episode_id,
            execution_host=episode.execution_host,
            wake_task_kind=wake_task_kind,
            wake_chat_id=episode.chat_id,
            continuation=continuation,
            watcher_snapshot_token=self._experiment_watcher_snapshot_token(
                connection,
                project_id,
                control_node_id,
            ),
        )

    @staticmethod
    def _experiment_watcher_snapshot_token(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
    ) -> str:
        """Fingerprint the node's observer membership, and nothing else.

        This defends exactly one gap. Every retirement is already a
        compare-and-swap inside the arming transaction, so a delivery claim, a
        **Stop loop**, or an already-resolved stop is caught per item without a
        fingerprint. Arming is not: new observers are plain inserts, so two
        maintenance turns could each retire the old set and each arm
        replacements, leaving the Experiment double-observed.

        Membership answers that and stays blind to everything RCP merely
        observed. Status and consecutive-error counts deliberately do not appear:
        a degraded observer is re-checked on the S84 backoff, so fingerprinting
        observation would reject the maintenance turn that exists to repair that
        very observer. Retired rows keep their id, so the set only grows and a
        concurrent retirement does not collide with an unrelated repair.
        """

        rows = connection.execute(
            """
            SELECT watcher_id FROM watchers
            WHERE project_id = ? AND node_id = ?
              AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
            ORDER BY watcher_id
            """,
            (project_id, control_node_id),
        ).fetchall()
        snapshot = json.dumps(
            [str(row["watcher_id"]) for row in rows],
            separators=(",", ":"),
        )
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def _validate_and_apply_agent_watcher_stops(
        self,
        connection: sqlite3.Connection,
        binding: WatcherBinding,
        stops: list[WatcherStopRequest],
        episode_row: sqlite3.Row | None,
        *,
        apply: bool = True,
    ) -> None:
        """Retire only staged compatible observers under the arming transaction."""

        continuation = binding.continuation
        episode_id = continuation.control_episode_id
        control_node_id = continuation.control_node_id
        if episode_row is None or not episode_id or not control_node_id:
            raise ValueError("An agent watcher stop requires the current Experiment episode.")
        episode = self._experiment_episode_record(episode_row)
        root_request = self._experiment_episode_root_request(
            connection,
            binding.project_id,
            control_node_id,
            episode_id,
        )
        if root_request is None:
            raise ValueError("An agent watcher stop requires the bound Experiment root task.")
        ids = [item.stop_watcher_id for item in stops]
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id = {str(row["watcher_id"]): self._watcher_record(row) for row in rows}
        for stop in stops:
            record = by_id.get(stop.stop_watcher_id)
            if record is None:
                raise ValueError(
                    f"Watcher stop names an unknown staged watcher: {stop.stop_watcher_id}"
                )
            if isinstance(record, GraphWatcherRecord):
                raise ValueError(
                    "Experiment agent watcher stops may retire only external observers: "
                    f"{stop.stop_watcher_id}"
                )
            if record.status == "stopped":
                if (
                    record.stopped_by == "agent"
                    and record.stop_operation_id == binding.origin_operation_id
                    and record.stop_reason == stop.reason
                ):
                    continue
                # Stop loop retires this episode's watchers while its authorized turn is
                # still running. That turn's retirement is already satisfied, so it
                # finishes normally instead of correcting a race it cannot win.
                if episode.stop_requested_at is not None:
                    continue
                raise ValueError(f"Watcher stop was already resolved: {stop.stop_watcher_id}")
            if record.notified or record.notification_operation_id is not None:
                raise WatcherClaimConflict("A watcher update was already claimed for delivery.")
            if (
                record.project_id != binding.project_id
                or record.node_id != control_node_id
                or not self._experiment_watcher_matches_current(record, root_request, episode)
            ):
                raise ValueError(
                    f"Watcher stop is outside the bound Experiment episode: {stop.stop_watcher_id}"
                )
            if record.status not in {"active", "degraded", "completed"}:
                raise ValueError(f"Watcher cannot be retired: {stop.stop_watcher_id}")

        if not apply:
            return
        timestamp = self.now()
        for stop in stops:
            record = by_id[stop.stop_watcher_id]
            if record.status == "stopped":
                continue
            cursor = connection.execute(
                """
                UPDATE watchers
                SET status = 'stopped', notified = 1, next_check_at = NULL,
                    stopped_by = 'agent', stop_reason = ?, stopped_at = ?, stop_operation_id = ?
                WHERE watcher_id = ? AND status IN ('active', 'degraded', 'completed')
                  AND notified = 0 AND notification_operation_id IS NULL
                """,
                (stop.reason, timestamp, binding.origin_operation_id, stop.stop_watcher_id),
            )
            if cursor.rowcount != 1:
                raise WatcherClaimConflict("A watcher update changed during its retirement claim.")

    def _stop_watcher_for_loop(self, connection: sqlite3.Connection, watcher_id: str) -> None:
        timestamp = self.now()
        connection.execute(
            """
            UPDATE watchers
            SET status = 'stopped', notified = 1, next_check_at = NULL,
                stopped_by = COALESCE(stopped_by, 'loop'),
                stopped_at = COALESCE(stopped_at, ?)
            WHERE watcher_id = ?
            """,
            (timestamp, watcher_id),
        )

    @staticmethod
    def _prepare_watcher_for_insert(record: StoredWatcherRecord) -> StoredWatcherRecord:
        continuation = record.continuation
        if continuation.patch_kind == "experiment_loop":
            episode_id = record.experiment_episode_id or continuation.control_episode_id
            if episode_id != record.experiment_episode_id:
                record = record.model_copy(update={"experiment_episode_id": episode_id})
        if isinstance(record, GraphWatcherRecord):
            return record
        if record.status not in {"active", "degraded"} or record.next_check_at is not None:
            return record
        error_count = record.consecutive_error_count
        if record.status == "degraded" and error_count == 0:
            error_count = 1
        checked_at = record.last_checked_at or record.created_at
        return record.model_copy(
            update={
                "consecutive_error_count": error_count,
                "next_check_at": watcher_next_check_at(
                    record.watcher_id,
                    checked_at,
                    error_count,
                ),
            }
        )

    @staticmethod
    def _validate_watch_list(records: list[StoredWatcherRecord]) -> None:
        if not records:
            raise ValueError("a watch list must contain at least one watcher")
        watcher_ids = [record.watcher_id for record in records]
        if len(watcher_ids) != len(set(watcher_ids)):
            raise ValueError("a watch list cannot repeat a watcher id")
        bindings = {
            (
                record.project_id,
                record.origin_operation_id,
                record.origin_task_kind,
                record.chat_id,
                record.node_id,
                record.experiment_episode_id,
                record.execution_host,
                record.continuation.model_dump_json(),
            )
            for record in records
        }
        if len(bindings) != 1:
            raise ValueError("one watch list must share one RCP-bound continuation context")
        continuation = records[0].continuation
        if any(
            isinstance(record, GraphWatcherRecord) and record.status == "degraded"
            for record in records
        ):
            raise ValueError("a graph condition cannot have a degraded shell-check state")
        if any(
            isinstance(record, GraphWatcherRecord) and record.armed_revision is None
            for record in records
        ):
            raise ValueError("a new graph condition requires its canonical arming revision")
        grouped = [record for record in records if record.group_id is not None]
        if any(isinstance(record, GraphWatcherRecord) for record in grouped):
            raise ValueError("graph conditions cannot join an external watcher group")
        if any((record.group_id is None) != (record.group_label is None) for record in records):
            raise ValueError("watcher group identity and label must be stored together")
        if grouped and continuation.patch_kind != "experiment_loop":
            raise ValueError("only Experiment-loop watchers may join a watcher group")
        if grouped:
            group_counts: dict[str, int] = {}
            for record in grouped:
                assert record.group_id is not None
                group_counts[record.group_id] = group_counts.get(record.group_id, 0) + 1
            if any(count < 2 for count in group_counts.values()):
                raise ValueError("an Experiment watcher group requires at least two observers")
        if continuation.patch_kind != "experiment_loop":
            if any(record.experiment_episode_id is not None for record in records):
                raise ValueError("only Experiment watchers may bind to an Experiment episode")
            return
        if not all(
            (
                continuation.control_node_id,
                continuation.control_episode_id,
                continuation.control_invocation,
                continuation.control_invocation_ceiling,
            )
        ):
            raise ValueError("an experiment-loop watcher must preserve its control binding")
        assert continuation.control_invocation is not None
        assert continuation.control_invocation_ceiling is not None
        if continuation.control_invocation > continuation.control_invocation_ceiling:
            raise ValueError("an experiment-loop watcher invocation exceeds its pinned ceiling")
        if any(
            record.experiment_episode_id != continuation.control_episode_id for record in records
        ):
            raise ValueError("an Experiment watcher must bind explicitly to its control episode")

    @staticmethod
    def _validate_idempotent_watcher(
        existing: StoredWatcherRecord,
        desired: StoredWatcherRecord,
    ) -> None:
        if type(existing) is not type(desired):
            raise ValueError("Experiment-loop watcher identity conflicts with stored state.")
        immutable_fields = [
            "project_id",
            "origin_operation_id",
            "origin_task_kind",
            "chat_id",
            "node_id",
            "experiment_episode_id",
            "execution_host",
            "continuation",
            "group_id",
            "group_label",
        ]
        if isinstance(existing, WatcherRecord):
            immutable_fields.extend(("check_command", "log_path", "cwd"))
        else:
            immutable_fields.append("condition")
        if any(getattr(existing, field) != getattr(desired, field) for field in immutable_fields):
            raise ValueError("Experiment-loop watcher identity conflicts with stored state.")

    @staticmethod
    def _insert_watcher(connection: sqlite3.Connection, record: StoredWatcherRecord) -> None:
        stopped_campaign = connection.execute(
            """
            SELECT COALESCE(
                       campaign.stop_requested_at,
                       campaign.ended_at,
                       campaign.updated_at
                   ) AS stop_requested_at
            FROM graph_runs AS run
            JOIN campaigns AS campaign ON campaign.campaign_id = run.campaign_id
            WHERE run.operation_id = ?
              AND (
                  campaign.stop_requested_at IS NOT NULL
                  OR campaign.ending IS NOT NULL
              )
            """,
            (record.origin_operation_id,),
        ).fetchone()
        if stopped_campaign is not None and record.status != "stopped":
            record = record.model_copy(
                update={
                    "status": "stopped",
                    "notified": True,
                    "next_check_at": None,
                    "stopped_by": "loop",
                    "stopped_at": stopped_campaign["stop_requested_at"],
                }
            )
        if isinstance(record, GraphWatcherRecord):
            # Legacy watcher tables keep these external-only columns NOT NULL.
            # The separate GraphWatcherRecord never exposes the compatibility
            # placeholders; graph_condition_json selects its stored type.
            check_command = ""
            log_path = ""
            cwd = ""
            graph_condition_json = record.condition.model_dump_json()
            armed_revision = record.armed_revision
        else:
            check_command = record.check_command
            log_path = record.log_path
            cwd = record.cwd
            graph_condition_json = None
            armed_revision = None
        connection.execute(
            """
            INSERT INTO watchers (
                watcher_id, project_id, origin_operation_id, origin_task_kind,
                chat_id, node_id, experiment_episode_id, execution_host,
                check_command, log_path, cwd, graph_condition_json, armed_revision,
                continuation_json, status, created_at, last_checked_at,
                last_exit_code, last_error, completed_at, next_check_at,
                consecutive_error_count, group_id, group_label, notified,
                notification_operation_id, stopped_by, stop_reason, stopped_at,
                stop_operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.watcher_id,
                record.project_id,
                record.origin_operation_id,
                record.origin_task_kind,
                record.chat_id,
                record.node_id,
                record.experiment_episode_id,
                record.execution_host,
                check_command,
                log_path,
                cwd,
                graph_condition_json,
                armed_revision,
                record.continuation.model_dump_json(),
                record.status,
                record.created_at,
                record.last_checked_at,
                record.last_exit_code,
                record.last_error,
                record.completed_at,
                record.next_check_at,
                record.consecutive_error_count,
                record.group_id,
                record.group_label,
                int(record.notified),
                record.notification_operation_id,
                record.stopped_by,
                record.stop_reason,
                record.stopped_at,
                record.stop_operation_id,
            ),
        )

    def watcher(self, watcher_id: str) -> StoredWatcherRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)
            ).fetchone()
        return self._watcher_record(row) if row is not None else None

    def watchers(
        self,
        project_id: str,
        *,
        chat_id: str | None = None,
    ) -> list[StoredWatcherRecord]:
        query = "SELECT * FROM watchers WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if chat_id is not None:
            query += " AND chat_id = ?"
            parameters.append(chat_id)
        query += " ORDER BY created_at DESC, watcher_id"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._watcher_record(row) for row in rows]

    def active_graph_watchers(self, project_id: str) -> list[GraphWatcherRecord]:
        """Return graph conditions awaiting a canonical revision boundary."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ? AND graph_condition_json IS NOT NULL
                  AND status = 'active' AND notified = 0
                ORDER BY created_at, watcher_id
                """,
                (project_id,),
            ).fetchall()
        records = [self._watcher_record(row) for row in rows]
        if any(not isinstance(record, GraphWatcherRecord) for record in records):
            raise RuntimeError("External watcher row appeared in the graph-condition index.")
        return records  # type: ignore[return-value]

    def graph_watcher_project_ids(self) -> list[str]:
        """Return projects needing startup graph evaluation or delivery retry."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT project_id FROM watchers
                WHERE graph_condition_json IS NOT NULL
                  AND status IN ('active', 'completed') AND notified = 0
                ORDER BY project_id
                """
            ).fetchall()
        return [str(row["project_id"]) for row in rows]

    def record_graph_watcher_result(
        self,
        watcher_id: str,
        *,
        result: Literal["active", "completed", "removed"],
        evaluated_at: str | None = None,
    ) -> GraphWatcherRecord:
        """Persist one canonical graph evaluation without entering the shell poller."""

        timestamp = evaluated_at or self.now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)
            ).fetchone()
            if row is None:
                raise KeyError(watcher_id)
            current = self._watcher_record(row)
            if not isinstance(current, GraphWatcherRecord):
                raise ValueError("an external watcher cannot receive a graph evaluation")
            if current.status != "active" or current.notified:
                return current
            if result == "active":
                connection.execute(
                    """
                    UPDATE watchers SET last_checked_at = ?
                    WHERE watcher_id = ? AND graph_condition_json IS NOT NULL
                      AND status = 'active' AND notified = 0
                    """,
                    (timestamp, watcher_id),
                )
            elif result == "completed":
                connection.execute(
                    """
                    UPDATE watchers
                    SET status = 'completed', last_checked_at = ?, completed_at = ?,
                        next_check_at = NULL
                    WHERE watcher_id = ? AND graph_condition_json IS NOT NULL
                      AND status = 'active' AND notified = 0
                    """,
                    (timestamp, timestamp, watcher_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE watchers
                    SET status = 'stopped', notified = 1, last_checked_at = ?,
                        next_check_at = NULL, stopped_by = 'loop',
                        stop_reason = 'Graph condition target was removed.', stopped_at = ?
                    WHERE watcher_id = ? AND graph_condition_json IS NOT NULL
                      AND status = 'active' AND notified = 0
                    """,
                    (timestamp, timestamp, watcher_id),
                )
        stored = self.watcher(watcher_id)
        assert isinstance(stored, GraphWatcherRecord)
        return stored

    def initialize_graph_watcher_baseline(
        self,
        watcher_id: str,
        *,
        armed_revision: int,
        evaluated_at: str | None = None,
    ) -> GraphWatcherRecord:
        """Fail closed while giving one pre-baseline graph row a durable boundary."""

        timestamp = evaluated_at or self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE watchers SET armed_revision = ?, last_checked_at = ?
                WHERE watcher_id = ? AND graph_condition_json IS NOT NULL
                  AND armed_revision IS NULL AND status = 'active' AND notified = 0
                """,
                (armed_revision, timestamp, watcher_id),
            )
        stored = self.watcher(watcher_id)
        assert isinstance(stored, GraphWatcherRecord)
        return stored

    def pollable_watchers(self, *, as_of: str | None = None) -> list[WatcherRecord]:
        """Return only active/degraded observers whose durable due time arrived."""

        now = as_of or self.now()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE status IN ('active', 'degraded')
                  AND notified = 0
                  AND graph_condition_json IS NULL
                  AND (next_check_at IS NULL OR next_check_at <= ?)
                ORDER BY created_at, watcher_id
                """,
                (now,),
            ).fetchall()
            records = [self._watcher_record(row) for row in rows]
            if any(not isinstance(record, WatcherRecord) for record in records):
                raise RuntimeError("Graph conditions cannot enter the external watcher poller.")
            stopping_contexts: dict[
                tuple[str, str],
                tuple[dict[str, object], ExperimentEpisodeRecord] | None,
            ] = {}
            return [
                record
                for record in records
                if not self._watcher_suppressed_by_current_stop(
                    connection,
                    record,
                    stopping_contexts,
                )
            ]

    def stop_watchers(self, project_id: str, watcher_ids: list[str]) -> list[StoredWatcherRecord]:
        """Release watchers the human has given up on.

        A stopped watcher leaves the polling set and can never wake a turn. RCP
        never decides this for itself — a check that cannot answer is reported,
        not interpreted.
        """

        ids = list(dict.fromkeys(watcher_ids))
        if not ids:
            raise ValueError("stopping watchers requires at least one watcher id")
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT watcher_id, project_id, status, notified, notification_operation_id
                FROM watchers
                WHERE watcher_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            if {str(row["watcher_id"]) for row in rows} != set(ids) or {
                str(row["project_id"]) for row in rows
            } != {project_id}:
                missing = next(
                    (
                        watcher_id
                        for watcher_id in ids
                        if watcher_id not in {str(row["watcher_id"]) for row in rows}
                    ),
                    ids[0],
                )
                raise KeyError(missing)
            if any(row["notification_operation_id"] is not None for row in rows):
                raise WatcherClaimConflict("A watcher update was already claimed for delivery.")
            invalid = [
                str(row["watcher_id"])
                for row in rows
                if row["status"] not in {"active", "degraded", "completed", "stopped"}
                or (bool(row["notified"]) and row["status"] != "stopped")
            ]
            if invalid:
                raise ValueError(f"Watchers cannot be stopped: {', '.join(sorted(invalid))}.")
            connection.execute(
                f"""
                UPDATE watchers
                SET status = 'stopped', notified = 1, next_check_at = NULL,
                    stopped_by = COALESCE(stopped_by, 'human'),
                    stopped_at = COALESCE(stopped_at, ?)
                WHERE project_id = ? AND watcher_id IN ({placeholders})
                  AND status IN ('active', 'degraded', 'completed')
                  AND notification_operation_id IS NULL
                """,
                (self.now(), project_id, *ids),
            )
        stopped: list[StoredWatcherRecord] = []
        for watcher_id in ids:
            record = self.watcher(watcher_id)
            assert record is not None
            stopped.append(record)
        return stopped

    def experiment_watcher_ids(self, project_id: str, control_node_id: str) -> list[str]:
        """Live watchers armed by a bounded loop on one experiment."""

        return [
            record.watcher_id
            for record in self.watchers(project_id)
            if (
                (record.status in {"active", "degraded"} and not record.notified)
                or (record.status == "completed" and not record.notified)
            )
            and record.continuation.control_node_id == control_node_id
        ]

    def experiment_handoff_has_live_watcher_after_stops(
        self,
        binding: WatcherBinding,
        stop_watcher_ids: list[str],
    ) -> bool:
        """Whether a stop-only handoff leaves another compatible wake source."""

        continuation = binding.continuation
        episode_id = continuation.control_episode_id
        control_node_id = continuation.control_node_id
        if not episode_id or not control_node_id:
            return False
        stopped = set(stop_watcher_ids)
        with self.connection() as connection:
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            if episode_row is None:
                return False
            episode = self._experiment_episode_record(episode_row)
            root = self._experiment_episode_root_request(
                connection,
                binding.project_id,
                control_node_id,
                episode_id,
            )
            if root is None:
                return False
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ? AND status IN ('active', 'degraded', 'completed')
                  AND notified = 0
                """,
                (binding.project_id,),
            ).fetchall()
        return any(
            record.watcher_id not in stopped
            and self._experiment_watcher_matches_current(record, root, episode)
            for record in (self._watcher_record(row) for row in rows)
        )

    def experiment_episode(self, episode_id: str) -> ExperimentEpisodeRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return self._experiment_episode_record(row) if row is not None else None

    def experiment_episode_recovery_context_problem(self, operation_id: str) -> str | None:
        """Explain why this task lineage cannot retain its episode context on recovery."""

        with self.connection() as connection:
            return self._experiment_episode_recovery_context_problem(connection, operation_id)

    @staticmethod
    def _experiment_episode_recovery_context_problem(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> str | None:
        """Validate the immutable candidate on an Experiment invocation's lineage root."""

        current_id = operation_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                return (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage contains a cycle. Use Stop loop and press Run to start a fresh "
                    "episode."
                )
            seen.add(current_id)
            row = connection.execute(
                "SELECT parent_operation_id FROM graph_runs WHERE operation_id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                return (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage is incomplete. Use Stop loop and press Run to start a fresh episode."
                )
            parent_id = row["parent_operation_id"]
            if parent_id is None:
                break
            current_id = str(parent_id)

        contract = connection.execute(
            """
            SELECT content FROM graph_run_contracts
            WHERE operation_id = ? AND role = ?
            """,
            (current_id, _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE),
        ).fetchone()
        if contract is None:
            return _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC
        try:
            candidate = json.loads(contract["content"])
        except (json.JSONDecodeError, TypeError):
            candidate = None
        if not isinstance(candidate, dict):
            return (
                "This Experiment-loop turn cannot be resumed or retried because its retained "
                "episode context candidate is invalid. Use Stop loop and press Run to start a "
                "fresh episode."
            )
        return None

    def previous_experiment_episode(
        self,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Return the episode immediately before this one for the same Experiment.

        Ordering comes from the root invocations, not the episode table, because
        an episode only gets a row once it binds a session or receives a stop.
        """

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
                FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (project_id, control_node_id),
            ).fetchall()
        ordered: list[str] = []
        for row in rows:
            value = row["episode_id"]
            if isinstance(value, str) and value not in ordered:
                ordered.append(value)
        if episode_id not in ordered:
            return None
        position = ordered.index(episode_id) + 1
        if position >= len(ordered):
            return None
        return self.experiment_episode(ordered[position])

    def commit_experiment_episode_turn(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        provider: str,
        execution_machine: str,
        execution_host: str,
        native_session_id: str,
        stage_host: str | None,
        stage_root: str,
        chat_id: str,
        operation_id: str,
        invocation: int,
        graph_result: str,
        watcher_ids: list[str],
        context_baseline: dict[str, object],
        replace_binding: bool = False,
        replacement_provenance: dict[str, object] | None = None,
    ) -> ExperimentEpisodeRecord:
        """Bind this episode to the session a later automatic wake resumes.

        Only a mechanically successful joint handoff commits, so a wake never
        tries to continue a session that never established one, and the context
        baseline can only move forward with an accepted operational turn. A
        graph-only rejection is retained as that turn's truthful result.
        """

        if not native_session_id or not stage_root:
            raise ValueError("An episode binding requires a native session and its exact stage.")
        if replace_binding and replacement_provenance is None:
            raise ValueError("An episode binding replacement requires its recovery provenance.")
        replacement_payload_json = (
            self._bounded_receipt_payload(replacement_provenance)
            if replacement_provenance is not None
            else None
        )
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            existing = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if (
                existing is None
                or existing["project_id"] != project_id
                or existing["control_node_id"] != control_node_id
            ):
                raise ValueError("This episode id belongs to a different Experiment.")
            if existing["native_session_id"] is not None:
                fixed = {
                    "execution_machine": execution_machine,
                    "execution_host": execution_host,
                    "chat_id": chat_id,
                }
                fixed_conflicts = sorted(
                    field for field, value in fixed.items() if (existing[field] or "") != value
                )
                if fixed_conflicts:
                    raise ValueError(
                        "An Experiment episode recovery cannot change its pinned identity: "
                        + ", ".join(fixed_conflicts)
                    )
                binding = {
                    "provider": provider,
                    "native_session_id": native_session_id,
                    "stage_host": stage_host or "",
                    "stage_root": stage_root,
                }
                binding_conflicts = sorted(
                    field for field, value in binding.items() if (existing[field] or "") != value
                )
                if binding_conflicts and not replace_binding:
                    raise ValueError(
                        "An Experiment episode cannot change its native-session binding: "
                        + ", ".join(binding_conflicts)
                    )
            connection.execute(
                """
                UPDATE experiment_episodes
                SET provider = ?, execution_machine = ?, execution_host = ?,
                    native_session_id = ?, stage_host = ?, stage_root = ?, chat_id = ?,
                    last_turn_operation_id = ?, last_turn_invocation = ?,
                    last_graph_result = ?, last_watcher_ids_json = ?,
                    context_baseline_json = ?, session_diagnostic = NULL, updated_at = ?
                WHERE episode_id = ?
                """,
                (
                    provider,
                    execution_machine,
                    execution_host,
                    native_session_id,
                    stage_host,
                    stage_root,
                    chat_id,
                    operation_id,
                    invocation,
                    graph_result,
                    json.dumps(list(watcher_ids), separators=(",", ":")),
                    json.dumps(context_baseline, sort_keys=True, separators=(",", ":")),
                    now,
                    episode_id,
                ),
            )
            if replace_binding and replacement_payload_json is not None:
                self._insert_agent_task_receipt(
                    connection,
                    operation_id,
                    "experiment_episode_binding_replaced",
                    replacement_payload_json,
                    tier="summary",
                    created_at=now,
                )
        stored = self.experiment_episode(episode_id)
        assert stored is not None
        return stored

    def record_experiment_episode_diagnostic(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        diagnostic: str | None,
    ) -> None:
        """Persist why an automatic wake could not use this episode's session.

        The row is created on demand: the episode whose very first turn never
        bound a session is exactly the one that most needs a diagnostic, and it
        has nothing else to write a row for it.
        """

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            connection.execute(
                "UPDATE experiment_episodes SET session_diagnostic = ?, updated_at = ? "
                "WHERE episode_id = ? AND project_id = ? AND control_node_id = ?",
                (diagnostic, now, episode_id, project_id, control_node_id),
            )

    def request_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Persist a durable stop for the newest episode before any new claim can win.

        The intent is written under the same write lock a watcher claim takes, so
        a claim that committed first becomes the current turn and anything later
        finds the loop already stopped.
        """

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode_id = self._newest_experiment_episode_id(connection, project_id, control_node_id)
            if episode_id is None:
                return None
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            connection.execute(
                """
                UPDATE experiment_episodes
                SET stop_requested_at = COALESCE(stop_requested_at, ?), updated_at = ?
                WHERE episode_id = ?
                """,
                (now, now, episode_id),
            )
            self._settle_experiment_loop_stop(connection, project_id, control_node_id, episode_id)
        return self.experiment_episode(episode_id)

    def settle_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Reconcile a persisted stop once its authorized turn is no longer live."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode_id = self._newest_experiment_episode_id(connection, project_id, control_node_id)
            if episode_id is None:
                return None
            self._settle_experiment_loop_stop(connection, project_id, control_node_id, episode_id)
        return self.experiment_episode(episode_id)

    def _settle_experiment_loop_stop(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> bool:
        """Terminalize this episode's observers once its authorized turn is resolved.

        "Resolved" is the same predicate the runtime calls `task_active`, not just
        "not running": a turn that paused or failed is still the authorized turn
        the human may Resume, so the loop keeps reading Stopping until it reaches
        a terminal state. A claimed watcher keeps its notification provenance,
        but becomes stopped once the task it woke has finished successfully.
        """

        requested = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if requested is None or requested["stop_requested_at"] is None:
            return False
        # A superseded attempt does not count: only the newest attempt of each
        # invocation is the turn the human can still act on, which is exactly what
        # `experiment_loop_runtime` reports as `task_active`.
        unresolved = connection.execute(
            """
            SELECT task.operation_id, task.status FROM graph_runs AS task
            WHERE task.project_id = ?
              AND json_extract(task.request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(task.request_json, '$.control_node_id') = ?
              AND json_extract(task.request_json, '$.control_episode_id') = ?
              AND task.status IN ('queued', 'running', 'pausing', 'paused', 'failed', 'interrupted')
              AND NOT EXISTS (
                  SELECT 1 FROM graph_runs AS child
                  WHERE child.parent_operation_id = task.operation_id
              )
            """,
            (project_id, control_node_id, episode_id),
        ).fetchall()
        if unresolved:
            diagnostic = requested["session_diagnostic"]
            if not diagnostic:
                diagnostic = next(
                    (
                        problem
                        for row in unresolved
                        if (
                            problem := self._experiment_episode_recovery_context_problem(
                                connection,
                                str(row["operation_id"]),
                            )
                        )
                    ),
                    None,
                )
                if diagnostic:
                    now = self.now()
                    connection.execute(
                        "UPDATE experiment_episodes SET session_diagnostic = ?, updated_at = ? "
                        "WHERE episode_id = ?",
                        (diagnostic, now, episode_id),
                    )
            abandonable = bool(diagnostic) and all(
                row["status"] in {"paused", "failed", "interrupted"} for row in unresolved
            )
            if not abandonable:
                return False
            now = self.now()
            for row in unresolved:
                already_abandoned = connection.execute(
                    """
                    SELECT 1 FROM graph_run_receipts
                    WHERE operation_id = ? AND category = 'experiment_recovery_abandoned'
                    LIMIT 1
                    """,
                    (row["operation_id"],),
                ).fetchone()
                if already_abandoned is not None:
                    continue
                detail = (
                    "Stop loop abandoned recovery of this terminal task because its saved "
                    "episode session cannot be continued. The task and all history remain "
                    "inspectable."
                )
                self._insert_agent_task_receipt(
                    connection,
                    str(row["operation_id"]),
                    "experiment_recovery_abandoned",
                    self._bounded_receipt_payload({"episode_id": episode_id, "reason": diagnostic}),
                    tier="summary",
                    created_at=now,
                )
                self._insert_agent_task_event(
                    connection,
                    str(row["operation_id"]),
                    detail,
                    level="warning",
                    created_at=now,
                )
        root_request = self._experiment_episode_root_request(
            connection,
            project_id,
            control_node_id,
            episode_id,
        )
        episode = self._experiment_episode_record(requested)
        watcher_rows = connection.execute(
            """
            SELECT * FROM watchers
            WHERE project_id = ?
              AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(continuation_json, '$.control_node_id') = ?
              AND status IN ('active', 'degraded', 'completed')
            """,
            (project_id, control_node_id),
        ).fetchall()
        watcher_ids = {
            record.watcher_id
            for record in (self._watcher_record(row) for row in watcher_rows)
            if root_request is not None
            and self._experiment_watcher_matches_current(record, root_request, episode)
        }
        claimed_rows = connection.execute(
            """
            SELECT watcher_id FROM watchers
            WHERE project_id = ?
              AND notification_operation_id IN (
                  SELECT operation_id FROM graph_runs
                  WHERE project_id = ?
                    AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                    AND json_extract(request_json, '$.control_node_id') = ?
                    AND json_extract(request_json, '$.control_episode_id') = ?
              )
            """,
            (project_id, project_id, control_node_id, episode_id),
        ).fetchall()
        watcher_ids.update(str(row["watcher_id"]) for row in claimed_rows)
        if watcher_ids:
            placeholders = ",".join("?" for _ in watcher_ids)
            connection.execute(
                f"UPDATE watchers SET status = 'stopped', notified = 1, next_check_at = NULL, "
                "stopped_by = COALESCE(stopped_by, 'loop'), "
                "stopped_at = COALESCE(stopped_at, ?) "
                f"WHERE watcher_id IN ({placeholders})",
                (self.now(), *sorted(watcher_ids)),
            )
        if requested["stop_settled_at"] is None:
            now = self.now()
            connection.execute(
                "UPDATE experiment_episodes SET stop_settled_at = ?, updated_at = ? "
                "WHERE episode_id = ?",
                (now, now, episode_id),
            )
        return True

    def settle_ready_experiment_loop_stops(self) -> int:
        """Reconcile every durable stop that no longer has a recoverable turn."""

        settled = 0
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT episode_id, project_id, control_node_id
                FROM experiment_episodes
                WHERE stop_requested_at IS NOT NULL AND stop_settled_at IS NULL
                ORDER BY created_at, episode_id
                """
            ).fetchall()
            for row in rows:
                if self._settle_experiment_loop_stop(
                    connection,
                    str(row["project_id"]),
                    str(row["control_node_id"]),
                    str(row["episode_id"]),
                ):
                    settled += 1
        return settled

    @staticmethod
    def _newest_experiment_episode_id(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
            FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if row is None or not isinstance(row["episode_id"], str):
            return None
        return row["episode_id"]

    def experiment_loop_runtime(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentLoopRuntime:
        """Derive the newest episode from root invocations and its watcher ledger."""

        return self.experiment_loop_runtimes(project_id, [control_node_id])[control_node_id]

    def experiment_loop_runtimes(
        self,
        project_id: str,
        control_node_ids: Iterable[str],
    ) -> dict[str, ExperimentLoopRuntime]:
        """Derive several Experiment runtimes from one project-scoped projection."""

        requested = tuple(dict.fromkeys(control_node_ids))
        if not requested:
            return {}
        projected = self._project_experiment_loop_runtimes(project_id, set(requested))
        return {
            control_node_id: projected.get(control_node_id, ExperimentLoopRuntime())
            for control_node_id in requested
        }

    def _project_experiment_loop_runtimes(
        self,
        project_id: str,
        requested: set[str] | None,
    ) -> dict[str, ExperimentLoopRuntime]:
        """Load loop ledgers in four bounded reads and group them in memory."""

        with self.connection() as connection:
            task_rows = connection.execute(
                """
                SELECT operation_id, parent_operation_id, status, attempt, request_json,
                       created_at, phase, status_message, last_activity_at,
                       rowid AS storage_rowid
                FROM graph_runs
                WHERE project_id = ?
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                """,
                (project_id,),
            ).fetchall()
            receipt_rows = connection.execute(
                """
                SELECT receipt.operation_id, receipt.category
                FROM graph_run_receipts AS receipt
                JOIN graph_runs AS task ON task.operation_id = receipt.operation_id
                WHERE task.project_id = ?
                  AND json_extract(task.request_json, '$.patch_kind') = 'experiment_loop'
                  AND receipt.category IN (
                      'experiment_loop_exit', 'experiment_recovery_abandoned'
                  )
                """,
                (project_id,),
            ).fetchall()
            watcher_rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ?
                  AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
                  AND notified = 0
                  AND status IN ('active', 'degraded', 'completed')
                """,
                (project_id,),
            ).fetchall()
            episode_rows = connection.execute(
                """
                SELECT * FROM experiment_episodes
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchall()

        tasks_by_control: dict[
            str,
            list[tuple[sqlite3.Row, dict[str, object]]],
        ] = {}
        for row in task_rows:
            request = json.loads(row["request_json"])
            control_node_id = request.get("control_node_id")
            if not isinstance(control_node_id, str) or not control_node_id:
                continue
            if requested is not None and control_node_id not in requested:
                continue
            tasks_by_control.setdefault(control_node_id, []).append((row, request))

        watchers_by_control: dict[str, list[StoredWatcherRecord]] = {}
        for row in watcher_rows:
            record = self._watcher_record(row)
            control_node_id = record.continuation.control_node_id
            if not control_node_id:
                continue
            if requested is not None and control_node_id not in requested:
                continue
            watchers_by_control.setdefault(control_node_id, []).append(record)

        receipt_categories: dict[str, set[str]] = {}
        for row in receipt_rows:
            receipt_categories.setdefault(str(row["operation_id"]), set()).add(str(row["category"]))
        episodes = {
            str(row["episode_id"]): self._experiment_episode_record(row) for row in episode_rows
        }
        control_node_ids = (
            set(tasks_by_control) | set(watchers_by_control) if requested is None else requested
        )
        return {
            control_node_id: self._derive_experiment_loop_runtime(
                tasks_by_control.get(control_node_id, []),
                watchers_by_control.get(control_node_id, []),
                receipt_categories,
                episodes,
            )
            for control_node_id in control_node_ids
        }

    @classmethod
    def _derive_experiment_loop_runtime(
        cls,
        task_entries: list[tuple[sqlite3.Row, dict[str, object]]],
        watchers: list[StoredWatcherRecord],
        receipt_categories: dict[str, set[str]],
        episodes: dict[str, ExperimentEpisodeRecord],
    ) -> ExperimentLoopRuntime:
        """Purely derive one runtime from an already-loaded project ledger."""

        root_entries = [entry for entry in task_entries if entry[0]["parent_operation_id"] is None]
        if not root_entries:
            return ExperimentLoopRuntime()
        _, root_request = max(
            root_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        episode_id = root_request.get("control_episode_id")
        if not isinstance(episode_id, str):
            raise ValueError("Stored experiment-loop root is missing its episode id.")
        try:
            uuid.UUID(episode_id)
        except ValueError as exc:
            raise ValueError("Stored experiment-loop root has an invalid episode id.") from exc

        episode_entries = [
            entry for entry in task_entries if entry[1].get("control_episode_id") == episode_id
        ]
        episode_entries.sort(
            key=lambda entry: (
                entry[0]["attempt"],
                entry[0]["created_at"],
                entry[0]["storage_rowid"],
            ),
            reverse=True,
        )
        episode = episodes.get(episode_id)
        compatible_watchers = [
            record
            for record in watchers
            if cls._experiment_watcher_matches_current(record, root_request, episode)
        ]
        latest_by_invocation: dict[
            int,
            tuple[sqlite3.Row, dict[str, object]],
        ] = {}
        for row, request in episode_entries:
            invocation = request.get("control_invocation")
            if isinstance(invocation, int) and invocation not in latest_by_invocation:
                latest_by_invocation[invocation] = (row, request)
        ceiling = root_request.get("control_invocation_ceiling")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("Stored experiment-loop root is missing its pinned ceiling.")
        if not latest_by_invocation or min(latest_by_invocation) < 1:
            raise ValueError("Stored experiment-loop root is missing its invocation number.")
        invocations_used = max(latest_by_invocation)
        if set(latest_by_invocation) != set(range(1, invocations_used + 1)):
            raise ValueError("Stored experiment-loop root invocations are out of sequence.")
        if invocations_used > ceiling:
            raise ValueError("Stored experiment-loop root exceeds its pinned ceiling.")
        unresolved = any(
            row["status"] in {"queued", "running", "pausing", "paused", "failed", "interrupted"}
            and "experiment_recovery_abandoned"
            not in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in latest_by_invocation.values()
        )
        detached_work_active = any(
            record.status in {"active", "degraded"} and not record.notified
            for record in compatible_watchers
        )
        watcher_degraded = any(
            record.status == "degraded" and not record.notified for record in compatible_watchers
        )
        watcher_completion_pending = any(
            record.status == "completed" and not record.notified for record in compatible_watchers
        )
        has_watcher = detached_work_active or watcher_completion_pending
        episode_exited = any(
            "experiment_loop_exit" in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in episode_entries
        )
        at_ceiling = invocations_used >= ceiling
        pins = root_request.get("control_decision_bundle")
        if not isinstance(pins, list):
            raise ValueError("Stored experiment-loop root is missing its pinned decision bundle.")
        control_revision = root_request.get("control_revision")
        if not isinstance(control_revision, int) or isinstance(control_revision, bool):
            raise ValueError("Stored experiment-loop root is missing its control revision.")
        completion_criteria = root_request.get("control_completion_criteria")
        if not isinstance(completion_criteria, list) or any(
            not isinstance(item, str) for item in completion_criteria
        ):
            raise ValueError("Stored experiment-loop root is missing its completion criteria.")
        current_row, current_request = max(
            episode_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        binding_request = next(
            (
                request
                for row, request in episode_entries
                if episode is not None and row["operation_id"] == episode.last_turn_operation_id
            ),
            root_request,
        )
        current_invocation = current_request.get("control_invocation")
        return ExperimentLoopRuntime(
            episode_id=episode_id,
            invocations_used=invocations_used,
            invocation_ceiling=ceiling,
            control_revision=control_revision,
            task_active=unresolved,
            detached_work_active=detached_work_active,
            watcher_degraded=watcher_degraded,
            watcher_completion_pending=watcher_completion_pending,
            episode_exited=episode_exited,
            active=unresolved
            or (
                has_watcher
                and not at_ceiling
                and not episode_exited
                and not (episode is not None and episode.stop_requested_at is not None)
            ),
            paused=has_watcher
            and at_ceiling
            and not unresolved
            and not episode_exited
            and not (episode is not None and episode.stop_requested_at is not None),
            decision_bundle=pins,
            completion_criteria=completion_criteria,
            stop_requested=episode is not None and episode.stop_requested_at is not None,
            stop_settled=episode is not None and episode.stop_settled_at is not None,
            session_bound=episode is not None and episode.session_bound,
            session_diagnostic=episode.session_diagnostic if episode else None,
            provider=(episode.provider if episode is not None else None)
            or _optional_str(binding_request.get("provider")),
            model=(
                binding_request["model"] if isinstance(binding_request.get("model"), str) else None
            ),
            reasoning=_optional_str(binding_request.get("reasoning")),
            run_on=(episode.execution_machine if episode is not None else None)
            or _optional_str(binding_request.get("run_on")),
            execution_host=episode.execution_host if episode else None,
            run_truth_scope=(
                [str(item) for item in root_request["run_truth_scope"]]
                if isinstance(root_request.get("run_truth_scope"), list)
                else None
            ),
            chat_id=_optional_str(root_request.get("chat_id")),
            current_operation_id=current_row["operation_id"],
            current_status=current_row["status"],
            current_phase=current_row["phase"],
            current_status_message=current_row["status_message"],
            current_last_activity_at=current_row["last_activity_at"],
            current_invocation=(
                current_invocation if isinstance(current_invocation, int) else None
            ),
        )

    @staticmethod
    def _experiment_watcher_matches_current(
        record: StoredWatcherRecord,
        root_request: dict[str, object],
        episode: ExperimentEpisodeRecord | None,
    ) -> bool:
        """Whether this node-owned observer can wake the current episode.

        Conversation, provider, execution-machine alias, and package provenance
        are deliberately absent. The episode owns its session and policy; the
        watcher owns only the node, episode, and check execution host needed to
        answer the operational question.
        """

        continuation = record.continuation
        control_node_id = root_request.get("control_node_id")
        episode_matches = episode is None or (
            record.project_id == episode.project_id
            and episode.control_node_id == control_node_id
            and record.execution_host == episode.execution_host
        )
        return (
            continuation.patch_kind == "experiment_loop"
            and continuation.control_node_id == control_node_id
            and record.node_id == control_node_id
            and record.experiment_episode_id is not None
            and episode_matches
        )

    @staticmethod
    def _experiment_episode_root_request(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, episode_id),
        ).fetchone()
        return json.loads(row["request_json"]) if row is not None else None

    @classmethod
    def _watcher_suppressed_by_current_stop(
        cls,
        connection: sqlite3.Connection,
        record: StoredWatcherRecord,
        cache: dict[
            tuple[str, str],
            tuple[dict[str, object], ExperimentEpisodeRecord] | None,
        ],
    ) -> bool:
        continuation = record.continuation
        control_node_id = continuation.control_node_id
        if continuation.patch_kind != "experiment_loop" or not control_node_id:
            return False
        key = (record.project_id, control_node_id)
        if key not in cache:
            root = connection.execute(
                """
                SELECT request_json FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                key,
            ).fetchone()
            context = None
            if root is not None:
                root_request = json.loads(root["request_json"])
                episode_id = root_request.get("control_episode_id")
                episode_row = (
                    connection.execute(
                        "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                        (episode_id,),
                    ).fetchone()
                    if isinstance(episode_id, str)
                    else None
                )
                if episode_row is not None and episode_row["stop_requested_at"] is not None:
                    context = (root_request, cls._experiment_episode_record(episode_row))
            cache[key] = context
        context = cache[key]
        return context is not None and cls._experiment_watcher_matches_current(
            record,
            context[0],
            context[1],
        )

    def experiment_watcher_compatible_with_episode(
        self,
        watcher_id: str,
        episode_id: str,
    ) -> bool:
        """Whether a stopped observer belonged to that episode operationally.

        Watcher origin remains immutable provenance. This derived relation lets
        a fresh post-stop Run stage compatible adopted observers as history even
        when an older invocation or episode originally armed them.
        """

        with self.connection() as connection:
            watcher_row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?",
                (watcher_id,),
            ).fetchone()
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if watcher_row is None or episode_row is None:
                return False
            record = self._watcher_record(watcher_row)
            episode = self._experiment_episode_record(episode_row)
            root_request = self._experiment_episode_root_request(
                connection,
                episode.project_id,
                episode.control_node_id,
                episode_id,
            )
        return root_request is not None and self._experiment_watcher_matches_current(
            record,
            root_request,
            episode,
        )

    def active_experiment_control_ids(self, project_id: str) -> set[str]:
        """Return Experiments whose newest operational episode is still live."""

        return {
            control_node_id
            for control_node_id, runtime in self._project_experiment_loop_runtimes(
                project_id, None
            ).items()
            if runtime.active
        }

    def record_watcher_check(
        self,
        watcher_id: str,
        *,
        status: WatcherStatus,
        exit_code: int | None,
        error: str | None,
        checked_at: str | None = None,
    ) -> WatcherRecord:
        if status == "degraded" and not error:
            raise ValueError("a degraded watcher requires a check error")
        if status != "degraded":
            error = None
        timestamp = checked_at or self.now()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)
            ).fetchone()
            if row is None:
                raise KeyError(watcher_id)
            current = self._watcher_record(row)
            if not isinstance(current, WatcherRecord):
                raise ValueError("a graph condition cannot receive a shell check result")
            if current.status not in {"active", "degraded"} or current.notified:
                return current
            consecutive_error_count = (
                current.consecutive_error_count + 1 if status == "degraded" else 0
            )
            next_check_at = (
                watcher_next_check_at(watcher_id, timestamp, consecutive_error_count)
                if status in {"active", "degraded"}
                else None
            )
            cursor = connection.execute(
                """
                UPDATE watchers
                SET status = ?, last_checked_at = ?, last_exit_code = ?, last_error = ?,
                    next_check_at = ?, consecutive_error_count = ?,
                    completed_at = CASE
                        WHEN ? = 'completed' THEN COALESCE(completed_at, ?)
                        ELSE completed_at
                    END
                WHERE watcher_id = ? AND status IN ('active', 'degraded') AND notified = 0
                """,
                (
                    status,
                    timestamp,
                    exit_code,
                    error,
                    next_check_at,
                    consecutive_error_count,
                    status,
                    timestamp,
                    watcher_id,
                ),
            )
            if cursor.rowcount == 0:
                return self._watcher_record(
                    connection.execute(
                        "SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)
                    ).fetchone()
                )
        stored = self.watcher(watcher_id)
        assert isinstance(stored, WatcherRecord)
        return stored

    def completed_watcher_groups(self) -> list[list[StoredWatcherRecord]]:
        """Return compatible ready delivery units without splitting Experiment groups."""

        with self.connection() as connection:
            units = self._ready_watcher_delivery_units(connection)
        groups: dict[tuple[object, ...], list[StoredWatcherRecord]] = {}
        for unit in units:
            first = unit[0]
            key = (
                (
                    first.project_id,
                    "experiment_loop",
                    first.node_id,
                    first.execution_host,
                    self._automatic_watcher_delivery_policy(first.continuation),
                )
                if first.continuation.patch_kind == "experiment_loop"
                else (
                    first.project_id,
                    first.origin_task_kind,
                    first.chat_id,
                    first.node_id,
                    first.execution_host,
                    self._automatic_watcher_delivery_policy(first.continuation),
                )
            )
            groups.setdefault(key, []).extend(unit)
        return list(groups.values())

    def _ready_watcher_delivery_units(
        self,
        connection: sqlite3.Connection,
    ) -> list[list[StoredWatcherRecord]]:
        """Build indivisible ready groups plus ordinary completed observer units."""

        ungrouped_rows = connection.execute(
            """
            SELECT * FROM watchers
            WHERE group_id IS NULL AND status = 'completed' AND notified = 0
            ORDER BY completed_at, created_at, watcher_id
            """
        ).fetchall()
        grouped_rows = connection.execute(
            """
            SELECT * FROM watchers
            WHERE group_id IN (
                SELECT DISTINCT group_id FROM watchers
                WHERE group_id IS NOT NULL AND notified = 0
                  AND (
                    status = 'completed'
                    OR (
                        status = 'degraded'
                        AND consecutive_error_count >= ?
                    )
                  )
            )
            ORDER BY completed_at, created_at, watcher_id
            """,
            (WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT,),
        ).fetchall()
        ungrouped = [self._watcher_record(row) for row in ungrouped_rows]
        grouped_records = [self._watcher_record(row) for row in grouped_rows]
        stopping_contexts: dict[
            tuple[str, str], tuple[dict[str, object], ExperimentEpisodeRecord] | None
        ] = {}
        units: list[list[StoredWatcherRecord]] = []
        grouped: dict[str, list[StoredWatcherRecord]] = {}
        for record in ungrouped:
            if self._watcher_suppressed_by_current_stop(connection, record, stopping_contexts):
                continue
            units.append([record])
        for record in grouped_records:
            assert record.group_id is not None
            grouped.setdefault(record.group_id, []).append(record)
        for members in grouped.values():
            ready = self._ready_group_members(members)
            if not ready:
                continue
            if self._watcher_suppressed_by_current_stop(connection, ready[0], stopping_contexts):
                continue
            units.append(ready)
        return units

    @staticmethod
    def _ready_group_members(
        members: list[StoredWatcherRecord],
    ) -> list[StoredWatcherRecord] | None:
        """Return deliverable members only when a durable group is collectively ready."""

        if not members or any(item.group_id is None for item in members):
            return None
        if any(item.status == "stopped" and item.stopped_by != "agent" for item in members):
            return None
        deliverable = [
            item
            for item in members
            if not (item.status == "stopped" and item.stopped_by == "agent")
        ]
        if not deliverable or any(item.notified for item in deliverable):
            return None
        if any(
            item.status == "active"
            or (
                item.status == "degraded"
                and item.consecutive_error_count < WATCHER_GROUP_DIAGNOSTIC_ERROR_COUNT
            )
            or item.status not in {"completed", "degraded"}
            for item in deliverable
        ):
            return None
        return deliverable

    def completed_experiment_watcher_group(
        self,
        project_id: str,
        control_node_id: str,
    ) -> list[StoredWatcherRecord] | None:
        """Return the oldest frozen group a human may reauthorize.

        Unlike automatic delivery, human reauthorization preserves the full
        watcher configuration, including model, reasoning, and package pointers.
        """

        with self.connection() as connection:
            units = self._ready_watcher_delivery_units(connection)
        groups: dict[tuple[object, ...], list[StoredWatcherRecord]] = {}
        for unit in units:
            first = unit[0]
            if (
                first.project_id != project_id
                or first.continuation.patch_kind != "experiment_loop"
                or first.continuation.control_node_id != control_node_id
            ):
                continue
            key = (
                first.node_id,
                first.execution_host,
                self._automatic_watcher_delivery_policy(first.continuation),
            )
            groups.setdefault(key, []).extend(unit)
        return next(iter(groups.values()), None)

    def create_watcher_notification_task(
        self,
        record: AgentTaskRecord,
        watcher_ids: list[str],
    ) -> AgentTaskRecord | None:
        """Queue a wake and mark its completed watchers notified in one transaction.

        A live task in the same conversation wins its slot. In that case no
        watcher row changes, and the completed group can be retried later.
        """

        ids = list(dict.fromkeys(watcher_ids))
        if not ids or len(ids) != len(watcher_ids):
            raise ValueError("a watcher notification requires unique watcher ids")
        if record.status != "queued":
            raise ValueError("a watcher notification task must be queued")
        requested_ids = record.request.get("watcher_ids")
        if (
            not isinstance(requested_ids, list)
            or any(not isinstance(item, str) for item in requested_ids)
            or len(requested_ids) != len(set(requested_ids))
            or set(requested_ids) != set(ids)
        ):
            raise ValueError("the watcher notification request must name exactly its watcher ids")
        placeholders = ",".join("?" for _ in ids)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM watchers
                    WHERE watcher_id IN ({placeholders})
                        AND status IN ('completed', 'degraded') AND notified = 0
                    """,
                    ids,
                ).fetchall()
                if {str(row["watcher_id"]) for row in rows} != set(ids):
                    raise ValueError("watchers are missing, unready, or already notified")
                watchers = [self._watcher_record(row) for row in rows]
                self._validate_watcher_notification_members(connection, watchers)
                if {item.project_id for item in watchers} != {record.project_id}:
                    raise ValueError("watchers and notification task belong to different projects")
                bindings = {
                    (
                        (
                            "experiment_loop",
                            item.node_id,
                            item.execution_host,
                            self._automatic_watcher_delivery_policy(item.continuation),
                        )
                        if item.continuation.patch_kind == "experiment_loop"
                        else (
                            item.origin_task_kind,
                            item.chat_id,
                            item.node_id,
                            item.execution_host,
                            self._watcher_delivery_policy(item.continuation),
                        )
                    )
                    for item in watchers
                }
                if len(bindings) != 1:
                    raise ValueError("one notification cannot merge incompatible watch lists")
                self._validate_watcher_notification_scope(connection, record, watchers)
                if self._experiment_wake_is_stopped(connection, record):
                    return None
                if self._has_active_chat_overlap(connection, record):
                    return None
                if record.kind == "campaign":
                    campaign_id = record.request.get("campaign_id")
                    if not isinstance(campaign_id, str) or campaign_id != record.campaign_id:
                        raise ValueError("campaign watcher wake has invalid campaign lineage")
                    campaign_row = connection.execute(
                        "SELECT * FROM campaigns WHERE campaign_id = ?",
                        (campaign_id,),
                    ).fetchone()
                    if campaign_row is None:
                        raise KeyError(campaign_id)
                    role = TypeAdapter(CampaignInvocationRole).validate_python(
                        record.request.get("role")
                    )
                    self._insert_campaign_task(
                        connection,
                        self._campaign_record(campaign_row),
                        record,
                        role,
                    )
                else:
                    self._insert_agent_task(connection, record)
                cursor = connection.execute(
                    f"""
                    UPDATE watchers
                    SET notified = 1, notification_operation_id = ?
                    WHERE watcher_id IN ({placeholders})
                        AND status IN ('completed', 'degraded') AND notified = 0
                    """,
                    [record.operation_id, *ids],
                )
                if cursor.rowcount != len(ids):
                    raise RuntimeError("watcher notification changed during its transaction")
        except CampaignActorBusy:
            return None
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not queue the watcher notification task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def resolve_watcher_delivery_authorizer(
        self,
        watcher_ids: list[str],
    ) -> tuple[AuthorizedHuman | None, str | None]:
        """Resolve one automatic wake's human authority or terminalize it.

        Legacy tasks have no trustworthy authorizer to inherit. Missing tasks,
        partial snapshots, and a delivery unit assembled from different humans
        are equally non-recoverable without a new human action. Consume those
        completed watchers with a durable, UI-visible diagnostic so the poller
        cannot retry an unauthorized wake forever.

        The resolution and terminal transition share the same write transaction
        as the watcher readiness check. A concurrent notification claim or Stop
        therefore wins cleanly instead of producing both a wake and a terminal
        diagnostic.
        """

        ids = list(dict.fromkeys(watcher_ids))
        if not ids or len(ids) != len(watcher_ids):
            raise ValueError("watcher delivery authorization requires unique watcher ids")
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT * FROM watchers
                WHERE watcher_id IN ({placeholders})
                  AND status IN ('completed', 'degraded')
                  AND notified = 0 AND notification_operation_id IS NULL
                """,
                ids,
            ).fetchall()
            if {str(row["watcher_id"]) for row in rows} != set(ids):
                return None, None
            watchers = [self._watcher_record(row) for row in rows]
            self._validate_watcher_notification_members(connection, watchers)

            origin_ids = sorted({item.origin_operation_id for item in watchers})
            origin_placeholders = ",".join("?" for _ in origin_ids)
            origin_rows = connection.execute(
                f"""
                SELECT operation_id, authorized_space_id, authorized_user_id,
                       authorized_display_name
                FROM graph_runs
                WHERE operation_id IN ({origin_placeholders})
                """,
                origin_ids,
            ).fetchall()
            by_operation = {str(row["operation_id"]): row for row in origin_rows}

            diagnostic: str | None = None
            if set(by_operation) != set(origin_ids):
                diagnostic = (
                    "Automatic watcher wake stopped: an originating task is unavailable, so "
                    "RCP cannot prove who authorized the wake. Start a new Work turn or "
                    "Experiment Run to continue."
                )
            else:
                try:
                    authorizers = [
                        self._authorized_human_snapshot(by_operation[operation_id])
                        for operation_id in origin_ids
                    ]
                except RuntimeError:
                    diagnostic = (
                        "Automatic watcher wake stopped: an originating task has an invalid "
                        "human authorizer snapshot, so RCP cannot prove who authorized the "
                        "wake. Start a new Work turn or Experiment Run to continue."
                    )
                else:
                    if any(authorizer is None for authorizer in authorizers):
                        diagnostic = (
                            "Automatic watcher wake stopped: an originating task predates "
                            "durable human attribution, so RCP cannot prove who authorized the "
                            "wake. Start a new Work turn or Experiment Run to continue."
                        )
                    else:
                        authorized_by = authorizers[0]
                        assert authorized_by is not None
                        if any(authorizer != authorized_by for authorizer in authorizers[1:]):
                            diagnostic = (
                                "Automatic watcher wake stopped: the originating tasks have "
                                "different human authorizers, so RCP cannot choose one. Start a "
                                "new Work turn or Experiment Run to continue."
                            )
                        else:
                            return authorized_by, None

            assert diagnostic is not None
            timestamp = self.now()
            cursor = connection.execute(
                f"""
                UPDATE watchers
                SET status = 'stopped', notified = 1, next_check_at = NULL,
                    stop_reason = ?, stopped_at = COALESCE(stopped_at, ?)
                WHERE watcher_id IN ({placeholders})
                  AND status IN ('completed', 'degraded')
                  AND notified = 0 AND notification_operation_id IS NULL
                """,
                [diagnostic, timestamp, *ids],
            )
            if cursor.rowcount != len(ids):
                raise RuntimeError(
                    "Watcher delivery changed during its authorizer terminalization."
                )

            episode_ids = sorted(
                {
                    item.experiment_episode_id
                    for item in watchers
                    if item.experiment_episode_id is not None
                }
            )
            if episode_ids:
                episode_placeholders = ",".join("?" for _ in episode_ids)
                connection.execute(
                    f"""
                    UPDATE experiment_episodes
                    SET session_diagnostic = ?, updated_at = ?
                    WHERE episode_id IN ({episode_placeholders})
                    """,
                    [diagnostic, timestamp, *episode_ids],
                )
            return None, diagnostic

    def _validate_watcher_notification_members(
        self,
        connection: sqlite3.Connection,
        watchers: list[StoredWatcherRecord],
    ) -> None:
        """Require a delivery claim to contain every ready member of each group."""

        requested = {item.watcher_id for item in watchers}
        group_ids = {item.group_id for item in watchers if item.group_id is not None}
        for watcher in watchers:
            if watcher.group_id is None and watcher.status != "completed":
                raise ValueError("an ungrouped watcher must complete before delivery")
        for group_id in group_ids:
            assert group_id is not None
            rows = connection.execute(
                "SELECT * FROM watchers WHERE group_id = ? ORDER BY created_at, watcher_id",
                (group_id,),
            ).fetchall()
            ready = self._ready_group_members([self._watcher_record(row) for row in rows])
            if ready is None:
                raise ValueError("a watcher group is not ready for delivery")
            ready_ids = {item.watcher_id for item in ready}
            if ready_ids != (requested & {item.watcher_id for item in ready}):
                raise ValueError("a watcher group must be claimed as one delivery unit")

    @staticmethod
    def _experiment_wake_is_stopped(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> bool:
        """Refuse an automatic wake whose episode already carries a stop request.

        The check runs inside the claim's own write transaction, so a claim either
        commits before the stop or finds it — there is no window where both win.
        """

        request = record.request
        if request.get("patch_kind") != "experiment_loop" or request.get("trigger") != "watcher":
            return False
        episode_id = request.get("control_episode_id")
        if not isinstance(episode_id, str):
            return False
        row = connection.execute(
            "SELECT stop_requested_at FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return row is not None and row["stop_requested_at"] is not None

    @staticmethod
    def _watcher_delivery_policy(continuation: WatcherContinuation) -> str:
        policy = continuation.model_dump(mode="json")
        if continuation.patch_kind == "experiment_loop" and policy.get("model") is None:
            # Legacy Experiment watchers stored the provider-default sentinel
            # as null. It is immutable policy, equivalent to today's "".
            policy["model"] = ""
        for field in (
            "control_revision",
            "control_episode_id",
            "control_invocation",
            "control_invocation_ceiling",
            "control_decision_bundle",
            "control_completion_criteria",
        ):
            policy.pop(field, None)
        return json.dumps(policy, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _automatic_watcher_delivery_policy(continuation: WatcherContinuation) -> str:
        """Policy key for poller-driven delivery; generic Work stays unchanged."""

        if continuation.patch_kind != "experiment_loop":
            return AppStore._watcher_delivery_policy(continuation)
        policy = {
            "patch_kind": continuation.patch_kind,
            "control_node_id": continuation.control_node_id,
        }
        return json.dumps(policy, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validate_watcher_notification_scope(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
        watchers: list[StoredWatcherRecord],
    ) -> None:
        first = watchers[0]
        continuation = first.continuation
        request = record.request
        trigger = request.get("trigger")
        campaign_wake = first.origin_task_kind == "campaign"
        if campaign_wake:
            if record.kind != "campaign" or record.campaign_id is None:
                raise ValueError("campaign watchers must wake a campaign task")
            actor_bindings: set[tuple[object, ...]] = set()
            for watcher in watchers:
                origin = connection.execute(
                    """
                    SELECT run.campaign_id, run.request_json, invocation.role
                    FROM graph_runs AS run
                    JOIN campaign_invocations AS invocation
                      ON invocation.operation_id = run.operation_id
                    WHERE run.operation_id = ?
                    """,
                    (watcher.origin_operation_id,),
                ).fetchone()
                if origin is None or origin["campaign_id"] != record.campaign_id:
                    raise ValueError("campaign watcher origin is outside the campaign")
                origin_request = json.loads(origin["request_json"])
                actor_bindings.add(
                    (
                        origin["campaign_id"],
                        origin_request.get("actor_operation_id") or watcher.origin_operation_id,
                        origin["role"],
                        origin_request.get("control_node_id"),
                    )
                )
            if len(actor_bindings) != 1:
                raise ValueError("one campaign watcher wake cannot merge different actors")
            expected_campaign, expected_actor, expected_role, expected_seat = next(
                iter(actor_bindings)
            )
            actual = (
                request.get("campaign_id"),
                request.get("actor_operation_id"),
                request.get("role"),
                request.get("control_node_id"),
            )
            if actual != (expected_campaign, expected_actor, expected_role, expected_seat):
                raise ValueError("campaign watcher wake changed its canonical actor binding")
        elif continuation.patch_kind == "experiment_loop":
            if (
                record.kind != "node_chat"
                or request.get("node_id") != continuation.control_node_id
                or not isinstance(request.get("chat_id"), str)
                or not request.get("chat_id")
            ):
                raise ValueError("Experiment watcher delivery must target its node chat.")
        else:
            expected = {
                "kind": first.origin_task_kind,
                "chat_id": first.chat_id,
                "node_id": first.node_id,
            }
            actual = {
                "kind": record.kind,
                "chat_id": request.get("chat_id"),
                "node_id": request.get("node_id"),
            }
            mismatched = sorted(key for key, value in expected.items() if actual[key] != value)
            if mismatched:
                raise ValueError(
                    f"watcher notification changed immutable scope: {', '.join(mismatched)}"
                )
        request_continuation_data = {
            key: request[key] for key in WatcherContinuation.model_fields if key in request
        }
        for nullable_list in ("workflow_ids", "skill_ids", "resolved_skill_packages"):
            if request_continuation_data.get(nullable_list) is None:
                request_continuation_data[nullable_list] = []
        request_continuation = WatcherContinuation.model_validate(request_continuation_data)
        request_policy = (
            AppStore._automatic_watcher_delivery_policy(request_continuation)
            if continuation.patch_kind == "experiment_loop"
            else AppStore._watcher_delivery_policy(request_continuation)
        )
        continuation_policy = (
            AppStore._automatic_watcher_delivery_policy(continuation)
            if continuation.patch_kind == "experiment_loop"
            else AppStore._watcher_delivery_policy(continuation)
        )
        if request_policy != continuation_policy:
            raise ValueError("watcher notification changed its immutable delivery policy")
        if campaign_wake:
            graph_wake = all(isinstance(item, GraphWatcherRecord) for item in watchers)
            expected_cause = "graph_condition" if graph_wake else "watcher"
            if request.get("wake_cause") != expected_cause:
                raise ValueError("campaign watcher wake changed its continuation cause")
            return
        if continuation.patch_kind != "experiment_loop":
            if trigger != "watcher":
                raise ValueError("a generic watcher notification must use the watcher trigger")
            return
        invocation = request.get("control_invocation")
        episode_id = request.get("control_episode_id")
        if trigger == "watcher":
            if not isinstance(invocation, int) or invocation < 2:
                raise ValueError("an automatic Experiment wake must continue an existing episode")
            newest = connection.execute(
                """
                SELECT kind, request_json FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (record.project_id, continuation.control_node_id),
            ).fetchone()
            newest_request = json.loads(newest["request_json"]) if newest is not None else None
            if newest_request is None or newest_request.get("control_episode_id") != episode_id:
                raise ValueError("an automatic Experiment wake must use the newest episode")
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            episode = (
                AppStore._experiment_episode_record(episode_row)
                if episode_row is not None
                else None
            )
            if episode is not None and (
                record.kind != newest["kind"] or request.get("chat_id") != episode.chat_id
            ):
                raise ValueError("Experiment watcher delivery changed its episode wake target.")
            if any(
                not AppStore._experiment_watcher_matches_current(item, newest_request, episode)
                for item in watchers
            ):
                raise ValueError(
                    "completed watchers are incompatible with the current Experiment episode"
                )
            return
        if trigger != "experiment_run" or invocation != 1:
            raise ValueError("a human Experiment watcher claim must start a new episode")
        previous = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (record.project_id, continuation.control_node_id),
        ).fetchone()
        if previous is None:
            raise ValueError("a human watcher claim requires a prior Experiment episode")
        previous_request = json.loads(previous["request_json"])
        if previous_request.get("control_episode_id") == episode_id:
            raise ValueError("a human watcher claim must authorize a fresh episode")

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
                SELECT operation_id, project_id, dispatch_authority_json,
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
        return AppStore._agent_command_from_connection(connection, row["command_id"])

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

        current = now or datetime.now(UTC)
        inactive = """
            operation_id NOT IN (
                SELECT operation_id FROM graph_runs
                WHERE status IN ('queued', 'running', 'pausing')
            )
        """
        patch_cutoff = (current - timedelta(days=PATCH_OUTPUT_RETENTION_DAYS)).isoformat()
        trace_cutoff = (current - timedelta(days=RUN_TRACE_RETENTION_DAYS)).isoformat()
        with self.connection() as connection:
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

    @staticmethod
    def _chat_session_context_record(row: sqlite3.Row) -> ChatSessionContextRecord:
        return ChatSessionContextRecord.model_validate(dict(row))

    @staticmethod
    def _result_view_record(row: sqlite3.Row) -> ResultViewRecord:
        return ResultViewRecord.model_validate(dict(row))

    @staticmethod
    def _project_record(row: sqlite3.Row) -> ProjectRecord:
        data = dict(row)
        data["state_remote"] = bool(data["state_remote"])
        if data["reachable"] is not None:
            data["reachable"] = bool(data["reachable"])
        return ProjectRecord.model_validate(data)

    @staticmethod
    def _watcher_record(row: sqlite3.Row) -> StoredWatcherRecord:
        data = dict(row)
        data["continuation"] = json.loads(data.pop("continuation_json"))
        if "experiment_episode_id" not in data:
            data["experiment_episode_id"] = data["continuation"].get("control_episode_id")
        data["notified"] = bool(data["notified"])
        graph_condition_json = data.pop("graph_condition_json", None)
        if graph_condition_json is None:
            data.pop("armed_revision", None)
            return WatcherRecord.model_validate(data)
        data.pop("check_command", None)
        data.pop("log_path", None)
        data.pop("cwd", None)
        data["last_evaluated_at"] = data.pop("last_checked_at", None)
        data.pop("last_exit_code", None)
        data.pop("last_error", None)
        data.pop("next_check_at", None)
        data.pop("consecutive_error_count", None)
        data.pop("group_id", None)
        data.pop("group_label", None)
        data["condition"] = json.loads(graph_condition_json)
        return GraphWatcherRecord.model_validate(data)

    @staticmethod
    def _experiment_episode_record(row: sqlite3.Row) -> ExperimentEpisodeRecord:
        data = dict(row)
        data["last_watcher_ids"] = json.loads(data.pop("last_watcher_ids_json"))
        data["context_baseline"] = json.loads(data.pop("context_baseline_json"))
        return ExperimentEpisodeRecord.model_validate(data)

    def _agent_task_record(self, row: sqlite3.Row) -> AgentTaskRecord:
        data = dict(row)
        recovery_abandoned = bool(data.pop("recovery_abandoned", False))
        data.pop("campaign_worker_handoffs_cleared_at", None)
        dispatch_json = data.pop("dispatch_authority_json", None)
        data["dispatch_authority"] = (
            AgentDispatchAuthority.model_validate_json(dispatch_json)
            if dispatch_json is not None
            else None
        )
        data["authorized_by"] = self._authorized_human_snapshot(data)
        data.pop("authorized_space_id", None)
        data.pop("authorized_user_id", None)
        data.pop("authorized_display_name", None)
        data["request"] = json.loads(data.pop("request_json"))
        result_json = data.pop("result_json", None)
        data["result"] = json.loads(result_json) if result_json else None
        status = data["status"]
        started = self._parse_time(data.get("started_at"))
        finished = self._parse_time(data.get("finished_at"))
        end = finished or datetime.now(UTC)
        elapsed = max(0.0, (end - started).total_seconds()) if started else 0.0
        estimate = max(1.0, float(data.get("estimate_seconds") or 300.0))
        if status == "succeeded":
            progress = 1.0
        elif not started:
            progress = 0.0
        elif elapsed <= estimate:
            progress = 0.85 * elapsed / estimate
        else:
            progress = 0.85 + 0.14 * (1.0 - math.exp(-(elapsed - estimate) / estimate))
        data["elapsed_seconds"] = round(elapsed, 1)
        data["progress"] = round(min(0.99, max(0.0, progress)), 4) if status != "succeeded" else 1.0
        active = status in ACTIVE_AGENT_TASK_STATUSES
        stage_ready = not data.get("stage_host") or bool(data.get("stage_root"))
        data["can_pause"] = status in {"queued", "running"}
        data["can_resume"] = (
            status in {"paused", "interrupted"}
            and bool(data.get("native_session_id"))
            and stage_ready
            and not recovery_abandoned
        )
        data["can_retry"] = (
            status in {"paused", "interrupted", "failed"} and not active and not recovery_abandoned
        )
        return AgentTaskRecord.model_validate(data)

    @staticmethod
    def _agent_task_event_record(row: sqlite3.Row) -> AgentTaskEventRecord:
        data = dict(row)
        payload_json = data.pop("payload_json", None)
        data["payload"] = json.loads(payload_json) if payload_json else None
        return AgentTaskEventRecord.model_validate(data)

    @staticmethod
    def _campaign_record(row: sqlite3.Row) -> CampaignRecord:
        data = dict(row)
        data["authorized_by"] = AppStore._authorized_human_snapshot(data)
        data.pop("authorized_space_id", None)
        data.pop("authorized_user_id", None)
        data.pop("authorized_display_name", None)
        return CampaignRecord.model_validate(data)

    @staticmethod
    def _campaign_recovery_record(row: sqlite3.Row) -> CampaignRecoveryRecord:
        return CampaignRecoveryRecord.model_validate(dict(row))

    @staticmethod
    def _campaign_report_record(row: sqlite3.Row) -> CampaignReportRecord:
        return CampaignReportRecord.model_validate(dict(row))

    @staticmethod
    def _campaign_message_record(row: sqlite3.Row) -> CampaignMessageRecord:
        data = dict(row)
        data["authorized_by"] = AppStore._authorized_human_snapshot(data)
        data.pop("authorized_space_id", None)
        data.pop("authorized_user_id", None)
        data.pop("authorized_display_name", None)
        return CampaignMessageRecord.model_validate(data)

    @staticmethod
    def _authorized_human_snapshot(
        row: sqlite3.Row | dict[str, object],
    ) -> AuthorizedHuman | None:
        values = {
            "space_id": row["authorized_space_id"],
            "user_id": row["authorized_user_id"],
            "display_name": row["authorized_display_name"],
        }
        present = {name for name, value in values.items() if value is not None}
        if not present:
            return None
        if len(present) != len(values):
            raise RuntimeError(
                "Agent task authorizer snapshot is partial; refusing to infer identity."
            )
        try:
            return AuthorizedHuman.model_validate(values)
        except ValueError as exc:
            raise RuntimeError("Agent task authorizer snapshot is invalid.") from exc

    @staticmethod
    def _agent_usage_record(row: sqlite3.Row) -> AgentUsageRecord:
        data = dict(row)
        data["counted"] = bool(data["counted"])
        data["provider_fields"] = json.loads(data.pop("provider_fields_json"))
        return AgentUsageRecord.model_validate(data)

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _plain_html_name(value: str, *, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\x00" in value:
        raise ValueError(f"{label} must be a plain base name")
    if Path(value).suffix.casefold() != ".html":
        raise ValueError(f"{label} must end in .html")
    return value


def _required_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("result view timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("result view timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _result_view_is_visible(
    record: ResultViewRecord,
    *,
    as_of: datetime | None,
) -> bool:
    if record.kept_filename is not None:
        return True
    current = as_of or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("result view visibility time must include a timezone")
    return _required_timestamp(record.expires_at) > current.astimezone(UTC)
