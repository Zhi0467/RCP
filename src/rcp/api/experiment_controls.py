from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict

from rcp.api.episodes import EpisodeResponse, EpisodeTaskResponse
from rcp.control import ExperimentControlState
from rcp.core.models import (
    CLOSED_EXPERIMENT_STATUSES,
    Experiment,
    GraphState,
)
from rcp.runs.experiment_admission import experiment_control_from_runtime
from rcp.storage import ExperimentLoopRuntime

ExperimentLoopHealth = Literal[
    "starting",
    "agent_active",
    "waiting_on_watchers",
    "completion_pending",
    "degraded",
    "stopping",
    "wrapping_up",
    "failed",
    "human_stopped",
    "paused_at_limit",
    "needs_action",
    "completed",
]
ExperimentRecommendationKind = Literal[
    "wait",
    "resume",
    "retry",
    "reauthenticate_provider",
    "keep_loop",
    "start_episode",
    "stop_and_restart",
    "resolve_requirements",
    "open_report",
    "review",
    "none",
]
ExperimentRunSection = Literal["running", "actionable", "completed"]
ExperimentTaskControlKind = Literal["resume", "retry"]


class ExperimentControlResponse(ExperimentControlState):
    """One Experiment's graph control plus its decided Runs lifecycle."""

    model_config = ConfigDict(extra="forbid", strict=True)

    episode: EpisodeResponse | None
    health: ExperimentLoopHealth
    recommendation: ExperimentRecommendationKind
    run_section: ExperimentRunSection
    live: bool
    can_start: bool
    can_stop: bool
    stop_pending: bool
    task_control: ExperimentTaskControlKind | None
    can_switch_provider: bool
    can_open_report: bool
    report_episode_id: str | None
    report_is_current: bool
    node_closed: bool


def _experiment_control_response(
    state: GraphState,
    experiment_id: str,
    runtime: ExperimentLoopRuntime,
    episode_payload: dict[str, object] | EpisodeResponse | None,
    *,
    latest_report_episode_id: str | None = None,
) -> ExperimentControlResponse:
    """Publish the complete Experiment control consumed by Runs.

    The canonical Experiment remains editable graph data. This response owns only
    operational conclusions whose inputs are already backend state, so a browser
    cannot grow a second lifecycle by combining the raw fields differently.
    """

    node = state.nodes.get(experiment_id)
    if not isinstance(node, Experiment):
        raise ValueError(f"Node {experiment_id!r} is not an Experiment.")
    control = experiment_control_from_runtime(state, experiment_id, runtime)
    episode = (
        episode_payload
        if isinstance(episode_payload, EpisodeResponse)
        else EpisodeResponse.model_validate(episode_payload)
        if episode_payload is not None
        else None
    )
    task = _experiment_control_task(episode)
    task_control = _experiment_task_control(episode)
    queued = task.queued if task is not None else control.operational.current_queued
    active = task.active if task is not None else control.operational.current_active
    awaiting_human = (
        task.awaiting_human if task is not None else control.operational.current_awaiting_human
    )
    # Another Work turn may hold the episode's native session while an answered
    # question waits for delivery. It is activity, not a request for human input.
    if (
        runtime.session_active
        and not awaiting_human
        and (episode is None or episode.blocked_reason != "question")
    ):
        active = True
    # A revoked login fails every attempt the same way. The turn is still
    # recoverable, so health keeps counting it, but the controls that would
    # spend an attempt on it are withheld until a person signs in.
    revoked_login = bool(
        control.operational.current_failure_kind == "provider_auth"
        and task_control is not None
        and awaiting_human
    )
    has_valid_recovery = task_control is not None or (
        task is None and control.operational.task_active and awaiting_human
    )
    health = _experiment_run_health(
        node,
        control,
        episode,
        queued=queued,
        active=active,
        awaiting_human=awaiting_human,
        has_valid_recovery=has_valid_recovery,
    )
    if runtime.projection_diagnostic is not None:
        health = "degraded"
    recommendation = _experiment_recommendation(
        control,
        episode,
        health,
        active=active,
        awaiting_human=awaiting_human,
        task_control=task_control,
        revoked_login=revoked_login,
    )
    run_section = _experiment_run_section(health, awaiting_human=awaiting_human)
    ended = episode is not None and episode.ending is not None
    live = not ended and bool(
        control.operational.episode_live
        or active
        or control.operational.task_active
        or control.operational.detached_work_active
        or control.operational.watcher_completion_pending
    )
    stop_pending = control.operational.stop_requested and not control.operational.stop_settled
    can_stop = bool(
        control.episode_id
        and not ended
        and not control.operational.stop_requested
        and (live or awaiting_human)
    )
    report_episode_id = latest_report_episode_id or (
        episode.episode_id
        if episode is not None and episode.report is not None and episode.wrapup_state == "ready"
        else None
    )
    can_open_report = report_episode_id is not None
    # A revoked login withdraws Retry but not this: recovery takes the provider
    # change before it consults the old session, so another provider is the one
    # recovery that still works without the human repairing that sign-in first.
    can_switch_provider = bool(task_control is not None and task is not None and task.can_retry)
    return ExperimentControlResponse.model_validate(
        {
            **control.model_dump(mode="json"),
            "episode": episode.model_dump(mode="json") if episode is not None else None,
            "health": health,
            "recommendation": recommendation,
            "run_section": run_section,
            "live": live,
            "can_start": control.ready,
            "can_stop": can_stop,
            "stop_pending": stop_pending,
            "task_control": task_control,
            "can_switch_provider": can_switch_provider,
            "can_open_report": can_open_report,
            "report_episode_id": report_episode_id,
            "report_is_current": bool(
                report_episode_id is not None and report_episode_id == control.episode_id
            ),
            "node_closed": node.status in CLOSED_EXPERIMENT_STATUSES,
        }
    )


