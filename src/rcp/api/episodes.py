from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.core.models import AuthorizedHuman
from rcp.storage import (
    AgentTaskRecord,
    AgentTaskStatus,
    AppStore,
    AutoResearchRecoveryMode,
    AutoResearchRecoveryStatus,
    EpisodeBudgetMeter,
    EpisodeEnding,
    EpisodeMode,
    EpisodeRecord,
    EpisodeStatus,
    EpisodeWrapupState,
)

OperationalEpisodeTaskKind = Literal[
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "auto_research",
]

_STOPPABLE_EPISODE_STATUSES: frozenset[EpisodeStatus] = frozenset({"queued", "running"})
_TERMINAL_WRAPUP_STATES: frozenset[EpisodeWrapupState] = frozenset(
    {"ready", "failed", "legacy_unavailable"}
)
_EPISODE_TEXT_MAX_LENGTH = 16_000


class StartEpisodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["auto_research"]
    invocation_ceiling: int = Field(ge=1)
    starting_instruction: str | None = Field(
        default=None,
        max_length=_EPISODE_TEXT_MAX_LENGTH,
    )

    @field_validator("starting_instruction", mode="before")
    @classmethod
    def trim_starting_instruction(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class ReauthorizeEpisodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    invocation_ceiling: int = Field(ge=1)


class EpisodeMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    body: str = Field(min_length=1, max_length=_EPISODE_TEXT_MAX_LENGTH)

    @field_validator("body", mode="before")
    @classmethod
    def trim_nonblank_body(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("episode message body must not be blank")
            return stripped
        return value


class EpisodeTaskResponse(BaseModel):
    """The public, operational-only projection of an episode task."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: str
    project_id: str
    kind: OperationalEpisodeTaskKind
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
    attempt: int
    parent_operation_id: str | None = None
    episode_id: str | None = None
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    estimate_seconds: float
    estimate_samples: int
    phase: str
    last_activity_at: str | None = None
    authorized_by: AuthorizedHuman | None = None
    elapsed_seconds: float
    progress: float
    can_pause: bool
    can_resume: bool
    can_retry: bool


class EpisodeReportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    report_id: str
    ending: EpisodeEnding
    created_at: str


class AutoResearchRecoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    purpose: Literal["task"] = "task"
    status: AutoResearchRecoveryStatus
    retry_mode: AutoResearchRecoveryMode
    operation_id: str | None
    attempts: int
    max_attempts: int
    next_attempt_at: str | None


class EpisodeResponse(BaseModel):
    """One public episode parent, without hidden report-attempt state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    episode_id: str
    project_id: str
    mode: EpisodeMode
    control_node_id: str | None
    root_operation_id: str | None
    current_operation_id: str | None
    current_orchestrator_task_id: str | None
    current_control_task_id: str | None
    recovery: AutoResearchRecoverySummary | None
    status: EpisodeStatus
    starting_instruction: str | None
    budget: EpisodeBudgetMeter
    authorized_by: AuthorizedHuman | None
    stop_requested_at: str | None
    ending: EpisodeEnding | None
    ending_diagnostic: str | None
    wrapup_state: EpisodeWrapupState
    wrapup_error: str | None
    created_at: str
    updated_at: str
    ended_at: str | None
    tasks: list[EpisodeTaskResponse]
    report: EpisodeReportSummary | None
    can_stop: bool
    can_reauthorize: bool


def episode_for_project(
    store: AppStore,
    project_id: str,
    episode_id: str,
) -> EpisodeRecord:
    """Load one episode without allowing a cross-project identifier lookup."""

    episode = store.episode(episode_id)
    if episode is None or episode.project_id != project_id:
        raise KeyError(episode_id)
    return episode


def serialize_episode(
    store: AppStore,
    project_id: str,
    episode: EpisodeRecord,
) -> EpisodeResponse:
    """Serialize one project-owned parent from its current durable ledgers."""

    if episode.project_id != project_id:
        raise KeyError(episode.episode_id)

    task_records = _operational_tasks(store, episode)
    recovery_controls_allowed = episode.ending is None and episode.stop_requested_at is None
    tasks = [
        _serialize_task(task, recovery_controls_allowed=recovery_controls_allowed)
        for task in task_records
    ]
    current_operation_id = task_records[-1].operation_id if task_records else None

    starting_instruction: str | None = None
    current_orchestrator_task_id: str | None = None
    current_control_task_id: str | None = current_operation_id
    recovery: AutoResearchRecoverySummary | None = None
    if episode.mode == "auto_research":
        (
            starting_instruction,
            current_orchestrator_task_id,
            current_control_task_id,
            recovery,
        ) = _auto_research_projection(store, episode, task_records)

    stored_report = (
        None if episode.ending == "stopped" else store.episode_report(episode.episode_id)
    )
    report = (
        EpisodeReportSummary(
            report_id=stored_report.report_id,
            ending=stored_report.ending,
            created_at=stored_report.created_at,
        )
        if stored_report is not None
        else None
    )
    stopped = episode.ending == "stopped"
    return EpisodeResponse(
        episode_id=episode.episode_id,
        project_id=episode.project_id,
        mode=episode.mode,
        control_node_id=episode.control_node_id,
        root_operation_id=episode.root_operation_id,
        current_operation_id=current_operation_id,
        current_orchestrator_task_id=current_orchestrator_task_id,
        current_control_task_id=current_control_task_id,
        recovery=recovery,
        status=episode.status,
        starting_instruction=starting_instruction,
        budget=store.episode_budget_meter(episode.episode_id),
        authorized_by=episode.authorized_by,
        stop_requested_at=episode.stop_requested_at,
        ending=episode.ending,
        ending_diagnostic=episode.ending_diagnostic,
        wrapup_state=episode.wrapup_state,
        wrapup_error=None if stopped else episode.wrapup_error,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
        ended_at=episode.ended_at,
        tasks=tasks,
        report=report,
        can_stop=(
            episode.status in _STOPPABLE_EPISODE_STATUSES
            and episode.stop_requested_at is None
            and episode.ending is None
        ),
        can_reauthorize=(
            episode.mode == "auto_research"
            and episode.status == "needs_action"
            and episode.ending == "exhausted"
            and episode.wrapup_state in _TERMINAL_WRAPUP_STATES
        ),
    )


def serialize_episodes(
    store: AppStore,
    project_id: str,
    *,
    mode: EpisodeMode | None = None,
    limit: int = 50,
) -> list[EpisodeResponse]:
    """Serialize the ordered project list, optionally limited to one episode mode."""

    bounded_limit = max(1, min(limit, 500))
    episodes = store.episodes(
        project_id,
        limit=500 if mode is not None else bounded_limit,
    )
    return [
        serialize_episode(store, project_id, episode)
        for episode in episodes
        if mode is None or episode.mode == mode
    ][:bounded_limit]


def _operational_tasks(store: AppStore, episode: EpisodeRecord) -> list[AgentTaskRecord]:
    tasks: list[AgentTaskRecord] = []
    for task in store.episode_tasks(episode.episode_id):
        if task.episode_id != episode.episode_id or task.project_id != episode.project_id:
            raise ValueError("episode task lineage crosses its parent boundary")
        if not task.visible or task.kind == "episode_report":
            continue
        tasks.append(task)
    return tasks


def _serialize_task(
    task: AgentTaskRecord,
    *,
    recovery_controls_allowed: bool,
) -> EpisodeTaskResponse:
    public_fields = EpisodeTaskResponse.model_fields.keys()
    values = task.model_dump(include=public_fields)
    if not recovery_controls_allowed:
        values.update(can_pause=False, can_resume=False, can_retry=False)
    return EpisodeTaskResponse.model_validate(values)


def _auto_research_projection(
    store: AppStore,
    episode: EpisodeRecord,
    tasks: list[AgentTaskRecord],
) -> tuple[
    str | None,
    str | None,
    str | None,
    AutoResearchRecoverySummary | None,
]:
    state = store.auto_research_state(episode.episode_id)
    starting_instruction = state.starting_instruction if state is not None else None
    current_orchestrator_task_id: str | None = None
    if episode.root_operation_id is not None:
        try:
            binding = store.auto_research_actor_binding(episode.root_operation_id)
        except (KeyError, RuntimeError, ValueError):
            binding = None
        if binding is not None and binding.episode_id == episode.episode_id:
            current_orchestrator_task_id = binding.current_operation_id

    tasks_by_id = {task.operation_id: task for task in tasks}
    current_control_task_id = _auto_research_control_task_id(
        store,
        episode,
        tasks,
        tasks_by_id,
        current_orchestrator_task_id,
    )
    control_recovery = (
        store.auto_research_control_recovery(episode.episode_id, current_control_task_id)
        if current_control_task_id is not None
        else None
    )
    recovery = (
        AutoResearchRecoverySummary(
            status=control_recovery.status,
            retry_mode=control_recovery.retry_mode,
            operation_id=control_recovery.operation_id,
            attempts=control_recovery.attempts,
            max_attempts=control_recovery.max_attempts,
            next_attempt_at=control_recovery.next_attempt_at,
        )
        if control_recovery is not None
        else None
    )
    return (
        starting_instruction,
        current_orchestrator_task_id,
        current_control_task_id,
        recovery,
    )


def _auto_research_control_task_id(
    store: AppStore,
    episode: EpisodeRecord,
    tasks: list[AgentTaskRecord],
    tasks_by_id: dict[str, AgentTaskRecord],
    current_orchestrator_task_id: str | None,
) -> str | None:
    if episode.status not in {"stopping", "wrapping_up"}:
        return current_orchestrator_task_id

    recovered_parent_ids = {
        task.parent_operation_id
        for task in tasks
        if task.parent_operation_id is not None
        and (parent := tasks_by_id.get(task.parent_operation_id)) is not None
        and task.attempt == parent.attempt + 1
        and (task.request.get("actor_operation_id") or task.operation_id)
        == (parent.request.get("actor_operation_id") or parent.operation_id)
    }
    current_orchestrator = tasks_by_id.get(current_orchestrator_task_id or "")
    orchestrator_recovery = (
        store.auto_research_control_recovery(
            episode.episode_id,
            current_orchestrator.operation_id,
        )
        if current_orchestrator is not None
        and current_orchestrator.status in {"failed", "interrupted"}
        else None
    )
    if (
        current_orchestrator is not None
        and current_orchestrator.operation_id not in recovered_parent_ids
        and (
            (
                current_orchestrator.status == "paused"
                and (current_orchestrator.can_resume or current_orchestrator.can_retry)
            )
            or (
                current_orchestrator.status in {"failed", "interrupted"}
                and orchestrator_recovery is not None
                and orchestrator_recovery.operation_id == current_orchestrator.operation_id
                and orchestrator_recovery.status != "admitted"
            )
        )
        and store.auto_research_invocation_role(current_orchestrator.operation_id) == "orchestrator"
    ):
        return current_orchestrator.operation_id

    paused_workers = [
        task
        for task in tasks
        if task.status == "paused"
        and task.operation_id not in recovered_parent_ids
        and (task.can_resume or task.can_retry)
        and store.auto_research_invocation_role(task.operation_id) == "worker"
    ]
    if paused_workers:
        return max(
            paused_workers,
            key=lambda task: (task.created_at, task.operation_id),
        ).operation_id
    return current_orchestrator_task_id
