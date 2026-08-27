from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from rcp.artifacts import validate_artifact_bytes
from rcp.core.authority import (
    AgentDispatchAuthority,
)
from rcp.core.models import (
    DISPLAY_NAME_MAX_LENGTH,
    AuthorizedHuman,
    normalize_display_name,
)
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.limits import (
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    TEAM_ENROLLMENT_CODE_MAX_LENGTH,
    WATCHER_ERROR_BACKOFF_SECONDS,
    WATCHER_HEALTHY_INTERVAL_SECONDS,
    WATCHER_SCHEDULE_JITTER_RATIO,
)
from rcp.providers import ProviderSkill, legacy_runtime_id, require_runtime_id, runtime_label
from rcp.skill_registry import SkillReference

if TYPE_CHECKING:
    pass


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


class ProjectMemberRecord(BaseModel):
    """One person's membership of one project.

    Membership is operational authority inside RCP. It binds the durable
    ``user_id`` and never a display name, so a member exists before they have
    chosen one. It lives in SQLite and never in ``.research/``.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str
    user_id: str
    seated_at: str
    seated_by: str | None = None

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        try:
            return _canonical_uuid4(value, label="user identity")
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc


class ProjectInvitationRecord(BaseModel):
    """One in-product invitation to join one project.

    It carries no code, no expiry, and no lockout, because it grants no
    credential — the person is already enrolled in the space.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    invitation_id: str
    project_id: str
    invited_user_id: str
    invited_by: str
    created_at: str
    response: Literal["accepted", "declined"] | None = None
    responded_at: str | None = None


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
    "auto_research",
    "branch_merge",
    "episode_report",
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
# A turn in one of these states is waiting on a person, not on the machine.
AWAITING_HUMAN_AGENT_TASK_STATUSES: frozenset[AgentTaskStatus] = frozenset(
    {"paused", "failed", "interrupted"}
)

# One table owns the status transitions used by the durable task lifecycle.
# Recovery actions are deliberately not represented here: Resume and Retry
# create child attempts and have additional native-session requirements.
AGENT_TASK_TRANSITIONS: dict[AgentTaskStatus, frozenset[AgentTaskStatus]] = {
    "running": frozenset({"queued"}),
    "pausing": frozenset({"queued", "running"}),
    "paused": frozenset({"queued", "running", "pausing"}),
    "succeeded": frozenset({"queued", "running", "pausing"}),
    "failed": frozenset({"queued", "running", "pausing"}),
    "interrupted": frozenset({"queued", "running", "pausing"}),
}

_EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE = "experiment_episode_context_candidate"
_MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC = (
    "This Experiment-loop turn cannot be resumed or retried because its pre-migration "
    "root has no retained episode context candidate. Use Stop loop and press Run to start "
    "a fresh episode."
)


class AgentTaskEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    operation_id: str
    created_at: str
    level: Literal["info", "warning", "error"]
    message: str
    event_kind: Literal["message", "command"] = "message"
    command_id: str | None = None
    episode_id: str | None = None
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
    model_config = ConfigDict(extra="forbid")

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
    episode_id: str | None = None
    runtime_id: str = ""
    #: How that runtime is named to a human. Derived here so a surface reporting
    #: what actually ran never maps a durable id back to the registry itself.
    runtime_label: str = ""
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
    write_scope_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
    # The lifecycle questions a surface asks about a task, answered here so no
    # reader has to ask them of `status` itself.
    active: bool = False
    queued: bool = False
    pausing: bool = False
    awaiting_human: bool = False
    paused: bool = False
    failed: bool = False
    settled: bool = False
    finished: bool = False
    status_label: str = ""
    visible: bool = True

    @model_validator(mode="after")
    def validate_provider_runtime(self) -> AgentTaskRecord:
        provider = self.request.get("provider")
        if not isinstance(provider, str) or not provider:
            if self.runtime_id:
                raise ValueError("an agent runtime requires a provider")
            return self
        try:
            legacy = legacy_runtime_id(provider)
        except ValueError:
            # An old row may name a provider RCP no longer supports. Nothing can
            # launch it, so it needs no runtime identity; keep it readable for
            # project deletion and forensic export instead of failing every read.
            return self
        if not self.runtime_id:
            self.runtime_id = legacy
        require_runtime_id(provider, self.runtime_id)
        self.runtime_label = runtime_label(provider, self.runtime_id)
        return self


