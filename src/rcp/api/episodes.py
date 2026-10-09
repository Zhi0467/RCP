from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.agents.provider_accounts import account_login_refusal
from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.api.dependencies import require_registered_project
from rcp.core.models import AuthorizedHuman, EpisodeIsolationState, GraphBranchSummary
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.episode_health import (
    EpisodeBlockedReason,
    EpisodeHealth,
    EpisodeHealthInput,
    EpisodeRecommendationKind,
    EpisodeRunSection,
    EpisodeTaskControlKind,
    _auto_research_control_task_id,
    _episode_task_controls,
    _EpisodeProjectionParent,
    episode_has_open_questions,
    operational_episode_tasks,
    project_episode_health,
)
from rcp.loop_status import EpisodeLoopMetadata, episode_loop_metadata
from rcp.projects import ProjectCatalog
from rcp.providers.browser_grant import BrowserTurnStatus
from rcp.storage import (
    AgentFailureKind,
    AgentTaskRecord,
    AgentTaskStatus,
    AppStore,
    AutoResearchRecoveryMode,
    AutoResearchRecoveryStatus,
    AutoResearchSpaceRunProjectionSnapshot,
    AutoResearchSpaceRunTaskState,
    EpisodeArchiveState,
    EpisodeBudgetMeter,
    EpisodeEnding,
    EpisodeMode,
    EpisodeRecord,
    EpisodeReportRecord,
    EpisodeStatus,
    EpisodeWrapupState,
    ExperimentEpisodeProjectionSnapshot,
)
from rcp.storage.episodes import _LIVE_EPISODE_STATUSES

OperationalEpisodeTaskKind = Literal[
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
    "auto_research",
    "branch_merge",
    "artifact_edit",
]

BranchSummaryResolver = Callable[[EpisodeRecord], GraphBranchSummary]
BranchSummariesResolver = Callable[[list[EpisodeRecord]], dict[str, GraphBranchSummary]]

_STOPPABLE_EPISODE_STATUSES: frozenset[EpisodeStatus] = frozenset({"queued", "running"})
_TERMINAL_EPISODE_STATUSES: frozenset[EpisodeStatus] = frozenset(
    {"completed", "failed", "needs_action", "stopped"}
)
_EPISODE_TEXT_MAX_LENGTH = 16_000


@dataclass(frozen=True)
class _SpaceRunRecoveryProjection:
    operation_id: str
    status: AutoResearchRecoveryStatus
    failure_kind: str | None = None


class StartEpisodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["auto_research"]
    browser_requested: bool = False
    code_worktree: bool | None = None
    graph_isolation: Literal[True] = True
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


class ContinueEpisodeBody(BaseModel):
    """Add turns to an ended episode; the request id makes a repeat return the same one."""

    model_config = ConfigDict(extra="forbid", strict=True)

    invocation_ceiling: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=120)

    @field_validator("request_id")
    @classmethod
    def request_id_is_a_uuid(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("the continuation request id must be a UUID") from exc
        return value


class EpisodeMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Null is ordinary-mail-only consent; omission uses the continuation default.
    invocation_ceiling: int | None = Field(default=3, ge=1)
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=120)

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
    """A public episode turn or artifact edit, with operational controls when applicable."""

    model_config = ConfigDict(extra="forbid", strict=True)

    operation_id: str
    role: Literal["orchestrator", "worker", "wake"]
    depth: int = Field(ge=0, le=4)
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
    failure_kind: AgentFailureKind | None
    browser_status: BrowserTurnStatus | None = None
    degradation: str | None = None
    applied_revision: int | None = None
    result: dict[str, object] | None = None
    attempt: int
    parent_operation_id: str | None = None
    episode_id: str | None = None
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    graph_target: GraphTargetRef
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
    active: bool
    queued: bool
    pausing: bool
    awaiting_human: bool
    paused: bool
    failed: bool
    settled: bool
    finished: bool
    status_label: str


class EpisodeReportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    report_id: str
    ending: EpisodeEnding
    created_at: str


class EpisodeChainMember(BaseModel):
    """One member of a continuation chain: its turns, how it ended, and its report."""

    model_config = ConfigDict(extra="forbid", strict=True)

    episode_id: str
    created_at: str
    status: EpisodeStatus
    ending: EpisodeEnding | None
    invocation_ceiling: int
    invocations_used: int
    report: EpisodeReportSummary | None


class AutoResearchRecoverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    purpose: Literal["task"] = "task"
    status: AutoResearchRecoveryStatus
    retry_mode: AutoResearchRecoveryMode
    # Two different failures both block, and only this says which, so a
    # surface can tell a dead login from a spent usage allowance.
    failure_kind: str
    operation_id: str | None
    attempts: int
    max_attempts: int
    next_attempt_at: str | None


class EpisodeResponse(EpisodeLoopMetadata):
    """One public episode parent, without hidden report-attempt state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    episode_id: str
    project_id: str
    mode: EpisodeMode
    browser_requested: bool = False
    code_worktree: bool
    graph_isolation: bool
    isolation_owner_episode_id: str | None
    isolation_state: EpisodeIsolationState | None = None
    control_node_id: str | None
    graph_target: GraphTargetRef
    graph_base_head: GraphHeadRef | None
    graph_branch: GraphBranchSummary | None
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
    archived: bool = False
    can_archive: bool = False
    tasks: list[EpisodeTaskResponse]
    report: EpisodeReportSummary | None
    can_stop: bool
    # A continuation adds turns to an ended episode on the same branch and
    # session. The chain is published so a card can show it as one run.
    continues_episode_id: str | None
    continued_by_episode_id: str | None
    can_continue: bool
    chain: list[EpisodeChainMember]
    can_message: bool
    message_refusal: dict[str, str] | None = None
    message_requires_continuation: bool = False
    # The lifecycle state this parent is in, what a human should do next, and the
    # recovery control that is actually available. All three are decided from
    # backend lifecycle alone, so the surfaces consume them rather than each
    # reaching its own conclusion from `status`, `ending`, and task rows.
    health: EpisodeHealth
    blocked_reason: EpisodeBlockedReason | None
    recommendation: EpisodeRecommendationKind
    task_control: EpisodeTaskControlKind | None
    run_section: EpisodeRunSection
    # Whether this parent still occupies its Experiment, which is what admission
    # refuses a second episode against. Published so no client reconstructs the
    # storage status list to answer it.
    live: bool


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


def _episode_for_http(
    store: AppStore,
    catalog: ProjectCatalog,
    project_id: str,
    episode_id: str,
) -> EpisodeRecord:
    require_registered_project(catalog, project_id)
    try:
        return episode_for_project(store, project_id, episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Episode not found") from exc


def serialize_episode(
    store: AppStore,
    project_id: str,
    episode: EpisodeRecord,
    *,
    branch_summary: BranchSummaryResolver | None = None,
    projection_snapshot: ExperimentEpisodeProjectionSnapshot | None = None,
    include_graph_branch: bool = True,
    archive_state: EpisodeArchiveState | None = None,
) -> EpisodeResponse:
    """Serialize one project-owned parent from its current durable ledgers."""

    if episode.project_id != project_id:
        raise KeyError(episode.episode_id)
    if archive_state is None:
        archive_state = store.episode_archive_states(project_id).get(episode.episode_id)
    if projection_snapshot is not None and (
        episode.mode != "experiment_loop" or projection_snapshot.episode != episode
    ):
        raise ValueError("Experiment episode projection does not match its durable parent.")
    has_graph_branch = episode.graph_target.kind == "branch"
    owns_graph_branch = has_graph_branch and episode.graph_target.branch_id == episode.episode_id
    if owns_graph_branch and include_graph_branch and branch_summary is None:
        raise ValueError("a branch-target episode requires its strict graph branch summary")

    task_records = (
        projection_snapshot.tasks
        if projection_snapshot is not None
        else operational_episode_tasks(store, episode)
    )
    task_metadata = _episode_task_metadata(store, episode, task_records)
    # An episode turn is a provider call like any other, so the note about a
    # setting the provider ignored belongs on its row too.
    degradations = store.agent_task_degradations([task.operation_id for task in task_records])
    tasks = [
        _serialize_task(
            task,
            store=store,
            episode=episode,
            role=task_metadata[task.operation_id][0],
            depth=task_metadata[task.operation_id][1],
            degradation=degradations.get(task.operation_id),
        )
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

    report = (
        _report_summary(projection_snapshot.report)
        if projection_snapshot is not None and episode.ending != "stopped"
        else _episode_report_summary(store, episode)
    )
    stopped = episode.ending == "stopped"
    continued_by = store.episode_continuation(episode.episode_id)
    wrapup = (
        store.episode_wrapup(episode.episode_id)
        if episode.wrapup_state in {"pending", "running"}
        else None
    )
    # The same refusal the report launch consults: a missing managed credential
    # blocks the account whatever its durable row says.
    report_login_blocked = (
        wrapup is not None
        and wrapup.provider is not None
        and account_login_refusal(
            store,
            ProviderCredentialStore.for_data_dir(store.path.parent),
            wrapup.provider,
            wrapup.execution_host or "",
        )
        is not None
    )
    health, next_step, task_control, blocked_reason = project_episode_health(
        [
            EpisodeHealthInput(
                episode.episode_id,
                episode,
                tasks,
                current_control_task_id,
                recovery,
                report_login_blocked,
                episode_has_open_questions(store, episode.project_id, episode.episode_id),
            )
        ]
    )[episode.episode_id]
    message_refusal = (
        auto_research_message_refusal(store, episode) if episode.mode == "auto_research" else None
    )
    return EpisodeResponse(
        **episode_loop_metadata(store, episode, tasks=task_records).model_dump(),
        episode_id=episode.episode_id,
        project_id=episode.project_id,
        mode=episode.mode,
        browser_requested=episode.browser_requested,
        code_worktree=episode.code_worktree,
        graph_isolation=episode.graph_isolation,
        isolation_owner_episode_id=episode.isolation_owner_episode_id,
        isolation_state=store.episode_isolation_state(
            project_id,
            episode.isolation_owner_episode_id
            or episode.graph_target.branch_id
            or episode.episode_id,
        ),
        control_node_id=episode.control_node_id,
        graph_target=episode.graph_target,
        graph_base_head=episode.graph_base_head,
        graph_branch=(
            branch_summary(episode)
            if has_graph_branch and include_graph_branch and branch_summary is not None
            else None
        ),
        root_operation_id=episode.root_operation_id,
        current_operation_id=current_operation_id,
        current_orchestrator_task_id=current_orchestrator_task_id,
        current_control_task_id=current_control_task_id,
        recovery=recovery,
        status=episode.status,
        starting_instruction=starting_instruction,
        budget=(
            projection_snapshot.budget
            if projection_snapshot is not None
            else store.episode_budget_meter(episode.episode_id)
        ),
        authorized_by=episode.authorized_by,
        stop_requested_at=episode.stop_requested_at,
        ending=episode.ending,
        ending_diagnostic=None if stopped else episode.ending_diagnostic,
        wrapup_state=episode.wrapup_state,
        wrapup_error=None if stopped else episode.wrapup_error,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
        ended_at=episode.ended_at,
        archived=archive_state.archived if archive_state is not None else False,
        can_archive=archive_state.can_archive if archive_state is not None else False,
        tasks=tasks
        + [
            _serialize_task(
                task,
                store=store,
                episode=episode,
                role="orchestrator"
                if task.request["artifact_edit"].get("reply_episode_id")
                else "worker",
                depth=0,
                degradation=None,
            )
            for task in store.episode_artifact_edit_tasks(episode.episode_id)
        ],
        report=report,
        can_stop=(
            episode.status in _STOPPABLE_EPISODE_STATUSES
            and episode.stop_requested_at is None
            and episode.ending is None
        ),
        continues_episode_id=episode.continues_episode_id,
        continued_by_episode_id=continued_by.episode_id if continued_by is not None else None,
        chain=_chain_members(store, episode, continued=continued_by is not None, report=report),
        can_continue=(
            episode.status in _TERMINAL_EPISODE_STATUSES
            and continued_by is None
            and not any(task.status in {"queued", "running", "pausing"} for task in tasks)
            and _continuable_session(store, episode)
            and store.continuation_slot_open(episode)
            and (episode.mode != "auto_research" or message_refusal is None)
        ),
        can_message=(
            message_refusal is None
            if episode.mode == "auto_research"
            else episode.status == "running"
        ),
        message_refusal=message_refusal,
        message_requires_continuation=(
            episode.mode == "auto_research"
            and episode_chain_records(store, episode)[-1].status in _TERMINAL_EPISODE_STATUSES
        ),
        live=episode.status in _LIVE_EPISODE_STATUSES,
        health=health,
        blocked_reason=blocked_reason,
        recommendation=next_step,
        task_control=task_control,
        run_section=_episode_run_section(health),
    )


def serialize_episodes(
    store: AppStore,
    project_id: str,
    *,
    mode: EpisodeMode | None = None,
    branch_summaries: BranchSummariesResolver | None = None,
    include_archived_branches: bool = False,
) -> list[EpisodeResponse]:
    """Serialize the ordered project list, optionally limited to one episode mode."""

    bounded_limit = 50

    def listed(episode: EpisodeRecord) -> bool:
        if mode is not None and episode.mode != mode:
            return False
        if include_archived_branches or episode.graph_target.kind != "branch":
            return True
        state = store.episode_isolation_state(
            project_id,
            episode.isolation_owner_episode_id
            or episode.graph_target.branch_id
            or episode.episode_id,
        )
        return state is None or not state.graph_archived

    # Filter before the limit so hidden archived branches do not shrink the list.
    selected = [episode for episode in store.episodes(project_id, limit=None) if listed(episode)][
        :bounded_limit
    ]
    # Retained archives remain discoverable after newer episodes fill the recent list.
    selected_by_id = {episode.episode_id: episode for episode in selected}
    for episode in store.archived_episodes(project_id):
        if listed(episode):
            selected_by_id[episode.episode_id] = episode
    selected = sorted(
        selected_by_id.values(),
        key=lambda episode: (episode.created_at, episode.episode_id),
        reverse=True,
    )
    archive_states = store.episode_archive_states(project_id)
    branch_summary: BranchSummaryResolver | None = None
    if branch_summaries is not None:
        branch_episodes = [episode for episode in selected if episode.graph_target.kind == "branch"]
        resolved = branch_summaries(branch_episodes)
        expected_ids = {episode.episode_id for episode in branch_episodes}
        if set(resolved) != expected_ids:
            raise ValueError("batched graph branch summaries do not match the episode list")

        def resolve_branch(episode: EpisodeRecord) -> GraphBranchSummary:
            return resolved[episode.episode_id]

        branch_summary = resolve_branch
    return [
        serialize_episode(
            store,
            project_id,
            episode,
            branch_summary=branch_summary,
            archive_state=archive_states.get(episode.episode_id),
        )
        for episode in selected
    ]


def _report_summary(stored: EpisodeReportRecord | None) -> EpisodeReportSummary | None:
    if stored is None:
        return None
    return EpisodeReportSummary(
        report_id=stored.report_id, ending=stored.ending, created_at=stored.created_at
    )


def _episode_report_summary(store: AppStore, episode: EpisodeRecord) -> EpisodeReportSummary | None:
    """A Stop skips the report, so a stopped episode publishes none."""

    if episode.ending == "stopped":
        return None
    return _report_summary(store.episode_report(episode.episode_id))


def episode_chain_records(store: AppStore, episode: EpisodeRecord) -> list[EpisodeRecord]:
    """The whole continuation chain ``episode`` belongs to, oldest first.

    The caller's record stands in for the requested episode itself so a
    projection built from a fresher copy stays coherent.
    """

    root = episode
    seen = {episode.episode_id}
    while root.continues_episode_id is not None and root.continues_episode_id not in seen:
        seen.add(root.continues_episode_id)
        source = store.episode(root.continues_episode_id)
        if source is None:
            break
        root = source
    try:
        chain = store.episode_chain(root.episode_id)
    except KeyError:
        return [episode]
    return [episode if member.episode_id == episode.episode_id else member for member in chain]


def _chain_members(
    store: AppStore,
    episode: EpisodeRecord,
    *,
    continued: bool,
    report: EpisodeReportSummary | None,
) -> list[EpisodeChainMember]:
    """Every chain member with its turns, ending, and report; a lone episode is its own chain."""

    members = (
        episode_chain_records(store, episode)
        if continued or episode.continues_episode_id is not None
        else [episode]
    )
    return [
        EpisodeChainMember(
            episode_id=member.episode_id,
            created_at=member.created_at,
            status=member.status,
            ending=member.ending,
            invocation_ceiling=member.invocation_ceiling,
            invocations_used=member.invocations_used,
            report=report
            if member.episode_id == episode.episode_id
            else _episode_report_summary(store, member),
        )
        for member in members
    ]


def episode_on_branch(store: AppStore, episode_id: str | None, branch_id: str) -> bool:
    """Whether ``episode_id`` names a member of the branch's continuation chain.

    A branch keeps its chain root's id while child routes and Work allocations
    belong to whichever member owns them now, so ownership is chain membership,
    not identity with the root.
    """

    if episode_id is None:
        return False
    member = store.episode(episode_id)
    return (
        member is not None
        and member.graph_target.kind == "branch"
        and member.graph_target.branch_id == branch_id
    )


def auto_research_message_refusal(store: AppStore, episode: EpisodeRecord) -> dict[str, str] | None:
    """Project the newest orchestrator's durable mail or continuation admission."""

    episode = episode_chain_records(store, episode)[-1]
    if episode.status == "running" and episode.ending is None and episode.root_operation_id:
        return None
    try:
        with store.connection() as connection:
            store.require_auto_research_continuation_available(connection, episode)
    except ValueError as exc:
        code, _, chat_id = str(exc).partition(":")
        detail = {
            "auto_research_not_ended": "The orchestrator is still ending; wait for it to settle.",
            "auto_research_already_continued": "Open the newest continuation to message its orchestrator.",
            "auto_research_session_unavailable": "The orchestrator session or stage is unavailable. Start a new Auto-research episode.",
            "auto_research_turn_active": "An episode turn is still active; wait for it to settle.",
            "auto_research_project_occupied": "Another Auto-research episode is live on this project.",
            "episode_merge_reserved": "The branch is being merged; wait for the merge to finish.",
            "episode_isolation_unavailable": "The branch's isolation has been removed or is being removed.",
            "auto_research_child_turn_active": f"Wait for the human turn in child chat {chat_id} to settle.",
        }.get(code, str(exc))
        return {"code": code, "detail": detail}
    return None


