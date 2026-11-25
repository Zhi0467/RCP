"""The one display value of a run, shared by chat, pane, dock, and Runs.

Every surface renders this value as published. The generic episode owner fills
it from episode health; the Experiment owner refines it with its node, watcher,
and session rules. No surface keeps a second label, tone, or status table.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.episode_health import (
    EpisodeBlockedReason,
    EpisodeHealth,
    EpisodeTaskControlKind,
    episode_needs_action,
)
from rcp.storage import EpisodeEnding, EpisodeStatus

RunState = Literal[
    "starting",
    "active",
    "recovering",
    "waiting_on_watchers",
    "completion_pending",
    "degraded",
    "stopping",
    "wrapping_up",
    "failed",
    "stopped",
    "paused_at_limit",
    "needs_action",
    "completed",
]
RunPhase = Literal["needs_you", "running", "ended"]
RunSection = Literal["running", "ended"]
RunTone = Literal[
    "running",
    "waiting",
    "degraded",
    "stopping",
    "stopped",
    "actionable",
    "completed",
]
RunRecommendation = Literal[
    "continue",
    "wait",
    "resume",
    "retry",
    "reauthorize",
    "reauthenticate_provider",
    "keep_loop",
    "start_episode",
    "stop_and_restart",
    "resolve_requirements",
    "open_report",
    "review",
    "none",
]
RunActionKind = Literal[
    "stop",
    "pause",
    "resume",
    "retry",
    "continue",
    "open_report",
    "start",
]

_DISPLAY: dict[RunState, tuple[str, RunTone]] = {
    "starting": ("Starting", "running"),
    "active": ("Running", "running"),
    "recovering": ("Recovering", "waiting"),
    "waiting_on_watchers": ("Waiting on watchers", "waiting"),
    "completion_pending": ("Completion pending delivery", "waiting"),
    "degraded": ("Watcher degraded", "degraded"),
    "stopping": ("Stopping gracefully", "stopping"),
    "wrapping_up": ("Wrapping up", "running"),
    "failed": ("Failed", "degraded"),
    "stopped": ("Stopped", "stopped"),
    "paused_at_limit": ("Paused at invocation limit", "actionable"),
    "needs_action": ("Needs action", "actionable"),
    "completed": ("Completed", "completed"),
}
_ENDED_STATUSES: frozenset[EpisodeStatus] = frozenset({"completed", "failed", "stopped"})


class RunAction(BaseModel):
    """One control a run offers now, with the exact record it acts on."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: RunActionKind
    episode_id: str | None = None
    operation_id: str | None = None
    report_episode_id: str | None = None
    node_id: str | None = None


class RunPresentation(BaseModel):
    """What a task card shows for one run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    phase: RunPhase
    title: str
    label: str
    state_line: str
    tone: RunTone
    section: RunSection
    needs_you: bool
    recommendation: RunRecommendation
    actions: list[RunAction]


def run_display(state: RunState) -> tuple[str, RunTone]:
    """The label and tone of one run state, for rows that publish nothing else."""

    return _DISPLAY[state]


def present_run(
    *,
    state: RunState,
    title: str,
    ended: bool,
    needs_you: bool,
    recommendation: RunRecommendation,
    invocations_used: int,
    invocation_ceiling: int,
    actions: list[RunAction],
) -> RunPresentation:
    label, tone = run_display(state)
    section: RunSection = "ended" if ended else "running"
    if ended:
        turns = f"{invocations_used} turn{'' if invocations_used == 1 else 's'}"
    else:
        turns = f"turn {invocations_used} of {invocation_ceiling}"
    return RunPresentation(
        phase="needs_you" if needs_you else section,
        title=title,
        label=label,
        state_line=f"{label} · {turns}" if invocations_used else label,
        tone=tone,
        section=section,
        needs_you=needs_you,
        recommendation=recommendation,
        actions=actions,
    )


def run_ended(status: EpisodeStatus, ending: EpisodeEnding | None, state: RunState) -> bool:
    """Whether the run is over. A run still writing its report is not."""

    return state != "wrapping_up" and (ending is not None or status in _ENDED_STATUSES)


def episode_run_state(health: EpisodeHealth, ending: EpisodeEnding | None) -> RunState:
    if health == "needs_action" and ending == "exhausted":
        return "paused_at_limit"
    return health


def run_actions(
    *,
    episode_id: str,
    can_stop: bool,
    task_control: EpisodeTaskControlKind | None,
    control_task_id: str | None,
    can_continue: bool,
    report_episode_id: str | None,
    start_node_id: str | None = None,
) -> list[RunAction]:
    actions: list[RunAction] = []
    if task_control is not None and control_task_id is not None:
        actions.append(
            RunAction(kind=task_control, episode_id=episode_id, operation_id=control_task_id)
        )
    if can_continue:
        actions.append(RunAction(kind="continue", episode_id=episode_id))
    if start_node_id is not None:
        actions.append(RunAction(kind="start", node_id=start_node_id))
    if can_stop:
        actions.append(RunAction(kind="stop", episode_id=episode_id))
    if report_episode_id is not None:
        actions.append(RunAction(kind="open_report", report_episode_id=report_episode_id))
    return actions


def present_episode(
    *,
    title: str,
    status: EpisodeStatus,
    ending: EpisodeEnding | None,
    health: EpisodeHealth,
    blocked_reason: EpisodeBlockedReason | None,
    recommendation: RunRecommendation,
    invocations_used: int,
    invocation_ceiling: int,
    actions: list[RunAction],
) -> RunPresentation:
    """The generic episode's presentation, decided from episode health alone."""

    state = episode_run_state(health, ending)
    return present_run(
        state=state,
        title=title,
        ended=run_ended(status, ending, state),
        needs_you=episode_needs_action(health, blocked_reason),
        recommendation=recommendation,
        invocations_used=invocations_used,
        invocation_ceiling=invocation_ceiling,
        actions=actions,
    )