# Fields a stored task carries because the row projection computed them, not
# because a caller supplied them. Equality between a requested task and its
# committed twin ignores these.
AGENT_TASK_PROJECTION_FIELDS: frozenset[str] = frozenset(
    {
        "elapsed_seconds",
        "progress",
        "can_pause",
        "can_resume",
        "can_retry",
        "active",
        "queued",
        "pausing",
        "awaiting_human",
        "paused",
        "failed",
        "settled",
        "finished",
        "status_label",
    }
)


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


AutoResearchRole = Literal["orchestrator", "worker"]
AutoResearchMessageRole = Literal["human", "orchestrator", "worker"]
AutoResearchRecoveryStatus = Literal["pending", "admitted", "exhausted", "blocked"]
AutoResearchRecoveryMode = Literal["exact", "clean", "blocked"]
AutoResearchChildExperimentState = Literal["pending", "running", "cancelled", "terminal"]
AutoResearchChildAdmissionState = Literal["accepted", "reflected", "cancelled"]
AutoResearchLifecycleNoticeState = Literal["pending", "delivered", "acknowledged"]
AutoResearchInboxReceiptMode = Literal["harvest", "clear"]
AutoResearchFinishDisposition = Literal["blocked", "completed"]
AutoResearchCommandFileKind = Literal["apply", "instruction", "goal"]
AutoResearchFinishBlockerKind = Literal[
    "spawned_work",
    "experiment_episode",
    "experiment_replacement",
    "lifecycle_notice",
    "child_admission",
]

EpisodeMode = Literal["auto_research", "experiment_loop"]
EpisodeStatus = Literal[
    "queued",
    "running",
    "stopping",
    "wrapping_up",
    "needs_action",
    "completed",
    "stopped",
    "failed",
]
EpisodeEnding = Literal["completed", "exhausted", "stopped", "failed", "human_pause"]
EpisodeWrapupState = Literal[
    "not_started",
    "pending",
    "running",
    "ready",
    "failed",
    "skipped",
    "legacy_unavailable",
]
EpisodeReportAttemptStatus = Literal["queued", "running", "succeeded", "failed"]


