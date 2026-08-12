from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from rcp.core.models import GraphState
from rcp.limits import (
    WATCHER_CHECK_TIMEOUT_SECONDS,
    WATCHER_CHECK_WORKERS,
    WATCHER_ERROR_MAX_CHARS,
    WATCHER_POLL_INTERVAL_SECONDS,
)
from rcp.storage import (
    AppStore,
    GraphCondition,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    ProposalResolvedGraphCondition,
    StoredWatcherRecord,
    WatcherContinuation,
    WatcherRecord,
    WatcherStopRequest,
)
from rcp.transport.ssh import ssh_arguments

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

    @field_validator("check_command")
    @classmethod
    def check_command_is_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("check_command must not be blank")
        return stripped

    @field_validator("log_path", "cwd")
    @classmethod
    def paths_are_absolute(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute():
            raise ValueError("watcher paths must be absolute")
        return value


class WatchHandoff(BaseModel):
    """One all-or-none watcher declaration with two closed condition kinds."""

    model_config = ConfigDict(extra="forbid")

    external: list[WatchSpec]
    graph: list[GraphCondition]

    @property
    def is_empty(self) -> bool:
        return not self.external and not self.graph


class ExperimentWatchSpec(WatchSpec):
    """An Experiment observer may opt into one immutable delivery group."""

    group: str | None = None

    @field_validator("group")
    @classmethod
    def group_is_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("group must not be blank")
        return stripped


class ExperimentWatchHandoff(BaseModel):
    """The Experiment-only mixed observer/retirement watcher file."""

    observers: list[ExperimentWatchSpec]
    graph_conditions: list[GraphCondition]
    stops: list[WatcherStopRequest]

    @property
    def is_empty(self) -> bool:
        return not self.observers and not self.graph_conditions and not self.stops


class WatcherBinding(BaseModel):
    """Identity and authority RCP binds from the originating operation."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    origin_operation_id: str
    origin_task_kind: Literal["node_chat", "project_chat", "campaign"]
    chat_id: str
    node_id: str | None = None
    execution_host: str = ""
    continuation: WatcherContinuation


class WatcherCheckResult(BaseModel):
    state: Literal["active", "complete", "error"]
    checked_at: str
    exit_code: int | None = None
    error: str | None = None


WatcherCheckRunner = Callable[[WatchSpec, str, float], WatcherCheckResult]
WatcherCompletionCallback = Callable[[list[WatcherRecord]], None]
WatcherPollCompletedCallback = Callable[[], None]


class WatcherRetryGeneration:
    """A retry pass lease that serializes its final side effect with stop()."""

    def __init__(
        self,
        is_current: Callable[[], bool],
        run_if_current: Callable[[Callable[[], None]], bool],
    ) -> None:
        self.is_current = is_current
        self.run_if_current = run_if_current


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


def parse_watch_json(payload: str) -> WatchHandoff:
    handoff = WatchHandoff.model_validate_json(payload)
    if handoff.is_empty:
        raise ValueError("a watch handoff must contain at least one watcher")
    _validate_unique_graph_conditions(handoff.graph)
    return handoff


def parse_experiment_watch_json(payload: str) -> ExperimentWatchHandoff:
    """Parse the one Experiment-only watcher handoff without loosening Work."""

    raw = json.loads(payload)
    if not isinstance(raw, dict) or set(raw) != {"external", "graph"}:
        raise ValueError("Experiment watch.json must contain exactly the external and graph lists")
    external = raw["external"]
    graph = raw["graph"]
    if not isinstance(external, list) or not isinstance(graph, list):
        raise ValueError("Experiment watch.json external and graph values must be lists")
    observers: list[ExperimentWatchSpec] = []
    stops: list[WatcherStopRequest] = []
    for item in external:
        if not isinstance(item, dict):
            raise ValueError("Experiment watch.json items must be objects")
        if "stop_watcher_id" in item or "reason" in item:
            stops.append(WatcherStopRequest.model_validate(item))
        else:
            observers.append(ExperimentWatchSpec.model_validate(item))
    stop_ids = [item.stop_watcher_id for item in stops]
    if len(stop_ids) != len(set(stop_ids)):
        raise ValueError("Experiment watcher stop ids must be unique")
    group_sizes: dict[str, int] = {}
    for observer in observers:
        if observer.group is not None:
            group_sizes[observer.group] = group_sizes.get(observer.group, 0) + 1
    undersized = sorted(label for label, count in group_sizes.items() if count < 2)
    if undersized:
        raise ValueError(
            "an Experiment watcher group requires at least two observers: " + ", ".join(undersized)
        )
    graph_conditions = TypeAdapter(list[GraphCondition]).validate_python(graph)
    _validate_unique_graph_conditions(graph_conditions)
    return ExperimentWatchHandoff(
        observers=observers,
        graph_conditions=graph_conditions,
        stops=stops,
    )


def _validate_unique_graph_conditions(conditions: list[GraphCondition]) -> None:
    identities = [item.model_dump_json() for item in conditions]
    if len(identities) != len(set(identities)):
        raise ValueError("a watch handoff cannot repeat a graph condition")


def validate_graph_conditions(
    conditions: list[GraphCondition],
    state: GraphState,
) -> None:
    """Validate graph conditions against one complete canonical state."""

    _validate_unique_graph_conditions(conditions)
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


def evaluate_graph_watchers(
    store: AppStore,
    project_id: str,
    state: GraphState,
) -> list[list[StoredWatcherRecord]]:
    """Evaluate one project's graph watchers and return coalesced ready deliveries.

    The caller supplies canonical state at a revision boundary or startup. A
    degraded replay is deliberately a no-op. Completed external observers are
    included by the store's existing grouping policy, so compatible conditions
    that become ready together share one wake.
    """

    if state.replay_status != "complete":
        return ready_graph_watcher_groups(store, project_id)
    evaluated_at = store.now()
    for record in store.active_graph_watchers(project_id):
        if record.armed_revision is None:
            store.initialize_graph_watcher_baseline(
                record.watcher_id,
                armed_revision=state.revision,
                evaluated_at=evaluated_at,
            )
            continue
        if record.armed_revision >= state.revision:
            continue
        result = graph_condition_result(
            record.condition,
            state,
            armed_revision=record.armed_revision,
        )
        if result != "removed":
            try:
                validate_graph_conditions([record.condition], state)
            except ValueError as exc:
                logger.error(
                    "Stored graph watcher %s is semantically invalid: %s",
                    record.watcher_id,
                    exc,
                )
                result = "active"
        store.record_graph_watcher_result(
            record.watcher_id,
            result=result,
            evaluated_at=evaluated_at,
        )
    return ready_graph_watcher_groups(store, project_id)


def ready_graph_watcher_groups(
    store: AppStore,
    project_id: str,
) -> list[list[StoredWatcherRecord]]:
    """Return ready groups containing graph rows without evaluating conditions."""

    return [
        group
        for group in store.completed_watcher_groups()
        if group
        and group[0].project_id == project_id
        and any(isinstance(record, GraphWatcherRecord) for record in group)
    ]


def run_watcher_check(
    spec: WatchSpec,
    execution_host: str = "",
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> WatcherCheckResult:
    """Ask a watcher from a fresh login shell without interpreting its command."""

    if execution_host:
        payload = f"cd {shlex.quote(spec.cwd)} && {spec.check_command}"
        command = ssh_arguments(
            execution_host,
            shlex.join(["bash", "-lic", payload]),
        )
        cwd = None
    else:
        command = ["bash", "-lic", spec.check_command]
        cwd = spec.cwd
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
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


def validate_watch_specs(
    specs: list[WatchSpec],
    execution_host: str,
    *,
    check_runner: WatcherCheckRunner = run_watcher_check,
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> list[WatcherCheckResult]:
    """Run every initial check; any error rejects the entire list."""

    if not specs:
        raise ValueError("a watch list must contain at least one watcher")
    results = [check_runner(spec, execution_host, timeout) for spec in specs]
    failures = [
        (index, specs[index], result)
        for index, result in enumerate(results)
        if result.state == "error"
    ]
    if failures:
        raise WatcherInitialCheckError(failures, results)
    return results


def arm_watchers(
    store: AppStore,
    specs: list[WatchSpec],
    binding: WatcherBinding,
    *,
    graph_conditions: list[GraphCondition] | None = None,
    state: GraphState | None = None,
    watcher_ids: list[str] | None = None,
    check_runner: WatcherCheckRunner = run_watcher_check,
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> list[StoredWatcherRecord]:
    """Validate one mixed handoff and persist all of it, or persist none of it."""

    conditions = list(graph_conditions or [])
    if not specs and not conditions:
        raise ValueError("a watch list must contain at least one watcher")
    watcher_count = len(specs) + len(conditions)
    if watcher_ids is None:
        resolved_watcher_ids = [str(uuid4()) for _ in range(watcher_count)]
    else:
        if len(watcher_ids) != watcher_count:
            raise ValueError("watcher_ids must match the mixed watcher handoff exactly")
        if any(
            not isinstance(watcher_id, str) or not watcher_id.strip() for watcher_id in watcher_ids
        ):
            raise ValueError("watcher_ids must contain only nonblank strings")
        if len(watcher_ids) != len(set(watcher_ids)):
            raise ValueError("watcher_ids must be unique")
        resolved_watcher_ids = list(watcher_ids)
    if conditions:
        if state is None:
            raise ValueError("graph watcher arming requires canonical graph state")
        validate_graph_conditions(conditions, state)
    results = (
        validate_watch_specs(
            specs,
            binding.execution_host,
            check_runner=check_runner,
            timeout=timeout,
        )
        if specs
        else []
    )
    created_at = _now()
    records: list[StoredWatcherRecord] = []
    for watcher_id, spec, result in zip(
        resolved_watcher_ids[: len(specs)],
        specs,
        results,
        strict=True,
    ):
        completed = result.state == "complete"
        records.append(
            WatcherRecord(
                watcher_id=watcher_id,
                project_id=binding.project_id,
                origin_operation_id=binding.origin_operation_id,
                origin_task_kind=binding.origin_task_kind,
                chat_id=binding.chat_id,
                node_id=binding.node_id,
                execution_host=binding.execution_host,
                check_command=spec.check_command,
                log_path=spec.log_path,
                cwd=spec.cwd,
                continuation=binding.continuation,
                status="completed" if completed else "active",
                created_at=created_at,
                last_checked_at=result.checked_at,
                last_exit_code=result.exit_code,
                completed_at=result.checked_at if completed else None,
            )
        )
    if state is not None:
        for watcher_id, condition in zip(
            resolved_watcher_ids[len(specs) :],
            conditions,
            strict=True,
        ):
            result = graph_condition_result(
                condition,
                state,
                armed_revision=state.revision,
            )
            if result == "removed":
                raise ValueError(f"graph condition target does not exist: {condition.node_id}")
            completed = result == "completed"
            records.append(
                GraphWatcherRecord(
                    watcher_id=watcher_id,
                    project_id=binding.project_id,
                    origin_operation_id=binding.origin_operation_id,
                    origin_task_kind=binding.origin_task_kind,
                    chat_id=binding.chat_id,
                    node_id=binding.node_id,
                    execution_host=binding.execution_host,
                    condition=condition,
                    armed_revision=state.revision,
                    continuation=binding.continuation,
                    status="completed" if completed else "active",
                    created_at=created_at,
                    last_evaluated_at=created_at,
                    completed_at=created_at if completed else None,
                )
            )
    return store.create_watchers(records)


class WatcherPoller:
    """Small process-owned polling loop over durable watcher rows."""

    def __init__(
        self,
        store: AppStore,
        *,
        on_completed: WatcherCompletionCallback | None = None,
        on_poll_completed: WatcherPollCompletedCallback | None = None,
        check_runner: WatcherCheckRunner = run_watcher_check,
        timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
        interval: float = WATCHER_POLL_INTERVAL_SECONDS,
        workers: int = WATCHER_CHECK_WORKERS,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.on_completed = on_completed
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

    def poll_once(self) -> list[list[WatcherRecord]]:
        with self._poll_lock:
            records = self.store.pollable_watchers(as_of=self.clock())
            self._check_records(records)
            return self._finish_poll()

    def check_now(self, project_id: str, watcher_id: str) -> WatcherRecord:
        """Check one degraded external watcher through the ordinary poll path."""

        with self._poll_lock:
            record = self.store.watcher(watcher_id)
            if record is None or record.project_id != project_id:
                raise KeyError(watcher_id)
            if not isinstance(record, WatcherRecord):
                raise ValueError("Only an external watcher can be checked now.")
            if record.status != "degraded" or record.notified:
                raise ValueError("Only a degraded watcher awaiting delivery can be checked now.")
            self._check_records([record])
            self._finish_poll()
            updated = self.store.watcher(watcher_id)
            if not isinstance(updated, WatcherRecord):
                raise RuntimeError("External watcher changed type during its check.")
            return updated

    def _check_records(self, records: list[WatcherRecord]) -> None:
        if records:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(records))) as executor:
                futures = {
                    executor.submit(
                        self.check_runner,
                        _spec_from_record(record),
                        record.execution_host,
                        self.timeout,
                    ): record
                    for record in records
                }
                for future in as_completed(futures):
                    record = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # a runner failure is a degraded check, not completion
                        result = WatcherCheckResult(
                            state="error",
                            checked_at=_now(),
                            error=_bounded_error(f"watcher check failed: {exc}"),
                        )
                    status = {
                        "active": "active",
                        "complete": "completed",
                        "error": "degraded",
                    }[result.state]
                    self.store.record_watcher_check(
                        record.watcher_id,
                        status=status,
                        exit_code=result.exit_code,
                        error=result.error,
                        checked_at=result.checked_at,
                    )

    def _finish_poll(self) -> list[list[WatcherRecord]]:
        groups: list[list[WatcherRecord]] = []
        for group in self.store.completed_watcher_groups():
            if any(isinstance(item, GraphWatcherRecord) for item in group):
                continue
            external = [item for item in group if isinstance(item, WatcherRecord)]
            if external:
                groups.append(external)
        if self.on_completed is not None:
            for group in groups:
                try:
                    self.on_completed(group)
                except Exception:
                    logger.exception(
                        "Watcher completion callback failed for %s",
                        [record.watcher_id for record in group],
                    )
        if self.on_poll_completed is not None:
            try:
                self.on_poll_completed()
            except Exception:
                logger.exception("Watcher poll-completed callback failed")
        return groups

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Watcher polling pass failed")
            self._stop.wait(self.interval)


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


def _spec_from_record(record: WatcherRecord) -> WatchSpec:
    return WatchSpec(
        check_command=record.check_command,
        log_path=record.log_path,
        cwd=record.cwd,
    )


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
