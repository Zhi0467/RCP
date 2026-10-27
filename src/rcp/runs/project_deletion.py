"""Stop a project's in-flight work so Delete never refuses because of it.

Delete names what it will stop (`deletion_preview`), then stops all of it
(`stop_project_work`) inside the catalog's deletion fence: watchers first, so
none wakes new work; runs, so none admits another turn; then every agent task
and its provider process; last, the compute jobs and watched external jobs RCP
holds a cancel command for, each confirmed by its saved check. A process RCP
could not stop, or could not confirm stopped, is returned by name; deletion goes
ahead and reports it. Delete stops only work the human was shown: when the live
work no longer matches the preview's `stop_token`, it refuses before stopping
anything, and the dialog reloads the list.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel

from rcp.compute_jobs.jobs import helper_watch_spec
from rcp.compute_jobs.models import ComputeJobRecord
from rcp.limits import (
    PROJECT_DELETION_CANCEL_CONFIRM_INTERVAL_SECONDS,
    PROJECT_DELETION_CANCEL_CONFIRM_SECONDS,
)
from rcp.runs.membership_fence import press_episode_stop
from rcp.server_ops.paper_lifecycle import get_paper_live
from rcp.storage import AppStore, EpisodeNotRunning, ProjectActiveTaskConflict
from rcp.storage.models import (
    AgentTaskRecord,
    EpisodeRecord,
    StoredWatcherRecord,
    WatcherClaimConflict,
    WatcherRecord,
)
from rcp.watchers import WatchSpec, run_watcher_cancel, run_watcher_check

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks

logger = logging.getLogger(__name__)

_TASK_KIND_LABELS = {
    "seed": "Seed project graph",
    "refresh": "Refresh project graph",
    "node_chat": "Node conversation",
    "project_chat": "Project conversation",
    "paper_coach": "Writing coach",
    "auto_research": "Auto-research",
    "branch_merge": "Branch merge",
    "episode_report": "Run report",
    "artifact_edit": "Artifact edit",
}
_EPISODE_MODE_LABELS = {"auto_research": "Auto-research", "experiment_loop": "Experiment loop"}
_STOP_DIAGNOSTIC = "The project is being deleted."


class ProjectDeletionStopGroup(BaseModel):
    summary: str
    items: list[str]


class ProjectDeletionPreview(BaseModel):
    """What confirming Delete will stop, rendered verbatim by the dialog."""

    stops: list[ProjectDeletionStopGroup]
    blocked_reason: str | None
    # Identifies the rendered `stops`; Delete sends it back to bind to them.
    stop_token: str


class ProjectDeletionStopsChanged(Exception):
    """The work Delete would stop differs from the list the human confirmed."""


def deletion_preview(store: AppStore, project_id: str) -> ProjectDeletionPreview:
    work = store.project_live_work(project_id)
    branches = store.branch_names(project_id)
    groups = [
        _group("agent task", [_task_label(task, branches) for task in work.tasks]),
        _group("run", [_episode_label(episode, branches) for episode in work.episodes]),
        _group("compute job", [_job_label(job) for job in work.compute_jobs]),
        _group("watcher", [], count=len(work.watchers)),
    ]
    stops = [group for group in groups if group is not None]
    rendered = "\n".join(group.model_dump_json() for group in stops)
    return ProjectDeletionPreview(
        stops=stops,
        stop_token=hashlib.sha256(rendered.encode()).hexdigest()[:32],
        blocked_reason=(
            "Let the project transfer finish before deleting this project."
            if work.transfer_unfinished
            else None
        ),
    )


def stop_project_work(
    store: AppStore,
    background: BackgroundAgentTasks,
    project_id: str,
    stop_token: str | None,
) -> list[str]:
    """Stop every in-flight item; return one line per process that may still run.

    Runs inside the deletion fence, which refuses new work, so the token check
    binds what the human confirmed; only a watcher delivery already claimed can
    still admit one turn, which the task pass stops. Work that finished since
    the preview never refuses: with nothing left to stop, no token is needed.
    """

    current = deletion_preview(store, project_id)
    if current.stops and current.stop_token != stop_token:
        raise ProjectDeletionStopsChanged(
            "The running work changed. Review it and confirm Delete again."
        )
    paper = get_paper_live(store, project_id)
    if paper is not None:
        paper.pause(project_id)
    unstopped: list[str] = []
    work = store.project_live_work(project_id)
    for watcher in work.watchers:
        try:
            store.stop_watchers(project_id, [watcher.watcher_id])
        except (KeyError, WatcherClaimConflict) as exc:
            # A delivery already claimed admits a turn; the task pass stops it.
            logger.info("Watcher %s left to the task stop: %s", watcher.watcher_id, exc)
    for episode in work.episodes:
        try:
            press_episode_stop(
                store,
                background,
                project_id,
                episode,
                diagnostic=_STOP_DIAGNOSTIC,
                initiated_by="system:project_deleted",
            )
        except (EpisodeNotRunning, KeyError, ValueError) as exc:
            # Settled meanwhile, or in wrap-up; its report task is stopped below.
            logger.info("Episode %s needed no Stop: %s", episode.episode_id, exc)
    branches = store.branch_names(project_id)
    # A run's settle or a claimed watcher delivery can admit one last turn.
    stopping: set[str] = set()
    for _ in range(2):
        tasks = store.project_live_work(project_id).tasks
        if not tasks:
            break
        for task in tasks:
            reason = background.stop_for_project_deletion(task.operation_id)
            if reason is not None:
                unstopped.append(f"{_task_label(task, branches)}: {reason}")
        stopping.update(background.wait_for_stopped_workers([task.operation_id for task in tasks]))
    # The watchers are already retired, so a retry would no longer find their
    # jobs: cancel them now, even when the project must be kept below.
    unstopped.extend(_cancel_external_work(store, project_id, work.watchers))
    # A worker can settle its task, then keep using the project's rows and
    # stage; deleting them under it would corrupt both, so keep the project.
    if background.wait_for_stopped_workers(sorted(stopping), timeout=0.0):
        raise ProjectActiveTaskConflict(
            " ".join(
                [
                    "An agent task is still stopping. Try Delete again in a moment.",
                    *(["RCP could not stop:", "; ".join(unstopped)] if unstopped else []),
                ]
            )
        )
    if paper is not None:
        paper.drain(project_id)
        paper.dispose(project_id)
    return unstopped


def _cancel_external_work(
    store: AppStore,
    project_id: str,
    earlier_watchers: list[StoredWatcherRecord],
) -> list[str]:
    """Run each saved cancel command once, then confirm with the job's saved check.

    A cancel's exit zero only means the command was accepted, so a job whose
    check still reports it running at the deadline is returned as unstopped.
    """

    work = store.project_live_work(project_id)
    # One action per job: watchers observing the same job share host, cwd, and command.
    actions: dict[tuple[str, str, str], tuple[WatchSpec, str, str]] = {}
    uncancellable: dict[tuple[str, str, str], tuple[WatchSpec, str, str]] = {}
    for job in work.compute_jobs:
        spec = WatchSpec.model_validate(helper_watch_spec(job))
        assert spec.cancel_command is not None
        actions[(job.execution_host, spec.cwd, spec.cancel_command)] = (
            spec,
            job.execution_host,
            _job_label(job),
        )
    for watcher in [*earlier_watchers, *work.watchers]:
        if not isinstance(watcher, WatcherRecord):
            continue
        spec = WatchSpec(
            check_command=watcher.check_command,
            log_path=watcher.log_path,
            cwd=watcher.cwd,
            cancel_command=watcher.cancel_command,
        )
        label = f"Watched job on {watcher.execution_host or 'this computer'}"
        if watcher.cancel_command is None:
            uncancellable.setdefault(
                (watcher.execution_host, watcher.cwd, watcher.check_command),
                (spec, watcher.execution_host, label),
            )
        else:
            actions.setdefault(
                (watcher.execution_host, watcher.cwd, watcher.cancel_command),
                (spec, watcher.execution_host, label),
            )
    unstopped: list[str] = []
    # Without a saved cancel, RCP can only say whether the job is still running.
    for spec, host, label in uncancellable.values():
        result = run_watcher_check(spec, host)
        if result.state == "active":
            unstopped.append(f"{label}: RCP holds no cancel for it, and it still reports running")
        elif result.state == "error":
            unstopped.append(f"{label}: RCP holds no cancel for it, and {result.error}")
    accepted: list[tuple[WatchSpec, str, str]] = []
    for spec, host, label in actions.values():
        error = run_watcher_cancel(spec, host)
        if error is None:
            accepted.append((spec, host, label))
        else:
            unstopped.append(f"{label}: {error}")
    deadline = time.monotonic() + PROJECT_DELETION_CANCEL_CONFIRM_SECONDS
    while accepted:
        running: list[tuple[WatchSpec, str, str]] = []
        for spec, host, label in accepted:
            result = run_watcher_check(spec, host)
            if result.state == "active":
                running.append((spec, host, label))
            elif result.state == "error":
                unstopped.append(f"{label}: cancel was accepted, then {result.error}")
        accepted = running
        if not accepted or time.monotonic() >= deadline:
            break
        time.sleep(PROJECT_DELETION_CANCEL_CONFIRM_INTERVAL_SECONDS)
    unstopped.extend(
        f"{label}: cancel was accepted, but the job still reports running"
        for _, _, label in accepted
    )
    return unstopped


def _group(
    noun: str, items: list[str], *, count: int | None = None
) -> ProjectDeletionStopGroup | None:
    total = len(items) if count is None else count
    if total == 0:
        return None
    return ProjectDeletionStopGroup(
        summary=f"{total} {noun}{'' if total == 1 else 's'}", items=items
    )


def _branch_suffix(target_branch_id: str | None, branches: dict[str, str]) -> str:
    if target_branch_id is None:
        return ""
    return f" ({branches.get(target_branch_id, 'graph branch')})"


def _task_label(task: AgentTaskRecord, branches: dict[str, str]) -> str:
    kind = _TASK_KIND_LABELS.get(task.kind, task.kind)
    return kind + _branch_suffix(task.graph_target.branch_id, branches)


def _episode_label(episode: EpisodeRecord, branches: dict[str, str]) -> str:
    mode = _EPISODE_MODE_LABELS.get(episode.mode, "Run")
    return mode + _branch_suffix(episode.graph_target.branch_id, branches)


def _job_label(job: ComputeJobRecord) -> str:
    return f"{job.label or _command_name(job.argv)} on {job.execution_machine}"


def _command_name(argv: list[str]) -> str:
    """The script a job runs, else its program: `nvda_poller.py`, not the interpreter."""

    scripts = [part for part in argv if PurePosixPath(part).suffix in {".py", ".sh", ".R", ".jl"}]
    return PurePosixPath(scripts[0] if scripts else argv[0]).name