class EpisodeRecord(BaseModel):
    """The mode-neutral parent and lifecycle for one bounded episode."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    project_id: str
    mode: EpisodeMode
    control_node_id: str | None = None
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
    graph_base_head: GraphHeadRef | None = None
    root_operation_id: str | None = None
    status: EpisodeStatus
    invocation_ceiling: int = Field(ge=1)
    invocations_used: int = Field(default=0, ge=0)
    authorized_by: AuthorizedHuman | None = None
    stop_requested_at: str | None = None
    stop_settled_at: str | None = None
    ending: EpisodeEnding | None = None
    ending_diagnostic: str | None = None
    wrapup_state: EpisodeWrapupState = "not_started"
    wrapup_error: str | None = None
    report_attempts_used: int = Field(default=0, ge=0, le=3)
    created_at: str
    updated_at: str
    ended_at: str | None = None

    @model_validator(mode="after")
    def lifecycle_is_coherent(self) -> EpisodeRecord:
        if self.invocations_used > self.invocation_ceiling:
            raise ValueError("episode invocations used exceed the authorized ceiling")
        if self.ending == "stopped" and self.wrapup_state != "skipped":
            raise ValueError("a stopped episode must skip report generation")
        if self.wrapup_state == "skipped" and self.ending != "stopped":
            raise ValueError("only a stopped episode may skip report generation")
        if self.mode == "experiment_loop" and not self.control_node_id:
            raise ValueError("an Experiment-loop episode requires its control node")
        if self.mode == "auto_research" and self.control_node_id is not None:
            raise ValueError("an Auto-research episode cannot carry an Experiment control node")
        if self.graph_target.kind == "main" and self.graph_base_head is not None:
            raise ValueError("a main-target episode cannot carry a branch base head")
        if self.graph_target.kind == "branch":
            if self.graph_base_head is None or self.graph_base_head.target.kind != "main":
                raise ValueError("a branch-target episode requires its immutable main base head")
            if self.mode == "auto_research" and self.graph_target.branch_id != self.episode_id:
                raise ValueError("an Auto-research episode must own its same-id graph branch")
        if self.wrapup_state in {"ready", "failed"} and self.ending is None:
            raise ValueError("a terminal episode wrap-up requires its semantic ending")
        return self

    @property
    def invocations_remaining(self) -> int:
        return max(0, self.invocation_ceiling - self.invocations_used)


class EpisodeBudgetMeter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_ceiling: int = Field(ge=1)
    invocations_used: int = Field(ge=0)
    invocations_remaining: int = Field(ge=0)
    observed_input_tokens: int = Field(default=0, ge=0)
    observed_generated_tokens: int = Field(default=0, ge=0)


class EpisodeInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str
    operation_id: str
    invocation_number: int = Field(ge=1)
    created_at: str


class EpisodeReportAttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    episode_id: str
    attempt_number: int = Field(ge=1, le=3)
    allocation_operation_id: str
    status: EpisodeReportAttemptStatus
    error: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None


class EpisodeReportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    episode_id: str
    attempt_id: str
    allocation_operation_id: str
    ending: EpisodeEnding
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    html: str
    created_at: str

    @field_validator("html")
    @classmethod
    def html_is_a_bounded_utf8_artifact(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("episode report HTML contains NUL bytes")
        if len(value.encode("utf-8")) > CHAT_ARTIFACT_MAX_FILE_BYTES:
            raise ValueError("episode report HTML exceeds the artifact size limit")
        return value

    @model_validator(mode="after")
    def digest_matches_html(self) -> EpisodeReportRecord:
        if hashlib.sha256(self.html.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("episode report HTML does not match its digest")
        return self


class EpisodeWrapupRecord(BaseModel):
    """The immutable restart fence for one episode's hidden report allocation."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    ending: EpisodeEnding | None
    partial: bool
    concluding_operation_id: str | None = None
    allocation_operation_id: str | None = None
    provider: str | None = None
    run_on: str | None = None
    execution_host: str | None = None
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    skill_id: str | None = None
    skill_version: str | None = None
    output_name: str | None = None
    output_path: str | None = None
    receipt_json: str
    receipt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: EpisodeWrapupState
    diagnostic: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None

    @model_validator(mode="after")
    def restart_fence_is_coherent(self) -> EpisodeWrapupRecord:
        try:
            receipt = json.loads(self.receipt_json)
        except json.JSONDecodeError as exc:
            raise ValueError("episode wrap-up receipt is invalid JSON") from exc
        if not isinstance(receipt, dict):
            raise ValueError("episode wrap-up receipt must be a JSON object")
        compact = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        if compact != self.receipt_json:
            raise ValueError("episode wrap-up receipt must use canonical compact JSON")
        if hashlib.sha256(self.receipt_json.encode("utf-8")).hexdigest() != self.receipt_sha256:
            raise ValueError("episode wrap-up receipt does not match its digest")
        if self.state == "skipped" and self.ending != "stopped":
            raise ValueError("only a stopped episode may skip its wrap-up")
        if self.ending == "stopped" and self.state != "skipped":
            raise ValueError("a stopped episode must skip its wrap-up")
        if self.state != "legacy_unavailable" and self.ending is None:
            raise ValueError("a new episode wrap-up requires its semantic ending")
        if self.output_name is not None:
            _plain_html_name(self.output_name, label="episode report output name")
        return self


class EpisodeInvocationCeilingReached(ValueError):
    pass


class EpisodeNotRunning(ValueError):
    pass


class EpisodeReportAttemptLimitReached(ValueError):
    pass


class EpisodeReportConflict(ValueError):
    pass


