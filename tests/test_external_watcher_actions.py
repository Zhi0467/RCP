from __future__ import annotations

import json
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.storage import AppStore
from rcp.watchers import (
    WatcherCheckResult,
    WatcherPoller,
    WatchSpec,
    arm_watchers,
    parse_experiment_watch_json,
    parse_watch_json,
    run_watcher_cancel,
)

from .test_watchers import _binding, _record


def test_probe_storage_is_latest_per_project_and_machine(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    probe = ComputeBackendProbe(
        execution_machine="local",
        backend_id="systemd_user",
        state="ready",
        ready=True,
        diagnostic="Ready",
        containment="cooperative",
        status_label="Ready",
        status_tone="ready",
    )
    assert store.compute_backend_probe("project", "local") is None
    store.record_compute_backend_probe("project", probe)
    failed = probe.model_copy(update={"ready": False, "state": "failed", "diagnostic": "Offline"})
    store.record_compute_backend_probe("project", failed)
    reopened = AppStore(store.path)
    assert reopened.compute_backend_probe("project", "local") == failed
    assert reopened.compute_backend_probe("other", "local") is None
    assert reopened.compute_backend_probe("project", "other") is None
    with reopened.connection() as connection:
        rows = connection.execute("SELECT probed_at FROM compute_backend_probes").fetchall()
    assert len(rows) == 1 and rows[0][0]


@pytest.mark.parametrize("parser", [parse_watch_json, parse_experiment_watch_json])
@pytest.mark.parametrize("cancel_command", [None, "scancel 1234"])
def test_one_shell_contract_preserves_optional_human_action(parser, cancel_command):
    item = {"check_command": "false", "log_path": "/logs/1234", "cwd": "/work"}
    if cancel_command is not None:
        item["cancel_command"] = cancel_command
    handoff = parser(json.dumps({"external": [item], "graph": []}))
    specs = getattr(handoff, "external", None) or handoff.observers
    assert specs[0].cancel_command == cancel_command
    assert specs[0].check_command == "false"


@pytest.mark.parametrize("parser", [parse_watch_json, parse_experiment_watch_json])
@pytest.mark.parametrize(
    "item",
    [
        {"job_id": "rcp-job"},
        {"check_command": "false", "log_path": "/log", "cwd": "/work", "job_id": "rcp-job"},
        {"check_command": "false", "log_path": "/log", "cwd": "/work", "cancel_command": " "},
        {"check_command": "false", "log_path": "/log", "cancel_command": "scancel 1"},
    ],
)
def test_no_native_job_observer_or_partial_shell_action(item, parser):
    with pytest.raises((ValueError, ValidationError)):
        parser(json.dumps({"external": [item], "graph": []}))


def _active(*_):
    return WatcherCheckResult(state="active", checked_at=AppStore.now(), exit_code=1)


def _cancel_record(store, *, host="", command="true", cwd="/tmp"):
    record = _record("cancel-work").model_copy(
        update={"cancel_command": command, "execution_host": host, "cwd": cwd}
    )
    store.create_watchers([record])
    return record


def test_shell_action_survives_restart_and_runs_in_saved_working_directory(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    running = tmp_path / "running"
    running.touch()
    spec = WatchSpec(
        check_command="test ! -e running",
        log_path=str(tmp_path / "output.log"),
        cwd=str(tmp_path),
        cancel_command="rm -- running",
    )
    record = arm_watchers(store, [spec], _binding())[0]
    reopened = AppStore(store.path)
    poller = WatcherPoller(reopened, clock=lambda: "9999-01-01T00:00:00Z")
    requested = poller.cancel("project", record.watcher_id, "human-one")
    assert not running.exists()
    assert requested.status == "active"  # command acceptance is not job completion
    assert requested.cancel_requested_by == "human-one"
    assert requested.cancel_requested_at and requested.cancel_error is None
    assert not requested.can_cancel
    poller.poll_once()
    assert reopened.watcher(record.watcher_id).status == "completed"


def test_saved_remote_execution_identity_is_used_for_check_and_cancel(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store, host="rcp@cluster", cwd="/scratch/rcp", command="scancel 381")
    calls = []

    def check(spec, host, timeout):
        calls.append(("check", host, spec.cwd, timeout))
        return _active()

    def cancel(spec, host, timeout):
        calls.append((spec.cancel_command, host, spec.cwd, timeout))
        return None

    poller = WatcherPoller(store, check_runner=check, cancel_runner=cancel)
    poller.cancel("project", record.watcher_id, "human-one")
    assert calls == [
        ("check", "rcp@cluster", "/scratch/rcp", poller.timeout),
        ("scancel 381", "rcp@cluster", "/scratch/rcp", poller.timeout),
    ]


def test_cancel_failure_preserves_diagnostic_and_allows_explicit_retry(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store, command="printf 'scheduler unavailable' >&2; exit 1")
    poller = WatcherPoller(store, check_runner=_active)
    failed = poller.cancel("project", record.watcher_id, "human-one")
    assert "scheduler unavailable" in failed.cancel_error
    assert "status 1" in failed.cancel_error
    assert failed.can_cancel and failed.status == "active"
    poller.cancel_runner = lambda *_: None
    retried = poller.cancel("project", record.watcher_id, "human-two")
    assert retried.cancel_requested_by == "human-two"
    assert retried.cancel_error is None and not retried.can_cancel


def test_concurrent_cancel_requests_execute_only_once(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def cancel(*_):
        calls.append("cancel")
        entered.set()
        assert release.wait(5)
        return None

    first = WatcherPoller(store, check_runner=_active, cancel_runner=cancel)
    second = WatcherPoller(AppStore(store.path), check_runner=_active, cancel_runner=cancel)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.cancel, "project", record.watcher_id, "human-one")
        assert entered.wait(5)
        two = pool.submit(second.cancel, "project", record.watcher_id, "human-two")
        duplicate = two.result(timeout=5)
        assert duplicate.cancel_requested_by == "human-one"
        release.set()
        one.result(timeout=5)
    assert calls == ["cancel"]


@pytest.mark.parametrize("stopped", [False, True])
def test_completed_work_is_not_attributed_to_cancel(tmp_path, stopped):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store)
    if stopped:
        store.stop_watchers("project", [record.watcher_id])
    poller = WatcherPoller(
        store,
        check_runner=lambda *_: WatcherCheckResult(
            state="complete", checked_at=store.now(), exit_code=0
        ),
        cancel_runner=lambda *_: pytest.fail("already completed work must not be cancelled"),
    )
    observed = poller.cancel("project", record.watcher_id, "human-one")
    assert observed.completed_at
    assert observed.status == ("stopped" if stopped else "completed")
    assert observed.cancel_requested_at is observed.cancel_requested_by is None
    assert not observed.can_cancel


def test_stopped_watcher_can_cancel_work_without_reopening_continuation(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store)
    stopped = store.stop_watchers("project", [record.watcher_id])[0]
    calls = []
    poller = WatcherPoller(store, check_runner=_active, cancel_runner=lambda *_: calls.append(1))
    assert stopped.can_cancel
    result = poller.cancel("project", record.watcher_id, "human-one")
    assert calls == [1]
    assert result.status == "stopped" and result.notified
    assert result.next_check_at is None
    assert store.pollable_watchers() == []
    assert store.completed_watcher_groups() == []


def test_failed_fresh_check_refuses_cancellation_without_attribution(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store)
    poller = WatcherPoller(
        store,
        check_runner=lambda *_: WatcherCheckResult(
            state="error", checked_at=store.now(), error="SSH unreachable"
        ),
        cancel_runner=lambda *_: pytest.fail("unknown liveness must not run Cancel"),
    )
    result = poller.cancel("project", record.watcher_id, "human-one")
    assert result.status == "degraded"
    assert result.cancel_requested_at is result.cancel_requested_by is None
    assert "SSH unreachable" in result.cancel_error
    assert result.can_cancel


def test_cancel_process_is_bounded(tmp_path):
    marker = tmp_path / "late-side-effect"
    spec = WatchSpec(
        check_command="false",
        log_path=str(tmp_path / "log"),
        cwd=str(tmp_path),
        cancel_command=f"sleep 5; touch {shlex.quote(str(marker))}",
    )
    diagnostic = run_watcher_cancel(spec, timeout=0.01)
    assert "timed out" in diagnostic
    assert "outcome is unknown" in diagnostic
    assert not marker.exists()


def test_watcher_actions_upgrade_preserves_old_shell_rows_and_indexes(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    shell = _record("old-shell")
    store.create_watchers([shell])
    expected = store.watcher(shell.watcher_id)
    with store.connection() as connection:
        indexes = list(connection.execute("PRAGMA index_list(watchers)"))
        for field in (
            "cancel_command",
            "cancel_requested_by",
            "cancel_requested_at",
            "cancel_error",
            "worker_id",
        ):
            connection.execute(f"ALTER TABLE watchers DROP COLUMN {field}")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version >= 10")
    upgraded = AppStore(store.path)
    assert upgraded.watcher(shell.watcher_id) == expected
    with upgraded.connection() as connection:
        assert list(connection.execute("PRAGMA index_list(watchers)")) == indexes
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(watchers)")}
        assert "job_id" not in columns
        assert "cancel_command" in columns
    assert upgraded.storage_schema_ledger_head() == upgraded.storage_schema_registry_head()


def test_arming_polling_and_stop_never_execute_cancel_command(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    marker = tmp_path / "cancel-was-run"
    spec = WatchSpec(
        check_command="false",
        cancel_command=f"touch {shlex.quote(str(marker))}",
        log_path=str(tmp_path / "log"),
        cwd=str(tmp_path),
    )
    record = arm_watchers(store, [spec], _binding())[0]
    WatcherPoller(store, clock=lambda: "9999-01-01T00:00:00Z").poll_once()
    store.stop_watchers("project", [record.watcher_id])
    assert not marker.exists()
    assert store.watcher(record.watcher_id).cancel_requested_at is None


def test_simultaneous_active_checks_share_one_cancellation_claim(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store)
    checks = threading.Barrier(2)
    calls = []

    def check(*_):
        checks.wait(timeout=5)
        return _active()

    first = WatcherPoller(store, check_runner=check, cancel_runner=lambda *_: calls.append(1))
    second = WatcherPoller(
        AppStore(store.path), check_runner=check, cancel_runner=lambda *_: calls.append(1)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(first.cancel, "project", record.watcher_id, "human-one"),
            pool.submit(second.cancel, "project", record.watcher_id, "human-two"),
        ]
        records = [result.result(timeout=5) for result in results]
    assert calls == [1]
    assert records[0].cancel_requested_by == records[1].cancel_requested_by
    assert records[0].cancel_requested_at == records[1].cancel_requested_at


@pytest.mark.parametrize("stopped", [False, True])
def test_restore_detaches_cancel_action_even_for_previously_stopped_watchers(tmp_path, stopped):
    store = AppStore(tmp_path / "app.sqlite")
    record = _cancel_record(store)
    if stopped:
        store.stop_watchers("project", [record.watcher_id])
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.detach_watchers_for_restore(
            connection, diagnostic="Restored history", confirmed_by="human-one", now=store.now()
        )
    restored = store.watcher(record.watcher_id)
    assert restored.status == "stopped"
    assert restored.cancel_command is None and not restored.can_cancel
    assert restored.notified and restored.next_check_at is None