def _continuable_session(store: AppStore, episode: EpisodeRecord) -> bool:
    """Whether a continuation could resume this episode's native session."""

    if episode.mode == "experiment_loop":
        experiment = store.experiment_episode(episode.episode_id)
        return experiment is not None and experiment.session_bound
    if episode.root_operation_id is None:
        return False
    binding = store.auto_research_actor_binding(episode.root_operation_id)
    return binding is not None and bool(binding.native_session_id and binding.stage_root)


def _episode_run_section(health: EpisodeHealth) -> EpisodeRunSection:
    """Separate what only a human can move from what is still moving on its own.

    ``actionable`` is reserved for a run that has stopped making progress and
    stays stopped until a human acts, so a queue count means work is owed. A run
    that is still advancing is ``running`` however slowly, and settled history is
    archived below.
    """

    if health in {"completed", "stopped"}:
        return "completed"
    if health in {"needs_action", "failed"}:
        return "actionable"
    return "running"


def _serialize_task(
    task: AgentTaskRecord,
    *,
    store: AppStore,
    episode: _EpisodeProjectionParent,
    role: Literal["orchestrator", "worker", "wake"],
    depth: int,
    degradation: str | None,
) -> EpisodeTaskResponse:
    public_fields = EpisodeTaskResponse.model_fields.keys()
    values = task.model_dump(include=public_fields)
    values.update(
        role=role,
        depth=depth,
        degradation=degradation,
        browser_status=store.browser_turn_status(task.operation_id).public(),
    )
    if not isinstance(task.request.get("artifact_edit"), dict):
        values.update(_episode_task_controls(episode, task))
    return EpisodeTaskResponse.model_validate(values)