class AutoResearchStateRecord(BaseModel):
    """Mode-specific state attached to one generic Auto-research episode."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    starting_instruction: str | None = Field(default=None, max_length=16_000)
    created_at: str
    updated_at: str


class AutoResearchInvocationRecord(BaseModel):
    """One Auto-research task and the operational allocation it belongs to."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    operation_id: str
    allocation_operation_id: str
    role: AutoResearchRole
    actor_operation_id: str
    control_node_id: str | None = None
    created_at: str


class AutoResearchChildWorkRecord(BaseModel):
    """One ordinary Work actor admitted and routed by an Auto-research parent."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str
    episode_id: str
    project_id: str
    control_node_id: str
    root_operation_id: str
    current_operation_id: str
    admitted_by_operation_id: str
    instruction: str = Field(min_length=1, max_length=16_000)
    instruction_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stop_requested_at: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def instruction_matches_digest(self) -> AutoResearchChildWorkRecord:
        if not self.instruction.strip():
            raise ValueError("an Auto-research child Work instruction must not be blank")
        if hashlib.sha256(self.instruction.encode("utf-8")).hexdigest() != self.instruction_sha256:
            raise ValueError("the child Work instruction does not match its digest")
        return self


class AutoResearchChildExperimentRecord(BaseModel):
    """Parent routing and immutable launch intent for one child Experiment episode."""

    model_config = ConfigDict(extra="forbid")

    child_episode_id: str
    auto_research_episode_id: str
    project_id: str
    control_node_id: str
    state: AutoResearchChildExperimentState
    replaces_episode_id: str | None = None
    request: dict[str, object]
    goal_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    parent_operation_id: str
    terminal_diagnostic: str | None = None
    created_at: str
    updated_at: str


class AutoResearchExperimentAllowance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=5)
    used: int = Field(ge=0)
    remaining: int = Field(ge=0)

    @model_validator(mode="after")
    def accounting_is_coherent(self) -> AutoResearchExperimentAllowance:
        if self.used > self.total or self.remaining != self.total - self.used:
            raise ValueError("the child Experiment allowance accounting is inconsistent")
        return self


class AutoResearchChildAdmissionRecord(BaseModel):
    """A durable command admission awaiting or naming its reflected child route."""

    model_config = ConfigDict(extra="forbid")

    admission_id: str
    episode_id: str
    project_id: str
    child_kind: Literal["work", "experiment"]
    child_id: str
    state: AutoResearchChildAdmissionState
    created_at: str
    updated_at: str


class AutoResearchLifecycleNoticeRecord(BaseModel):
    """An RCP-authored lifecycle fact, deliberately separate from agent mail."""

    model_config = ConfigDict(extra="forbid")

    notice_id: str
    episode_id: str
    source_kind: str
    source_id: str
    source_event: str
    source_attempt: int = Field(default=1, ge=1)
    state: AutoResearchLifecycleNoticeState = "pending"
    payload: dict[str, object]
    created_at: str
    delivered_at: str | None = None
    delivery_operation_id: str | None = None
    acknowledged_at: str | None = None
    acknowledged_by: str | None = None

    @model_validator(mode="after")
    def delivery_state_is_coherent(self) -> AutoResearchLifecycleNoticeRecord:
        if (self.delivered_at is None) != (self.delivery_operation_id is None):
            raise ValueError("a lifecycle delivery requires both its time and operation")
        if (self.acknowledged_at is None) != (self.acknowledged_by is None):
            raise ValueError("a lifecycle acknowledgment requires both its time and actor")
        expected = (
            "acknowledged"
            if self.acknowledged_at is not None
            else "delivered"
            if self.delivered_at is not None
            else "pending"
        )
        if self.state != expected:
            raise ValueError("the lifecycle notice state does not match its timestamps")
        return self


class AutoResearchInboxReceiptRecord(BaseModel):
    """The exact lifecycle-notice snapshot acknowledged by one keyed inbox effect."""

    model_config = ConfigDict(extra="forbid")

    effect_id: str
    episode_id: str
    mode: AutoResearchInboxReceiptMode
    notice_ids: list[str]
    count: int = Field(ge=0)
    notices: list[AutoResearchLifecycleNoticeRecord] = Field(default_factory=list)
    acknowledged_by: str
    created_at: str

    @model_validator(mode="after")
    def result_matches_mode(self) -> AutoResearchInboxReceiptRecord:
        if self.count != len(self.notice_ids) or len(set(self.notice_ids)) != self.count:
            raise ValueError("an inbox receipt count must match its unique notice ids")
        if self.mode == "clear" and self.notices:
            raise ValueError("a clear receipt must not retain notice bodies")
        if self.mode == "harvest" and [item.notice_id for item in self.notices] != self.notice_ids:
            raise ValueError("a harvest receipt body must match its notice ids in order")
        return self


class AutoResearchFinishReceiptRecord(BaseModel):
    """The complete immutable result of one keyed guarded-Finish decision."""

    model_config = ConfigDict(extra="forbid")

    effect_id: str
    episode_id: str
    actor_operation_id: str = Field(min_length=1)
    disposition: AutoResearchFinishDisposition
    blocker_count: int = Field(ge=0)
    result: dict[str, object]
    result_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str

    @model_validator(mode="after")
    def result_matches_decision(self) -> AutoResearchFinishReceiptRecord:
        compact = json.dumps(
            self.result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if hashlib.sha256(compact.encode("utf-8")).hexdigest() != self.result_sha256:
            raise ValueError("the guarded-Finish result does not match its digest")
        if self.result.get("episode_id") != self.episode_id:
            raise ValueError("the guarded-Finish result belongs to another episode")
        blockers = self.result.get("blockers")
        if self.disposition == "blocked":
            if set(self.result) != {"episode_id", "blockers"} or not isinstance(blockers, list):
                raise ValueError("a blocked Finish receipt requires its complete blocker array")
            parsed = [AutoResearchFinishBlocker.model_validate(item) for item in blockers]
            if self.blocker_count == 0 or len(parsed) != self.blocker_count:
                raise ValueError("a blocked Finish receipt count must match its blockers")
        elif (
            set(self.result) != {"episode_id", "status", "ending"}
            or self.blocker_count != 0
            or blockers is not None
            or self.result.get("ending") != "completed"
        ):
            raise ValueError("a completed Finish receipt requires its fenced episode result")
        return self


class AutoResearchCommandFileRecord(BaseModel):
    """Immutable text snapshotted before a keyed staged command takes effect."""

    model_config = ConfigDict(extra="forbid")

    command_id: str
    episode_id: str
    operation_id: str
    kind: AutoResearchCommandFileKind
    filename: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str
    created_at: str

    @field_validator("filename")
    @classmethod
    def filename_is_direct(cls, value: str) -> str:
        if not value or value in {".", ".."} or "\x00" in value or Path(value).name != value:
            raise ValueError("a staged command snapshot requires one direct filename")
        return value

    @model_validator(mode="after")
    def content_matches_digest(self) -> AutoResearchCommandFileRecord:
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("the staged command snapshot does not match its digest")
        return self


class AutoResearchApplyResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply_id: str
    episode_id: str
    operation_id: str
    patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: dict[str, object]
    created_at: str


class AutoResearchFinishBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: AutoResearchFinishBlockerKind
    blocker_id: str
    state: str
    action: str


class AutoResearchExperimentAllowanceReached(ValueError):
    def __init__(self, allowance: AutoResearchExperimentAllowance) -> None:
        self.allowance = allowance
        super().__init__(
            "the Auto-research child Experiment allowance is exhausted "
            f"({allowance.used}/{allowance.total})"
        )


class AutoResearchRecoveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_id: str
    episode_id: str
    operation_id: str
    failure_kind: str
    retry_mode: AutoResearchRecoveryMode
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(ge=1)
    status: AutoResearchRecoveryStatus
    next_attempt_at: str | None = None
    diagnostic: str
    admitted_operation_id: str | None = None
    created_at: str
    updated_at: str


class AutoResearchMessageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    episode_id: str
    sender_role: AutoResearchMessageRole
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
            raise ValueError("Auto-research message body must not be blank")
        return stripped

    @model_validator(mode="after")
    def only_human_messages_carry_human_identity(self) -> AutoResearchMessageRecord:
        if self.sender_role != "human" and self.authorized_by is not None:
            raise ValueError("an agent Auto-research message cannot claim a human sender snapshot")
        return self


class AutoResearchActorBinding(BaseModel):
    """Canonical actor identity plus the newest task carrying its native session."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    actor_operation_id: str
    role: AutoResearchRole
    control_node_id: str | None = None
    current_operation_id: str
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None


