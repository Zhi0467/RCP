from __future__ import annotations

import json
import shlex

import pytest
from pydantic import ValidationError

from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.storage import AppStore
from rcp.watchers import (
    AgentWatchSpec,
    WatcherPoller,
    arm_watchers,
    parse_experiment_watch_json,
    parse_watch_json,
    run_watcher_cancel,
)

from .test_watchers import _binding


def test_probe_storage_is_latest_per_project_and_machine(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    probe = ComputeBackendProbe(
        machine_id="local",
        backend_id="systemd_user",
        state="ready",
        ready=True,
        diagnostic="Ready",
        containment="cooperative",
        status_label="Ready",
        status_tone="ready",
    )
    assert store.compute_backend_probe("project", "local", "helper") is None
    store.record_compute_backend_probe("project", probe, "helper")
    failed = probe.model_copy(update={"ready": False, "state": "failed", "diagnostic": "Offline"})
    store.record_compute_backend_probe("project", failed, "helper")
    reopened = AppStore(store.path)
    assert reopened.compute_backend_probe("project", "local", "helper") == failed
    assert reopened.compute_backend_probe("other", "local", "helper") is None
    assert reopened.compute_backend_probe("project", "other", "helper") is None
    with reopened.connection() as connection:
        rows = connection.execute("SELECT probed_at FROM compute_backend_probes").fetchall()
    assert len(rows) == 1 and rows[0][0]


@pytest.mark.parametrize("parser", [parse_watch_json, parse_experiment_watch_json])
@pytest.mark.parametrize("cancel_command", [None, "scancel 1234"])
def test_one_shell_contract_preserves_optional_human_action(parser, cancel_command):
    item = {
        "purpose": "job finishes",
        "check_command": "false",
        "log_path": "/logs/1234",
        "cwd": "/work",
    }
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
        {
            "purpose": "job finishes",
            "check_command": "false",
            "log_path": "/log",
            "cwd": "/work",
            "job_id": "rcp-job",
        },
        {
            "purpose": "job finishes",
            "check_command": "false",
            "log_path": "/log",
            "cwd": "/work",
            "cancel_command": " ",
        },
        {
            "purpose": "job finishes",
            "check_command": "false",
            "log_path": "/log",
            "cancel_command": "scancel 1",
        },
    ],
)
def test_no_native_job_observer_or_partial_shell_action(item, parser):
    with pytest.raises((ValueError, ValidationError)):
        parser(json.dumps({"external": [item], "graph": []}))


def test_cancel_process_is_bounded(tmp_path):
    marker = tmp_path / "late-side-effect"
    spec = AgentWatchSpec(
        purpose="job finishes",
        check_command="false",
        log_path=str(tmp_path / "log"),
        cwd=str(tmp_path),
        cancel_command=f"sleep 5; touch {shlex.quote(str(marker))}",
    )
    diagnostic = run_watcher_cancel(spec, timeout=0.01)
    assert diagnostic is not None
    assert not marker.exists()


def test_arming_polling_and_cancel_never_execute_cancel_command(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    marker = tmp_path / "cancel-was-run"
    spec = AgentWatchSpec(
        purpose="job finishes",
        check_command="false",
        cancel_command=f"touch {shlex.quote(str(marker))}",
        log_path=str(tmp_path / "log"),
        cwd=str(tmp_path),
    )
    record = arm_watchers(store, [spec], _binding())[0]
    WatcherPoller(store, clock=lambda: "9999-01-01T00:00:00Z").poll_once()
    store.cancel_watchers([record.watcher_id], by="agent")
    assert not marker.exists()
    assert store.watcher(record.watcher_id).state == "cancelled"
