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

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from rcp.limits import (
    WATCHER_CHECK_TIMEOUT_SECONDS,
    WATCHER_CHECK_WORKERS,
    WATCHER_ERROR_MAX_CHARS,
    WATCHER_POLL_INTERVAL_SECONDS,
)
from rcp.storage import AppStore, WatcherContinuation, WatcherRecord, WatcherStopRequest
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


class WatchList(RootModel[list[WatchSpec]]):
    root: list[WatchSpec] = Field(min_length=1)


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
    stops: list[WatcherStopRequest]

    @property
    def is_empty(self) -> bool:
        return not self.observers and not self.stops


class WatcherBinding(BaseModel):
    """Identity and authority RCP binds from the originating operation."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    origin_operation_id: str
    origin_task_kind: Literal["node_chat", "project_chat"]
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


def parse_watch_json(payload: str) -> list[WatchSpec]:
    return WatchList.model_validate_json(payload).root


def parse_experiment_watch_json(payload: str) -> ExperimentWatchHandoff:
    """Parse the one Experiment-only watcher handoff without loosening Work."""

    raw = json.loads(payload)
    if not isinstance(raw, list):
        raise ValueError("Experiment watch.json must contain a JSON list")
    observers: list[ExperimentWatchSpec] = []
    stops: list[WatcherStopRequest] = []
    for item in raw:
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
    return ExperimentWatchHandoff(observers=observers, stops=stops)


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
    check_runner: WatcherCheckRunner = run_watcher_check,
    timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
) -> list[WatcherRecord]:
    """Validate one list and persist all of it, or persist none of it."""

    results = validate_watch_specs(
        specs,
        binding.execution_host,
        check_runner=check_runner,
        timeout=timeout,
    )
    created_at = _now()
    records = []
    for spec, result in zip(specs, results, strict=True):
        completed = result.state == "complete"
        records.append(
            WatcherRecord(
                watcher_id=str(uuid4()),
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
    return store.create_watchers(records)


class WatcherPoller:
    """Small process-owned polling loop over durable watcher rows."""

    def __init__(
        self,
        store: AppStore,
        *,
        on_completed: WatcherCompletionCallback | None = None,
        check_runner: WatcherCheckRunner = run_watcher_check,
        timeout: float = WATCHER_CHECK_TIMEOUT_SECONDS,
        interval: float = WATCHER_POLL_INTERVAL_SECONDS,
        workers: int = WATCHER_CHECK_WORKERS,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.on_completed = on_completed
        self.check_runner = check_runner
        self.timeout = timeout
        self.interval = interval
        self.workers = max(1, workers)
        self.clock = clock or store.now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        records = self.store.pollable_watchers(as_of=self.clock())
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

        groups = self.store.completed_watcher_groups()
        if self.on_completed is not None:
            for group in groups:
                try:
                    self.on_completed(group)
                except Exception:
                    logger.exception(
                        "Watcher completion callback failed for %s",
                        [record.watcher_id for record in group],
                    )
        return groups

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Watcher polling pass failed")
            self._stop.wait(self.interval)


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