class AutoResearchActorBusy(ValueError):
    """One Auto-research actor already has an unresolved leaf."""

    def __init__(self, actor_operation_id: str, operation_id: str) -> None:
        self.actor_operation_id = actor_operation_id
        self.operation_id = operation_id
        super().__init__(
            f"Auto-research actor {actor_operation_id} already has unresolved task {operation_id}."
        )


class AgentCommandInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    episode_id: str | None = None
    operation_id: str
    verb: str
    idempotency_key: str | None = None
    started_at: str
    start_payload: dict[str, object]
    exited_at: str | None = None
    status: Literal["ok", "invalid", "unavailable"] | None = None
    exit_payload: dict[str, object] | None = None


class ExperimentEpisodeRecord(BaseModel):
    """Joined projection of an Experiment episode parent and its mode state.

    The binding is what an automatic watcher wake resumes. It is committed only
    by a mechanically successful joint handoff, so a failed first invocation
    never leaves a session an automatic wake would try to continue. A graph-only
    rejection is still a truthful accepted operational handoff. Project,
    control-node, and Stop fields are read from ``episodes``; they are not
    duplicated in the mode-specific child row.
    """

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    project_id: str
    control_node_id: str
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
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
    # A parent row still occupying this Experiment, which is what admission
    # refuses a second episode against. Wider than `active`: a settled turn that
    # armed nothing leaves the parent live with no work left to wake it.
    episode_live: bool = False
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