def _episode_task_metadata(
    store: AppStore,
    episode: EpisodeRecord,
    tasks: list[AgentTaskRecord],
) -> dict[str, tuple[Literal["orchestrator", "worker", "wake"], int]]:
    """Publish display role/depth from durable actor allocation lineage."""

    by_id = {task.operation_id: task for task in tasks}
    invocations = (
        store.auto_research_invocations([task.operation_id for task in tasks])
        if episode.mode == "auto_research"
        else {}
    )

    def actor_id(task: AgentTaskRecord) -> str:
        invocation = invocations.get(task.operation_id)
        return invocation.actor_operation_id if invocation is not None else task.operation_id

    def actor_depth(task: AgentTaskRecord) -> int:
        origin = by_id.get(actor_id(task), task)
        depth = 0
        current = origin
        parent_id = current.parent_operation_id
        seen = {current.operation_id}
        while parent_id and parent_id in by_id and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id[parent_id]
            if actor_id(current) != actor_id(parent):
                depth += 1
            current = parent
            parent_id = current.parent_operation_id
        return min(depth, 4)

    metadata: dict[str, tuple[Literal["orchestrator", "worker", "wake"], int]] = {}
    for task in tasks:
        invocation = invocations.get(task.operation_id)
        continuation_cause = store.agent_task_continuation_cause(task.operation_id)
        if (
            continuation_cause
            in {"watcher_wake", "graph_condition_wake", "message_wake", "lifecycle_wake"}
            or task.request.get("trigger") == "watcher"
        ):
            role: Literal["orchestrator", "worker", "wake"] = "wake"
        elif invocation is None:
            role: Literal["orchestrator", "worker", "wake"] = (
                "orchestrator" if task.operation_id == episode.root_operation_id else "worker"
            )
        else:
            role = invocation.role
        metadata[task.operation_id] = (role, actor_depth(task))
    return metadata


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
        episode,
        tasks,
        tasks_by_id,
        current_orchestrator_task_id,
        actor_operation_id_for=lambda task: str(
            task.request.get("actor_operation_id") or task.operation_id
        ),
        recovery_for=lambda operation_id: store.auto_research_control_recovery(
            episode.episode_id,
            operation_id,
        ),
        role_for=store.auto_research_invocation_role,
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
            failure_kind=control_recovery.failure_kind,
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


