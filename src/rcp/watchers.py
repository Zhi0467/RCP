"""Watchers: shell checks, graph conditions, polling, and the one wake path.

A watcher wakes the agent that armed it. When that agent runs an episode,
the episode owner decides in one step whether the wake runs and spends.
"""

from __future__ import annotations

import logging
import shlex
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from rcp.core.models import AuthorizedHuman, GraphState, Patch
from rcp.core.transition_models import (
    GraphHeadRef,
    GraphTargetRef,
    TransitionEvent,
    TransitionTrace,
)
from rcp.limits import (
    WATCHER_CHECK_TIMEOUT_SECONDS,
    WATCHER_CHECK_WORKERS,
    WATCHER_ERROR_MAX_CHARS,
    WATCHER_POLL_INTERVAL_SECONDS,
)
from rcp.machine_cards import host_of
from rcp.machine_sleep import seconds_until_automatic_launch
from rcp.storage import (
    AgentTaskKind,
    AgentTaskRecord,
    AppStore,
    EpisodeRecord,
    GraphCondition,
    NodeStatusGraphCondition,
    ProposalResolvedGraphCondition,
    WatcherCheckRecord,
    WatcherRecord,
)
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script
from rcp.transport.watcher_process import run_check_process, watcher_shell_command

if TYPE_CHECKING:
    from rcp.runs.transition_event_reconciliation import AcceptedGraphBoundary

logger = logging.getLogger(__name__)

_LOGIN_SHELL_NOISE = (
    "cannot set terminal process group",
    "no job control in this shell",
)


class WatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_command: str = Field(min_length=1)
    log_path: str = Field(min_length=1)
    cwd: str = Field(min_length=1)
    cancel_command: str | None = Field(default=None, min_length=1)

    @field_validator("check_command", "cancel_command")
    @classmethod
    def command_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("watcher commands must not be blank")
        return stripped

    @field_validator("log_path", "cwd")
    @classmethod
    def paths_are_absolute(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute():
            raise ValueError("watcher paths must be absolute")
        return value


class AgentWatchSpec(WatchSpec):
    """A watcher an agent declares, named by the one line a member reads in Wakes."""

    purpose: str = Field(min_length=1, max_length=120)

    @field_validator("purpose")
    @classmethod
    def purpose_is_not_blank(cls, value: str) -> str:
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("purpose must not be blank")
        return stripped


class WatcherBinding(BaseModel):
    """The agent and turn a newly armed watcher belongs to."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    agent_id: str = Field(min_length=1)
    origin_turn_id: str = Field(min_length=1)
    graph_target: GraphTargetRef = Field(default_factory=GraphTargetRef)
    machine_id: str


class WatcherCheckResult(BaseModel):
    state: Literal["active", "complete", "error"]
    checked_at: str
    exit_code: int | None = None
    error: str | None = None


WatcherCheckRunner = Callable[[WatchSpec, str, float], WatcherCheckResult]
WatcherPollCompletedCallback = Callable[[], None]
AdmissionFence = Callable[[Callable[[], None]], bool]


class WatcherRetryGeneration:
    """A retry pass lease that serializes its final side effect with stop()."""

    def __init__(
        self,
        is_current: Callable[[], bool],
        run_if_current: Callable[[Callable[[], None]], bool],
    ) -> None:
        self.is_current = is_current
        self.run_if_current = run_if_current


class GraphWatcherRetryRegistry:
    """Retry generations and per-project reconciliation locks for graph watchers."""

    def __init__(self) -> None:
        self._retry_guard = threading.Lock()
        self._failures: dict[str, int] = {}
        self._passes: dict[str, int] = {}
        self._lock_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def schedule(self, project_id: str) -> None:
        """Retry transient reconciliation failures with capped poll-pass backoff."""

        max_passes = max(1, 60 // WATCHER_POLL_INTERVAL_SECONDS)
        with self._retry_guard:
            failures = self._failures.get(project_id, 0) + 1
            self._failures[project_id] = failures
            self._passes[project_id] = min(
                2 ** min(failures - 1, 6),
                max_passes,
            )

    def request(self, project_id: str) -> None:
        """Make a project due on the next retry pass without counting a failure."""

        with self._retry_guard:
            self._passes[project_id] = min(self._passes.get(project_id, 1), 1)

    def clear(self, project_id: str) -> None:
        with self._retry_guard:
            self._failures.pop(project_id, None)
            self._passes.pop(project_id, None)

    def due(self) -> list[str]:
        due: list[str] = []
        with self._retry_guard:
            for project_id, passes in list(self._passes.items()):
                if passes <= 1:
                    # Keep due work durable in process until its reconciliation
                    # explicitly succeeds or reaches a non-retryable outcome. A
                    # retry generation can be invalidated after this selection.
                    self._passes[project_id] = 0
                    due.append(project_id)
                else:
                    self._passes[project_id] = passes - 1
        return sorted(due)

    @staticmethod
    def generation_is_current(generation: WatcherRetryGeneration | None) -> bool:
        return generation is None or generation.is_current()

    @staticmethod
    def run_for_generation(
        generation: WatcherRetryGeneration | None,
        callback: Callable[[], None],
    ) -> bool:
        if generation is None:
            callback()
            return True
        return generation.run_if_current(callback)

    def lock_for(self, project_id: str) -> threading.Lock:
        with self._lock_guard:
            return self._locks.setdefault(project_id, threading.Lock())


WatcherRetryCallback = Callable[[WatcherRetryGeneration], None]


class WatcherInitialCheckError(ValueError):
    def __init__(
        self,
        failures: list[tuple[int, WatchSpec, WatcherCheckResult]],
        results: list[WatcherCheckResult],
    ) -> None:
        self.failures = failures
        self.results = results
        detail = "; ".join(
            f"watcher {index + 1} ({spec.log_path}): {result.error or 'check failed'}"
            for index, spec, result in failures
        )
        super().__init__(detail)


def validate_graph_conditions(
    conditions: list[GraphCondition],
    state: GraphState,
) -> None:
    """Validate graph conditions against one complete canonical state."""

    if state.replay_status != "complete":
        raise ValueError("graph conditions cannot be validated while graph replay is degraded")
    for condition in conditions:
        node = state.nodes.get(condition.node_id)
        if node is None:
            raise ValueError(f"graph condition target does not exist: {condition.node_id}")
        if not isinstance(condition, NodeStatusGraphCondition):
            continue
        status_field = type(node).model_fields.get("status")
        if status_field is None:
            raise ValueError(f"graph condition target has no status: {condition.node_id}")
        status_adapter = TypeAdapter(status_field.annotation)
        invalid: list[str] = []
        for status in condition.status_in:
            try:
                status_adapter.validate_python(status)
            except ValueError:
                invalid.append(status)
        if invalid:
            raise ValueError(
                f"graph condition has invalid statuses for {condition.node_id}: "
                + ", ".join(invalid)
            )


def graph_condition_result(
    condition: GraphCondition,
    state: GraphState,
    *,
    armed_revision: int,
) -> Literal["active", "completed", "removed"]:
    """Evaluate one structurally valid condition without mutating its record."""

    if state.replay_status != "complete":
        return "active"
    node = state.nodes.get(condition.node_id)
    if node is None:
        return "removed"
    if isinstance(condition, NodeStatusGraphCondition):
        return "completed" if getattr(node, "status", None) in condition.status_in else "active"
    if isinstance(condition, ProposalResolvedGraphCondition):
        resolved = any(
            proposal.status != "pending"
            and proposal.resolved_rev is not None
            and proposal.resolved_rev > armed_revision
            and condition.node_id in proposal.related_node_ids
            for proposal in state.proposals.values()
        )
        return "completed" if resolved else "active"
    raise TypeError(f"Unsupported graph condition: {type(condition).__name__}")


def graph_condition_transition_event_result(
    condition: GraphCondition,
    lifecycle_events: Sequence[TransitionEvent],
    *,
    armed_revision: int,
    boundary_revision: int,
) -> Literal["active", "completed"]:
    """Evaluate stable events visible after a watcher's canonical arm boundary.

    A node-status event is authoritative even when a later action in the same
    transition removes the node from the final graph.  The strict revision
    comparison also keeps watchers armed after a commit from consuming that
    commit retroactively.  Proposal conditions currently have no lifecycle
    event contract and therefore continue to use final-state evaluation.
    """

    if armed_revision >= boundary_revision:
        return "active"
    if not isinstance(condition, NodeStatusGraphCondition):
        return "active"
    for event in lifecycle_events:
        if (
            event.event_type == "node_status_changed"
            and event.node_id == condition.node_id
            and event.field == "status"
            and isinstance(event.after, str)
            and event.after in condition.status_in
        ):
            return "completed"
    return "active"


def graph_check_boundary_result(
    check: WatcherCheckRecord,
    state: GraphState,
    lifecycle_events: Sequence[TransitionEvent],
) -> Literal["active", "completed", "removed"]:
    """Pure target-boundary evaluation of one graph check."""

    if check.armed_head is None or check.condition is None:
        raise ValueError("a graph check requires its condition and arming revision")
    event_result = graph_condition_transition_event_result(
        check.condition,
        lifecycle_events,
        armed_revision=check.armed_head,
        boundary_revision=state.revision,
    )
    result = (
        "completed"
        if event_result == "completed"
        else graph_condition_result(check.condition, state, armed_revision=check.armed_head)
    )
    if result != "removed" and event_result != "completed":
        try:
            validate_graph_conditions([check.condition], state)
        except ValueError as exc:
            logger.error("Stored graph check %s is semantically invalid: %s", check.check_id, exc)
            return "active"
    return result


class _AcceptedReplay(Protocol):
    state: GraphState


class _ProjectHistory(Protocol):
    def state(self) -> GraphState: ...

    def accepted_boundary_states(self) -> tuple[_AcceptedReplay, list[GraphState]]: ...

    def accepted_patch_boundaries(
        self,
    ) -> tuple[_AcceptedReplay, list[tuple[GraphState, Patch, GraphState]]]: ...

    def transition_trace_at_revision(self, revision: int) -> TransitionTrace | None: ...


class _ProjectService(Protocol):
    @property
    def history(self) -> _ProjectHistory: ...


class _TaskExecution(Protocol):
    operation_id: str
    applied_revision: int | None
    applied_graph_state: GraphState | None
    armed_graph_watchers: bool


class _ExperimentEpisodeReconciler(Protocol):
    def __call__(
        self,
        episode_id: str,
        *,
        source: str,
        operation_id: str | None = None,
    ) -> None: ...


class _GraphBoundaryReconciler(Protocol):
    def __call__(
        self,
        store: AppStore,
        project_id: str,
        boundaries: Sequence[AcceptedGraphBoundary],
        *,
        current_head: GraphHeadRef,
    ) -> None: ...


class _GraphWatcherReplayDegraded(RuntimeError):
    pass


def watcher_wake_message(ready: list[WatcherRecord]) -> str:
    """Name each ready watcher and its checks for the agent it wakes."""

    lines = []
    for watcher in ready:
        lines.append(f"- watcher `{watcher.watcher_id}`: {watcher.purpose}")
        for check in watcher.live_checks:
            detail = (
                f"graph condition `{check.condition.model_dump_json()}`"
                if check.condition is not None
                else f"external check log `{check.log_path}`"
            )
            state = "completed" if check.completed_at is not None else "unobservable"
            lines.append(f"  - {detail} ({state})")
    return (
        "RCP watcher update: the following watchers are ready. Inspect their current "
        "authoritative state and continue the Work turn.\n" + "\n".join(lines)
    )


class EpisodeWakeOwner(Protocol):
    """An episode's half of a wake: check Stop, ending, budget and session, spend, claim."""

    def __call__(
        self,
        agent_id: str,
        episode: EpisodeRecord,
        ready: list[WatcherRecord],
        fence: AdmissionFence | None,
    ) -> None: ...


class PlainWake(Protocol):
    """Admit an ordinary turn for an agent outside any episode."""

    def __call__(
        self,
        agent_id: str,
        ready: list[WatcherRecord],
        authorized_by: AuthorizedHuman,
        fence: AdmissionFence | None,
    ) -> AgentTaskRecord | None: ...


class WatcherDelivery:
    """Deliver each agent's ready watchers through one path."""

    def __init__(
        self,
        store: AppStore,
        *,
        retry: GraphWatcherRetryRegistry,
        graph_project_service: Callable[[str, GraphTargetRef], _ProjectService],
        episode_owners: Mapping[str, EpisodeWakeOwner],
        plain_wake: PlainWake,
        state_unavailable: Callable[[Exception], bool],
        task_graph_capable: Callable[[AgentTaskKind, object], bool],
        task_experiment_episode_id: Callable[[object], str | None],
        reconcile_experiment_episode: _ExperimentEpisodeReconciler,
        reconcile_graph_boundaries: _GraphBoundaryReconciler,
        logger: logging.Logger,
        launch_wait: Callable[[], float] = seconds_until_automatic_launch,
    ) -> None:
        self._store = store
        self._retry = retry
        self._graph_project_service = graph_project_service
        self._episode_owners = episode_owners
        self._plain_wake = plain_wake
        self._state_unavailable = state_unavailable
        self._task_graph_capable = task_graph_capable
        self._task_experiment_episode_id = task_experiment_episode_id
        self._reconcile_experiment_episode = reconcile_experiment_episode
        self._reconcile_graph_boundaries = reconcile_graph_boundaries
        self._logger = logger
        self._launch_wait = launch_wait

    def deliver_ready(
        self,
        agent_id: str,
        ready: list[WatcherRecord],
        *,
        retry_generation: WatcherRetryGeneration | None = None,
    ) -> None:
        """Wake one agent for its ready watchers, through its episode when it has one.

        A plain wake carries one member's authority, so a shared chat's watchers
        armed by different members wake in separate turns, oldest member first.
        """

        if not ready or not self._retry.generation_is_current(retry_generation):
            return
        if any(watcher.agent_id != agent_id for watcher in ready):
            raise ValueError("one wake delivers only its own agent's watchers")
        if self._launch_wait() > 0:
            # A wake started now would lose its link partway through its launch.
            return
        ids = [watcher.watcher_id for watcher in ready]
        refusal = self._store.chat_turn_refusal(ready[0].project_id, agent_id)
        if refusal:
            self._store.cancel_watchers(ids, by="rcp", reason=refusal)
            return
        fence = retry_generation.run_if_current if retry_generation is not None else None
        episode = self._store.agent_episode(agent_id)
        if episode is not None:
            self._episode_owners[episode.mode](agent_id, episode, ready, fence)
            return
        for group in self._store.watcher_authorizer_groups(ids):
            authorized_by = self._store.resolve_watcher_authorizer(group)
            if authorized_by is not None:
                members = set(group)
                batch = [watcher for watcher in ready if watcher.watcher_id in members]
                self._plain_wake(agent_id, batch, authorized_by, fence)
                return

    def deliver_ready_watchers(
        self,
        *,
        project_id: str | None = None,
        source: str,
        retry_generation: WatcherRetryGeneration | None = None,
    ) -> None:
        """Deliver every ready watcher, one batch per agent."""

        try:
            ready = self._store.ready_watchers()
        except Exception as exc:
            self._logger.warning("Could not read ready watchers after %s: %s", source, exc)
            return
        by_agent: dict[str, list[WatcherRecord]] = {}
        for watcher in ready:
            if project_id is None or watcher.project_id == project_id:
                by_agent.setdefault(watcher.agent_id, []).append(watcher)
        for agent_id, batch in by_agent.items():
            if not self._retry.generation_is_current(retry_generation):
                return
            try:
                self.deliver_ready(agent_id, batch, retry_generation=retry_generation)
            except Exception as exc:
                self._logger.warning(
                    "Could not deliver watchers %s after %s: %s",
                    [watcher.watcher_id for watcher in batch],
                    source,
                    exc,
                )

    def evaluate_graph_conditions_after_task(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: object,
        execution: _TaskExecution,
    ) -> None:
        episode_id = self._task_experiment_episode_id(request)
        if episode_id is not None:
            self._reconcile_experiment_episode(
                episode_id,
                source=f"task {execution.operation_id} settlement",
                operation_id=execution.operation_id,
            )
        if not self._task_graph_capable(kind, request) or (
            execution.applied_revision is None and not execution.armed_graph_watchers
        ):
            self.deliver_ready_watchers(project_id=project_id, source="task settlement")
            return
        task = self._store.agent_task(execution.operation_id)
        graph_target = task.graph_target if task is not None else GraphTargetRef()
        if kind == "branch_merge":
            from rcp.runs.branch_merge_request import BranchMergeRunRequest

            merge = BranchMergeRunRequest.model_validate(request)
            graph_target = (
                merge.merge_binding.destination if merge.merge_binding else GraphTargetRef()
            )
        self.evaluate_graph_wake_boundary(
            project_id,
            execution.applied_graph_state,
            graph_target=graph_target,
            source=("agent patch" if execution.applied_revision is not None else "watcher arming"),
        )

    def evaluate_graph_wake_boundary(
        self,
        project_id: str,
        _trigger_state: GraphState | None,
        *,
        graph_target: GraphTargetRef | None = None,
        source: str,
        retry_generation: WatcherRetryGeneration | None = None,
    ) -> None:
        """Reconcile canonical conditions independently for each graph target."""

        if not self._retry.generation_is_current(retry_generation):
            return
        retry_needed = False
        with self._retry.lock_for(project_id):
            try:
                checks = self._store.open_graph_checks(project_id)
                targets = {
                    check.graph_target.key: check.graph_target
                    for check in checks
                    if check.graph_target is not None
                }
                if graph_target is not None:
                    targets = (
                        {graph_target.key: graph_target} if graph_target.key in targets else {}
                    )
                for target in targets.values():
                    if not self._retry.generation_is_current(retry_generation):
                        return
                    try:
                        self._reconcile_graph_wake_target(project_id, target, retry_generation)
                    except Exception as exc:
                        retry_needed |= self._graph_reconciliation_retryable(exc)
                        self._logger.warning(
                            "Could not reconcile graph conditions after %s for project %s "
                            "target %s: %s",
                            source,
                            project_id,
                            target.key,
                            exc,
                        )
            except Exception as exc:
                retry_needed |= self._graph_reconciliation_retryable(exc)
                self._logger.warning(
                    "Could not reconcile graph conditions after %s for project %s: %s",
                    source,
                    project_id,
                    exc,
                )
            # Retire retries before releasing the reconciliation lock, so an
            # older success cannot clear a failure from a later pass. Only an
            # all-target pass can retire work another target scheduled.
            if retry_needed:
                if not self._retry.run_for_generation(
                    retry_generation, lambda: self._retry.schedule(project_id)
                ):
                    return
            elif graph_target is None and not self._retry.run_for_generation(
                retry_generation, lambda: self._retry.clear(project_id)
            ):
                return
        self.deliver_ready_watchers(
            project_id=project_id, source=source, retry_generation=retry_generation
        )

    def _graph_reconciliation_retryable(self, exc: Exception) -> bool:
        return (
            isinstance(exc, OSError) or self._state_unavailable(exc) or _retryable_sqlite_error(exc)
        )

    def _reconcile_graph_wake_target(
        self,
        project_id: str,
        target: GraphTargetRef,
        retry_generation: WatcherRetryGeneration | None,
    ) -> None:
        service = self._graph_project_service(project_id, target)
        replay, boundaries = service.history.accepted_boundary_states()
        if not self._retry.generation_is_current(retry_generation):
            return
        if replay.state.replay_status != "complete":
            raise _GraphWatcherReplayDegraded(
                f"{target.key} graph replay is degraded at revision {replay.state.revision}"
            )
        # The target-local durable head selects the not-yet-consumed accepted
        # boundaries; main and branch revisions never consume each other's events.
        consumed_head = self._store.graph_watcher_reconciliation_head(project_id, target)
        if consumed_head is not None and consumed_head.revision > replay.state.revision:
            raise _GraphWatcherReplayDegraded(
                f"{target.key} graph is behind its consumed watcher head "
                f"{consumed_head.revision} at revision {replay.state.revision}"
            )
        from rcp.runs.transition_event_reconciliation import AcceptedGraphBoundary

        accepted_boundaries: list[AcceptedGraphBoundary] = []
        for boundary in boundaries:
            if consumed_head is not None and boundary.revision < consumed_head.revision:
                continue
            trace = service.history.transition_trace_at_revision(boundary.revision)
            if trace is not None and trace.pre_head.target != target:
                raise _GraphWatcherReplayDegraded(
                    "accepted transition target does not match its history view"
                )
            accepted_boundaries.append(
                AcceptedGraphBoundary(
                    target=target,
                    revision=boundary.revision,
                    transition_id=(trace.transition_id if trace is not None else None),
                    state=boundary,
                    lifecycle_events=tuple(trace.lifecycle_events) if trace is not None else (),
                )
            )
        if not self._retry.generation_is_current(retry_generation):
            return
        self._reconcile_graph_boundaries(
            self._store,
            project_id,
            accepted_boundaries,
            current_head=GraphHeadRef(target=target, revision=replay.state.revision),
        )

    def request_startup_sweep(self) -> None:
        """Queue every watcher project for the retry worker's first pass.

        Conditions satisfied while RCP was down are evaluated right after
        startup, on the retry worker, never on the API readiness path.
        """
        for project_id in self._store.graph_watcher_project_ids():
            self._retry.request(project_id)

    def retry_graph_wakes_after_poll(self, generation: WatcherRetryGeneration) -> None:
        for project_id in self._retry.due():
            if not generation.is_current():
                return
            self.evaluate_graph_wake_boundary(
                project_id,
                None,
                source="reconciliation retry",
                retry_generation=generation,
            )


def _retryable_sqlite_error(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _run_watcher_command(
    shell_command: str, cwd: str, host: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Use the same bounded shell and remote watchdog for checks and human actions."""

    if host:
        command = ssh_arguments(
            host,
            shlex.join(
                [
                    "python3",
                    "-c",
                    _remote_script("watcher_process.py"),
                    cwd,
                    str(timeout),
                    shell_command,
                ]
            ),
        )
        process_cwd = None
    else:
        command = watcher_shell_command(shell_command, cwd)
        process_cwd = cwd
    return run_check_process(command, cwd=process_cwd, timeout=timeout)


def run_watcher_check(
    spec: WatchSpec,
    host: str = "",
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> WatcherCheckResult:
    """Ask a watcher from a fresh login shell without interpreting its command."""

    try:
        result = _run_watcher_command(spec.check_command, spec.cwd, host, timeout)
    except subprocess.TimeoutExpired as exc:
        detail = _process_error_output(exc.stderr, exc.stdout)
        message = f"check timed out after {timeout:g} seconds"
        if detail:
            message = f"{message}: {detail}"
        return WatcherCheckResult(state="error", checked_at=_now(), error=message)
    except OSError as exc:
        return WatcherCheckResult(
            state="error",
            checked_at=_now(),
            error=_bounded_error(f"could not execute check: {exc}"),
        )

    if result.returncode == 0:
        return WatcherCheckResult(
            state="complete",
            checked_at=_now(),
            exit_code=result.returncode,
        )
    if result.returncode == 1:
        return WatcherCheckResult(
            state="active",
            checked_at=_now(),
            exit_code=result.returncode,
        )
    detail = _process_error_output(result.stderr, result.stdout)
    message = f"check exited with status {result.returncode}"
    if detail:
        message = f"{message}: {detail}"
    return WatcherCheckResult(
        state="error",
        checked_at=_now(),
        exit_code=result.returncode,
        error=_bounded_error(message),
    )


def cancel_watched_job(
    store: AppStore,
    project_id: str,
    check_id: str,
    requested_by: str,
    *,
    runner: Callable[[WatchSpec, str, float], str | None] | None = None,
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> WatcherRecord:
    """Run a live check's saved Cancel command for a member, then end its watcher.

    This is job control: the agent's own command stops the job. A Cancel that
    fails leaves the watcher live, showing the error, and can be tried again.
    """

    watcher = store.watcher_of_check(check_id)
    if watcher is None or watcher.project_id != project_id:
        raise KeyError(check_id)
    claimed = store.claim_job_cancel(check_id, requested_by)
    if claimed is None:
        raise ValueError("This job has no Cancel to run now.")
    assert claimed.check_command and claimed.log_path and claimed.cwd
    spec = WatchSpec(
        check_command=claimed.check_command,
        log_path=claimed.log_path,
        cwd=claimed.cwd,
        cancel_command=claimed.cancel_command,
    )
    try:
        error = (runner or run_watcher_cancel)(spec, host_of(store, claimed.machine_id), timeout)
    except Exception as exc:
        error = _bounded_error(f"Cancel command failed: {exc}")
    store.finish_job_cancel(claimed, error)
    current = store.watcher(watcher.watcher_id)
    assert current is not None
    return current


def run_watcher_cancel(
    spec: WatchSpec,
    host: str = "",
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> str | None:
    """Run only a human-requested saved action; exit zero is command acceptance."""

    if spec.cancel_command is None:
        raise ValueError("This watcher has no cancel command.")
    try:
        result = _run_watcher_command(spec.cancel_command, spec.cwd, host, timeout)
    except subprocess.TimeoutExpired as exc:
        detail = _process_error_output(exc.stderr, exc.stdout)
        return _bounded_error(
            f"Cancel command timed out after {timeout:g} seconds; its outcome is unknown."
            + (f" {detail}" if detail else "")
        )
    except OSError as exc:
        return _bounded_error(f"Could not execute Cancel command: {exc}")
    if result.returncode == 0:
        return None
    detail = _process_error_output(result.stderr, result.stdout)
    return _bounded_error(
        f"Cancel command exited with status {result.returncode}." + (f" {detail}" if detail else "")
    )


def validate_watch_specs(
    specs: list[WatchSpec],
    host: str,
    *,
    check_runner: WatcherCheckRunner = run_watcher_check,
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> list[WatcherCheckResult]:
    """Run every initial check; any error rejects the entire list."""

    if not specs:
        raise ValueError("a watch list must contain at least one watcher")
    results = [check_runner(spec, host, timeout) for spec in specs]
    failures = [
        (index, specs[index], result)
        for index, result in enumerate(results)
        if result.state == "error"
    ]
    if failures:
        raise WatcherInitialCheckError(failures, results)
    return results


def external_check(
    check_id: str,
    spec: WatchSpec,
    result: WatcherCheckResult,
    *,
    machine_id: str,
    created_at: str,
) -> WatcherCheckRecord:
    """A new external check carrying its first observation."""

    completed = result.state == "complete"
    return WatcherCheckRecord(
        check_id=check_id,
        watcher_id=check_id,
        machine_id=machine_id,
        definition="external",
        check_command=spec.check_command,
        log_path=spec.log_path,
        cwd=spec.cwd,
        cancel_command=spec.cancel_command,
        created_at=created_at,
        last_checked_at=result.checked_at,
        last_exit_code=result.exit_code,
        completed_at=result.checked_at if completed else None,
    )


def graph_check(
    check_id: str,
    condition: GraphCondition,
    state: GraphState,
    *,
    graph_target: GraphTargetRef,
    machine_id: str,
    created_at: str,
) -> WatcherCheckRecord:
    """A new graph check armed at the state's revision."""

    result = graph_condition_result(condition, state, armed_revision=state.revision)
    if result == "removed":
        raise ValueError(f"graph condition target does not exist: {condition.node_id}")
    return WatcherCheckRecord(
        check_id=check_id,
        watcher_id=check_id,
        machine_id=machine_id,
        definition="graph",
        condition=condition,
        graph_target=graph_target,
        armed_head=state.revision,
        created_at=created_at,
        last_checked_at=created_at,
        completed_at=created_at if result == "completed" else None,
    )


def _new_watcher(
    binding: WatcherBinding,
    watcher_id: str,
    purpose: str,
    check: WatcherCheckRecord,
    created_at: str,
) -> WatcherRecord:
    return WatcherRecord(
        watcher_id=watcher_id,
        agent_id=binding.agent_id,
        project_id=binding.project_id,
        purpose=purpose,
        origin_turn_id=binding.origin_turn_id,
        created_at=created_at,
        checks=[check],
    )


class WatcherPoller:
    """Small process-owned polling loop over durable external checks."""

    def __init__(
        self,
        store: AppStore,
        *,
        on_poll_completed: WatcherPollCompletedCallback | None = None,
        check_runner: WatcherCheckRunner = run_watcher_check,
        timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
        interval: float = WATCHER_POLL_INTERVAL_SECONDS,
        workers: int = WATCHER_CHECK_WORKERS,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.on_poll_completed = on_poll_completed
        self.check_runner = check_runner
        self.timeout = timeout
        self.interval = interval
        self.workers = max(1, workers)
        self.clock = clock or store.now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rcp-watchers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.timeout + 1)
            if not thread.is_alive():
                self._thread = None

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll_once(self) -> None:
        """Check every due external check, then hand off to delivery."""

        with self._poll_lock:
            self._check(self.store.due_external_checks(as_of=self.clock()))
            if self.on_poll_completed is not None:
                try:
                    self.on_poll_completed()
                except Exception:
                    logger.exception("Watcher poll-completed callback failed")

    def _check_one(self, check: WatcherCheckRecord) -> WatcherCheckResult:
        return self.check_runner(
            _spec_from_check(check), host_of(self.store, check.machine_id), self.timeout
        )

    def _check(self, checks: list[WatcherCheckRecord]) -> None:
        if not checks:
            return
        with ThreadPoolExecutor(max_workers=min(self.workers, len(checks))) as executor:
            futures = {executor.submit(self._check_one, check): check for check in checks}
            for future in as_completed(futures):
                check = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # a runner failure is an error, not completion
                    result = WatcherCheckResult(
                        state="error",
                        checked_at=_now(),
                        error=_bounded_error(f"watcher check failed: {exc}"),
                    )
                self.store.record_watcher_check(
                    check.check_id,
                    status="degraded"
                    if result.state == "error"
                    else "completed"
                    if result.state == "complete"
                    else "active",
                    exit_code=result.exit_code,
                    error=result.error,
                    checked_at=result.checked_at,
                )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Watcher polling pass failed")
            self._stop.wait(self.interval)


def _spec_from_check(check: WatcherCheckRecord) -> WatchSpec:
    assert check.check_command and check.log_path and check.cwd
    return WatchSpec(check_command=check.check_command, log_path=check.log_path, cwd=check.cwd)


class WatcherRetryWorker:
    """Coalesce poll-pass signals onto generation-scoped retry threads."""

    def __init__(self, callback: WatcherRetryCallback) -> None:
        self.callback = callback
        self._lifecycle_lock = threading.Lock()
        self._generation = 0
        self._accepting = False
        self._pending: threading.Event | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._accepting and self._thread is not None and self._thread.is_alive():
                return
            old_stop = self._stop
            old_pending = self._pending
            if old_stop is not None:
                old_stop.set()
            if old_pending is not None:
                old_pending.set()

            self._generation += 1
            generation = self._generation
            pending = threading.Event()
            stop = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(generation, pending, stop),
                name=f"rcp-graph-watcher-retries-{generation}",
                daemon=True,
            )
            self._accepting = True
            self._pending = pending
            self._stop = stop
            self._thread = thread
            thread.start()

    def signal(self) -> None:
        with self._lifecycle_lock:
            pending = self._pending if self._accepting else None
        if pending is not None:
            pending.set()

    def stop(self, *, timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS + 1) -> None:
        with self._lifecycle_lock:
            self._accepting = False
            self._generation += 1
            stop = self._stop
            pending = self._pending
            thread = self._thread
            if stop is not None:
                stop.set()
            if pending is not None:
                pending.set()
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        with self._lifecycle_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
                self._pending = None
                self._stop = None

    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def _run(
        self,
        generation: int,
        pending: threading.Event,
        stop: threading.Event,
    ) -> None:
        def is_current() -> bool:
            with self._lifecycle_lock:
                return self._accepting and self._generation == generation

        def run_if_current(callback: Callable[[], None]) -> bool:
            with self._lifecycle_lock:
                if not self._accepting or self._generation != generation:
                    return False
                callback()
                return True

        lease = WatcherRetryGeneration(is_current, run_if_current)

        while True:
            pending.wait()
            pending.clear()
            if stop.is_set() or not is_current():
                return
            try:
                self.callback(lease)
            except Exception:
                logger.exception("Graph watcher ready-delivery retry failed")


def _process_error_output(stderr: object, stdout: object) -> str:
    for value in (stderr, stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value.strip():
            lines = [
                line
                for line in value.splitlines()
                if line.strip() != "logout"
                and not any(noise in line for noise in _LOGIN_SHELL_NOISE)
            ]
            detail = "\n".join(lines).strip()
            if detail:
                return _bounded_error(detail)
    return ""


def _bounded_error(value: str) -> str:
    return value[:WATCHER_ERROR_MAX_CHARS]


def _now() -> str:
    return datetime.now(UTC).isoformat()
