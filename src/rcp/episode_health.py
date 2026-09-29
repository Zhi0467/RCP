"""Shared, batched episode lifecycle policy for projections and notifications."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol

from rcp.storage import (
    AgentFailureKind,
    AgentTaskRecord,
    AgentTaskStatus,
    AppStore,
    AutoResearchRecoveryStatus,
    EpisodeEnding,
    EpisodeRecord,
    EpisodeStatus,
    EpisodeWrapupState,
)


class _EpisodeProjectionParent(Protocol):
    status: EpisodeStatus
    stop_requested_at: str | None
    ending: EpisodeEnding | None
    wrapup_state: EpisodeWrapupState


class _EpisodeProjectionTask(Protocol):
    operation_id: str
    status: AgentTaskStatus
    can_pause: bool
    can_resume: bool
    can_retry: bool
    failure_kind: AgentFailureKind | None


class _AutoResearchControlTask(_EpisodeProjectionTask, Protocol):
    parent_operation_id: str | None
    attempt: int
    created_at: str


class _RecoveryProjection(Protocol):
    operation_id: str | None
    status: AutoResearchRecoveryStatus
    # The compact space-run snapshot does not carry it, and a summary view
    # showing `needs_action` without the exact reason is the same answer.
    failure_kind: str | None


EpisodeHealth = Literal[
    "starting",
    "active",
    "recovering",
    "needs_action",
    "stopping",
    "wrapping_up",
    "completed",
    "stopped",
    "failed",
]
EpisodeBlockedReason = Literal["sign_in", "reauthorize", "repeated_failure"]
EpisodeRecommendationKind = Literal[
    "continue",
    "wait",
    "resume",
    "retry",
    "reauthorize",
    "open_report",
    "review",
    "none",
]
EpisodeTaskControlKind = Literal["pause", "resume", "retry"]
EpisodeRunSection = Literal["actionable", "running", "completed"]


@dataclass(frozen=True)
class EpisodeHealthInput:
    episode_id: str
    episode: _EpisodeProjectionParent
    tasks: Sequence[_EpisodeProjectionTask]
    control_task_id: str | None
    recovery: _RecoveryProjection | None
    report_login_blocked: bool = False


def project_episode_health(
    inputs: Sequence[EpisodeHealthInput],
) -> dict[
    str,
    tuple[
        EpisodeHealth,
        EpisodeRecommendationKind,
        EpisodeTaskControlKind | None,
        EpisodeBlockedReason | None,
    ],
]:
    """Apply one precedence table to a batch of locally captured episode inputs."""
    return {
        item.episode_id: _episode_projection(
            item.episode,
            item.tasks,
            control_task_id=item.control_task_id,
            recovery=item.recovery,
            report_login_blocked=item.report_login_blocked,
        )
        for item in inputs
    }


def _episode_recovery_control(
    task: _EpisodeProjectionTask | None,
) -> EpisodeTaskControlKind | None:
    """Name the one recovery this turn actually offers, in its own preference order."""

    if task is None:
        return None
    if task.status == "paused":
        if task.can_resume:
            return "resume"
        if task.can_retry:
            return "retry"
    if task.status in {"interrupted", "failed"}:
        if task.can_retry:
            return "retry"
        if task.can_resume:
            return "resume"
    return None


def _episode_projection(
    episode: _EpisodeProjectionParent,
    tasks: Sequence[_EpisodeProjectionTask],
    *,
    control_task_id: str | None,
    recovery: _RecoveryProjection | None,
    report_login_blocked: bool = False,
) -> tuple[
    EpisodeHealth,
    EpisodeRecommendationKind,
    EpisodeTaskControlKind | None,
    EpisodeBlockedReason | None,
]:
    """Apply the episode health table in precedence order, before task controls."""

    task = next((item for item in tasks if item.operation_id == control_task_id), None)
    if episode.ending == "stopped" or (episode.ending is None and episode.status == "stopped"):
        return "stopped", "none", None, None
    if report_login_blocked and episode.wrapup_state in {"pending", "running"}:
        return "wrapping_up", "wait", None, "sign_in"
    if episode.status == "wrapping_up" or episode.wrapup_state in {"pending", "running"}:
        return "wrapping_up", "wait", None, None
    if episode.ending == "completed" or (episode.ending is None and episode.status == "completed"):
        return (
            "completed",
            "open_report" if episode.wrapup_state == "ready" else "none",
            None,
            None,
        )
    if episode.ending == "failed" or (episode.ending is None and episode.status == "failed"):
        return (
            "failed",
            "open_report" if episode.wrapup_state == "ready" else "review",
            None,
            None,
        )
    if episode.ending in {"exhausted", "human_pause"}:
        return "needs_action", "reauthorize", None, "reauthorize"
    recovery_control = _episode_recovery_control(task)
    live_turn = any(item.status in {"queued", "running", "pausing"} for item in tasks)
    if (episode.stop_requested_at is not None or episode.status == "stopping") and live_turn:
        return "stopping", "wait", None, None
    if recovery is not None and recovery.status == "pending":
        return "recovering", "wait", None, None
    # A retry that failed the way its predecessor did stopped the ladder. RCP
    # does not say why; the provider's own message is on the turn, and the
    # human can change the provider, model, or reasoning and retry.
    if (
        recovery is not None
        and recovery.status == "blocked"
        and recovery.failure_kind != "provider_auth"
    ):
        return "needs_action", "retry", "retry", "repeated_failure"
    if task is not None and task.status == "failed" and task.failure_kind == "provider_auth":
        return "needs_action", recovery_control or "review", recovery_control, "sign_in"
    if task is not None and task.status in {"paused", "interrupted", "failed"}:
        return "needs_action", recovery_control or "review", recovery_control, None
    if episode.status == "stopping":
        return "stopping", "wait", None, None
    if episode.status == "queued" or any(item.status == "queued" for item in tasks):
        return "starting", "wait", None, None
    if episode.status == "needs_action":
        return "needs_action", "review", None, None
    if task is not None and task.status == "pausing":
        return "active", "wait", None, None
    pause = "pause" if task is not None and task.status == "running" and task.can_pause else None
    return "active", "continue", pause, None


def _auto_research_control_task_id(
    episode: _EpisodeProjectionParent,
    tasks: Sequence[_AutoResearchControlTask],
    tasks_by_id: dict[str, _AutoResearchControlTask],
    current_orchestrator_task_id: str | None,
    *,
    actor_operation_id_for: Callable[[_AutoResearchControlTask], str],
    recovery_for: Callable[[str], _RecoveryProjection | None],
    role_for: Callable[[str], str | None],
) -> str | None:
    if episode.status not in {"stopping", "wrapping_up"}:
        return current_orchestrator_task_id

    recovered_parent_ids = {
        task.parent_operation_id
        for task in tasks
        if task.parent_operation_id is not None
        and (parent := tasks_by_id.get(task.parent_operation_id)) is not None
        and task.attempt == parent.attempt + 1
        and actor_operation_id_for(task) == actor_operation_id_for(parent)
    }
    current_orchestrator = tasks_by_id.get(current_orchestrator_task_id or "")
    orchestrator_recovery = (
        recovery_for(current_orchestrator.operation_id)
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
        and role_for(current_orchestrator.operation_id) == "orchestrator"
    ):
        return current_orchestrator.operation_id

    paused_workers = [
        task
        for task in tasks
        if task.status == "paused"
        and task.operation_id not in recovered_parent_ids
        and (task.can_resume or task.can_retry)
        and role_for(task.operation_id) == "worker"
    ]
    if paused_workers:
        return max(
            paused_workers,
            key=lambda task: (task.created_at, task.operation_id),
        ).operation_id
    return current_orchestrator_task_id


def load_episode_health(
    store: AppStore,
    episodes: Sequence[EpisodeRecord],
) -> dict[
    str,
    tuple[
        EpisodeHealth,
        EpisodeRecommendationKind,
        EpisodeTaskControlKind | None,
        EpisodeBlockedReason | None,
    ],
]:
    """Read local episode inputs, then apply the shared policy as one batch."""
    from rcp.agents.provider_accounts import account_login_refusal
    from rcp.agents.provider_environment import ProviderCredentialStore

    inputs = []
    credentials = ProviderCredentialStore.for_data_dir(store.path.parent)
    for episode in episodes:
        tasks = [
            task.model_copy(update=_episode_task_controls(episode, task))
            for task in operational_episode_tasks(store, episode)
        ]
        control_id = tasks[-1].operation_id if tasks else None
        recovery = None
        if episode.mode == "auto_research":
            binding = None
            if episode.root_operation_id is not None:
                with suppress(KeyError, RuntimeError, ValueError):
                    binding = store.auto_research_actor_binding(episode.root_operation_id)
            orchestrator_id = (
                binding.current_operation_id
                if binding is not None and binding.episode_id == episode.episode_id
                else None
            )
            control_id = _auto_research_control_task_id(
                episode,
                tasks,
                {task.operation_id: task for task in tasks},
                orchestrator_id,
                actor_operation_id_for=lambda task: str(
                    task.request.get("actor_operation_id") or task.operation_id
                ),
                recovery_for=lambda operation_id, episode_id=episode.episode_id: (
                    store.auto_research_control_recovery(episode_id, operation_id)
                ),
                role_for=store.auto_research_invocation_role,
            )
            if control_id is not None:
                recovery = store.auto_research_control_recovery(episode.episode_id, control_id)
        wrapup = (
            store.episode_wrapup(episode.episode_id)
            if episode.wrapup_state in {"pending", "running"}
            else None
        )
        blocked = (
            wrapup is not None
            and wrapup.provider is not None
            and account_login_refusal(
                store, credentials, wrapup.provider, wrapup.execution_host or ""
            )
            is not None
        )
        inputs.append(
            EpisodeHealthInput(episode.episode_id, episode, tasks, control_id, recovery, blocked)
        )
    return project_episode_health(inputs)


def _episode_task_controls(
    episode: _EpisodeProjectionParent,
    task: _EpisodeProjectionTask,
) -> dict[str, bool]:
    """Mask all controls after ending, or only Pause behind a graceful Stop."""

    if episode.ending is not None:
        return {"can_pause": False, "can_resume": False, "can_retry": False}
    return {
        "can_pause": task.can_pause and episode.stop_requested_at is None,
        "can_resume": task.can_resume,
        "can_retry": task.can_retry,
    }


def operational_episode_tasks(
    store: AppStore, episode: EpisodeRecord, *, newest: int | None = None
) -> list[AgentTaskRecord]:
    tasks: list[AgentTaskRecord] = []
    for task in store.episode_tasks(episode.episode_id, newest=newest):
        if task.episode_id != episode.episode_id or task.project_id != episode.project_id:
            raise ValueError("episode task lineage crosses its parent boundary")
        # Hidden wrap-up work and branch merges are not the episode's turns: the
        # graph branch summary narrates merges, and a queued merge must not read
        # as the episode starting.
        if not task.visible or task.kind in {"episode_report", "branch_merge"}:
            continue
        tasks.append(task)
    return tasks