class ExperimentEpisodeProjectionSnapshot(BaseModel):
    """One transactionally coherent Experiment episode read model input."""

    model_config = ConfigDict(extra="forbid")

    episode: EpisodeRecord
    tasks: list[AgentTaskRecord] = Field(default_factory=list)
    budget: EpisodeBudgetMeter
    report: EpisodeReportRecord | None = None


class ExperimentControlProjectionSnapshot(BaseModel):
    """Runtime and episode inputs observed in one SQLite read transaction."""

    model_config = ConfigDict(extra="forbid")

    runtime: ExperimentLoopRuntime
    episode: ExperimentEpisodeProjectionSnapshot | None = None
    latest_report_episode_id: str | None = None


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
    origin_task_kind: Literal["node_chat", "project_chat", "auto_research"]
    chat_id: str
    node_id: str | None = None
    episode_id: str | None = None
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
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
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
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
    "project_members",
    "paper_drafts",
    "writing_sessions",
    "chat_session_contexts",
    "result_views",
    "graph_runs",
    "episodes",
    "agent_usage",
    "watchers",
    "graph_watcher_reconciliation",
    "auto_research_child_work",
    "auto_research_child_experiments",
    "auto_research_child_admissions",
)


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
    return _required_timestamp(record.expires_at) > _result_view_reference_time(as_of)


def _result_view_reference_time(as_of: datetime | None) -> datetime:
    current = as_of or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("result view visibility time must include a timezone")
    return current.astimezone(UTC)


def _validated_result_view_html(record: ResultViewRecord, data: bytes) -> str:
    if not isinstance(data, bytes):
        raise TypeError("result view HTML must be bytes")
    if len(data) > CHAT_ARTIFACT_MAX_FILE_BYTES:
        raise ValueError("result view HTML exceeds its byte limit")
    if len(data) != record.size_bytes:
        raise ValueError("result view HTML size does not match its metadata")
    if hashlib.sha256(data).hexdigest() != record.content_sha256:
        raise ValueError("result view HTML digest does not match its metadata")
    if validate_artifact_bytes(record.source_name, data) != "text/html":
        raise ValueError("result view must be HTML")
    return data.decode("utf-8")