def space_auto_research_episode_projection(
    snapshot: AutoResearchSpaceRunProjectionSnapshot,
) -> tuple[EpisodeHealth, EpisodeRunSection, str]:
    """Reuse the canonical lifecycle policy on one compact space-run snapshot."""

    episode = snapshot.episode
    tasks = [
        task.model_copy(update=_episode_task_controls(episode, task)) for task in snapshot.tasks
    ]
    tasks_by_id = {task.operation_id: task for task in tasks}

    def recovery_for(operation_id: str) -> _SpaceRunRecoveryProjection | None:
        task = tasks_by_id.get(operation_id)
        if task is None or task.recovery_operation_id is None or task.recovery_status is None:
            return None
        return _SpaceRunRecoveryProjection(
            operation_id=task.recovery_operation_id,
            status=task.recovery_status,
        )

    current_control_task_id = _auto_research_control_task_id(
        episode,
        tasks,
        tasks_by_id,
        snapshot.current_orchestrator_task_id,
        actor_operation_id_for=lambda task: (
            task.actor_operation_id
            if isinstance(task, AutoResearchSpaceRunTaskState) and task.actor_operation_id
            else task.operation_id
        ),
        recovery_for=recovery_for,
        role_for=lambda operation_id: (
            tasks_by_id[operation_id].role if operation_id in tasks_by_id else None
        ),
    )
    recovery = (
        recovery_for(current_control_task_id) if current_control_task_id is not None else None
    )
    health, _recommendation, _task_control, _blocked_reason = project_episode_health(
        [
            EpisodeHealthInput(
                episode.episode_id,
                episode,
                tasks,
                current_control_task_id,
                recovery,
                has_open_questions=snapshot.has_open_questions,
            )
        ]
    )[episode.episode_id]
    run_section = _episode_run_section(health)
    last_activity_at = next(
        (task.last_activity_at for task in reversed(snapshot.tasks) if task.last_activity_at),
        episode.updated_at,
    )
    if run_section == "completed":
        last_activity_at = episode.ended_at or episode.updated_at
    return health, run_section, last_activity_at