def _experiment_control_task(episode: EpisodeResponse | None) -> EpisodeTaskResponse | None:
    if episode is None or episode.current_control_task_id is None:
        return None
    return next(
        (task for task in episode.tasks if task.operation_id == episode.current_control_task_id),
        None,
    )


def _experiment_task_control(
    episode: EpisodeResponse | None,
) -> ExperimentTaskControlKind | None:
    if episode is None or episode.task_control not in {"resume", "retry"}:
        return None
    return episode.task_control


def _experiment_run_health(
    node: Experiment,
    control: ExperimentControlState,
    episode: EpisodeResponse | None,
    *,
    queued: bool,
    active: bool,
    awaiting_human: bool,
    has_valid_recovery: bool,
) -> ExperimentLoopHealth:
    operational = control.operational
    stop_requested = operational.stop_requested
    if stop_requested and not operational.stop_settled:
        return "needs_action" if has_valid_recovery else "stopping"
    if (
        node.status in CLOSED_EXPERIMENT_STATUSES
        and episode is not None
        and episode.health == "stopped"
    ):
        return "completed"
    if stop_requested and operational.stop_settled and awaiting_human:
        return "human_stopped"
    if episode is not None and (
        episode.status == "wrapping_up" or episode.wrapup_state in {"pending", "running"}
    ):
        return "wrapping_up"
    if episode is not None and episode.health == "failed":
        return "failed"
    if episode is not None and episode.health == "stopped":
        return "human_stopped"
    if episode is not None and episode.ending is not None:
        if episode.ending == "completed":
            return "completed"
        if node.status in CLOSED_EXPERIMENT_STATUSES:
            return "completed"
        if episode.ending == "exhausted":
            return "paused_at_limit"
        if episode.ending == "human_pause":
            return "needs_action"
    if queued:
        return "starting"
    if active:
        return "agent_active"
    if awaiting_human or operational.task_active:
        return "needs_action"

    completion_pending = operational.watcher_completion_pending
    detached_work_active = operational.detached_work_active
    can_wake = bool(
        not stop_requested
        and control.invocations_remaining > 0
        and not operational.episode_exited
        and not operational.session.diagnostic
        and not control.graph_reasons
    )
    if (completion_pending or detached_work_active) and control.invocations_remaining <= 0:
        return "paused_at_limit"
    if completion_pending and not detached_work_active and operational.watcher_delivery_diagnostic:
        return "needs_action"
    if completion_pending and not can_wake:
        return "needs_action"
    if detached_work_active and not can_wake:
        return "needs_action"
    if operational.watcher_degraded:
        return "degraded"
    if detached_work_active:
        return "waiting_on_watchers"
    if completion_pending:
        return "completion_pending"
    if node.status in CLOSED_EXPERIMENT_STATUSES:
        return "completed"
    if stop_requested:
        return "human_stopped"
    if control.invocations_remaining <= 0 and control.invocations_used > 0:
        return "paused_at_limit"
    return "needs_action"


def _experiment_recommendation(
    control: ExperimentControlState,
    episode: EpisodeResponse | None,
    health: ExperimentLoopHealth,
    *,
    active: bool,
    awaiting_human: bool,
    task_control: ExperimentTaskControlKind | None,
    revoked_login: bool,
) -> ExperimentRecommendationKind:
    if health in {"stopping", "wrapping_up"}:
        return "wait"
    # A report is the deliverable of the ending, not the next step past it. Once
    # the loop is actually resumable again, recommending the report would leave
    # the human with no published pointer to the control that moves the work on.
    resumable = health in {"paused_at_limit", "human_stopped"} and control.ready
    if (
        episode is not None
        and episode.wrapup_state == "ready"
        and episode.report is not None
        and not resumable
    ):
        return "open_report"
    if episode is not None and episode.wrapup_state == "legacy_unavailable":
        return "none"
    if health == "failed":
        # An ended failure is past recovering in place; what moves the work on is
        # the control the card offers, Add turns or Start new episode.
        return "start_episode" if control.ready else "none"
    if active:
        return "wait"
    # Retry stays available, because signing in is what makes it work and
    # nothing would restore a control withdrawn on a failure that never
    # changes. This is what tells the human to sign in first.
    if revoked_login:
        return "reauthenticate_provider"
    if task_control is not None:
        return task_control
    if health == "degraded":
        return "keep_loop"
    if health in {"waiting_on_watchers", "completion_pending"}:
        return "wait"
    if health == "completed":
        return "none"
    if control.graph_reasons:
        return "resolve_requirements"
    ended = episode is not None and episode.ending is not None
    if (
        control.episode_id
        and not ended
        and not control.operational.stop_requested
        and (awaiting_human or control.operational.episode_live)
    ):
        return "stop_and_restart"
    if health in {"paused_at_limit", "human_stopped"}:
        return "start_episode" if control.ready else "review"
    if control.ready:
        return "start_episode"
    return "review"


def _experiment_run_section(
    health: ExperimentLoopHealth,
    *,
    awaiting_human: bool,
) -> ExperimentRunSection:
    if health == "stopping" and awaiting_human:
        return "actionable"
    if health in {
        "starting",
        "agent_active",
        "waiting_on_watchers",
        "completion_pending",
        "degraded",
        "stopping",
        "wrapping_up",
    }:
        return "running"
    if health in {"human_stopped", "paused_at_limit", "needs_action"}:
        return "actionable"
    return "completed"


__all__ = [
    "_experiment_control_response",
    "ExperimentControlResponse",
]