def _result_view_html_bytes(record: ResultViewRecord, html: object) -> bytes:
    if not isinstance(html, str):
        raise ValueError("stored result view HTML is invalid")
    data = html.encode("utf-8")
    _validated_result_view_html(record, data)
    return data


__all__ = [
    "ACTIVE_AGENT_TASK_STATUSES",
    "AGENT_TASK_TRANSITIONS",
    "AgentCommandInvocationRecord",
    "AgentTaskContractRecord",
    "AgentTaskEventRecord",
    "AgentTaskKind",
    "AgentTaskReceiptRecord",
    "AgentTaskReceiptTier",
    "AgentTaskRecord",
    "AgentTaskStatus",
    "AgentUsageCell",
    "AgentUsageCountReason",
    "AgentUsageMetric",
    "AgentUsageRecord",
    "AgentUsageSnapshot",
    "AutoResearchActorBinding",
    "AutoResearchActorBusy",
    "AutoResearchApplyResultRecord",
    "AutoResearchChildAdmissionRecord",
    "AutoResearchChildAdmissionState",
    "AutoResearchChildExperimentRecord",
    "AutoResearchChildExperimentState",
    "AutoResearchChildWorkRecord",
    "AutoResearchCommandFileKind",
    "AutoResearchCommandFileRecord",
    "AutoResearchExperimentAllowance",
    "AutoResearchExperimentAllowanceReached",
    "AutoResearchFinishBlocker",
    "AutoResearchFinishBlockerKind",
    "AutoResearchFinishDisposition",
    "AutoResearchFinishReceiptRecord",
    "AutoResearchInboxReceiptMode",
    "AutoResearchInboxReceiptRecord",
    "AutoResearchInvocationRecord",
    "AutoResearchLifecycleNoticeRecord",
    "AutoResearchLifecycleNoticeState",
    "AutoResearchMessageRecord",
    "AutoResearchMessageRole",
    "AutoResearchRecoveryMode",
    "AutoResearchRecoveryRecord",
    "AutoResearchRecoveryStatus",
    "AutoResearchRole",
    "AutoResearchStateRecord",
    "ChatSessionContextRecord",
    "ExperimentEpisodeRecord",
    "ExperimentEpisodeProjectionSnapshot",
    "ExperimentControlProjectionSnapshot",
    "ExperimentLoopRuntime",
    "EpisodeBudgetMeter",
    "EpisodeEnding",
    "EpisodeInvocationCeilingReached",
    "EpisodeInvocationRecord",
    "EpisodeMode",
    "EpisodeNotRunning",
    "EpisodeRecord",
    "EpisodeReportAttemptLimitReached",
    "EpisodeReportAttemptRecord",
    "EpisodeReportAttemptStatus",
    "EpisodeReportConflict",
    "EpisodeReportRecord",
    "EpisodeStatus",
    "EpisodeWrapupState",
    "EpisodeWrapupRecord",
    "ExperimentWatcherResourceRecord",
    "GraphCondition",
    "GraphWatcherRecord",
    "NodeStatusGraphCondition",
    "ProjectInvitationRecord",
    "ProjectMemberRecord",
    "ProjectRecord",
    "ProjectStageRecord",
    "ProposalResolvedGraphCondition",
    "ProviderSkillInventoryRecord",
    "ResultViewConflict",
    "ResultViewRecord",
    "SPACE_NAME_MAX_LENGTH",
    "SpaceKind",
    "SpaceUserKind",
    "SpaceUserRecord",
    "StoredWatcherRecord",
    "TeamAuthenticationError",
    "TeamInvitationRecord",
    "WatcherClaimConflict",
    "WatcherContinuation",
    "WatcherDeliveryRecord",
    "WatcherRecord",
    "WatcherStatus",
    "WatcherStopRequest",
    "normalize_space_name",
    "watcher_next_check_at",
]
