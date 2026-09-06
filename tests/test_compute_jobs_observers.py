from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rcp.api.app import _generic_watcher_delivery_request
from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.runs.experiment_loop import _watcher_state, prepare_experiment_watcher_records
from rcp.server_ops.application_validation import (
    _rebind_local_stage_paths,
    _validate_path_column_inventory,
    _validate_rebound_paths,
)
from rcp.storage import AppStore
from rcp.watchers import (
    ExperimentWatchSpec,
    WatcherInitialCheckError,
    WatcherPoller,
    WatchSpec,
    arm_watchers,
    parse_experiment_watch_json,
    parse_watch_json,
    validate_watch_specs,
)

from .test_compute_jobs_storage import job_record
from .test_watchers import _binding, _record, _task


@pytest.mark.parametrize("experiment", [False, True])
def test_job_observer_parsing_accepts_mixed_closed_forms(experiment):
    shell = {"check_command": "true", "log_path": "/log", "cwd": "/cwd"}
    job = {"job_id": "job-1"}
    if experiment:
        shell["group"] = job["group"] = "batch"
    parsed = (parse_experiment_watch_json if experiment else parse_watch_json)(
        json.dumps({"external": [shell, job], "graph": []})
    )
    specs = parsed.observers if experiment else parsed.external
    assert specs[0].check_command == "true"
    assert specs[1].job_id == "job-1"
    assert specs[1].check_command is None
    assert specs[1].log_path is None
    assert specs[1].cwd is None


@pytest.mark.parametrize(
    "fields",
    [
        {"job_id": ""},
        {"job_id": " "},
        {"job_id": "job", "cwd": "/cwd"},
        {"job_id": "job", "log_path": None},
        {"job_id": None, "check_command": "true", "log_path": "/log", "cwd": "/cwd"},
        {"job_id": "job", "unknown": True},
        {"check_command": "true", "log_path": "/log"},
    ],
)
@pytest.mark.parametrize("parser", [parse_watch_json, parse_experiment_watch_json])
def test_job_observer_rejects_partial_or_mixed_forms(fields, parser):
    with pytest.raises((ValueError, ValidationError)):
        parser(json.dumps({"external": [fields], "graph": []}))


def test_probe_storage_is_latest_per_project_and_machine(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    probe = ComputeBackendProbe(
        execution_machine="local",
        backend_id="launchd",
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


def test_job_observer_migration_preserves_old_shell_rows_and_indexes(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    shell = _record("old-shell")
    store.create_watchers([shell])
    expected = store.watcher(shell.watcher_id)
    with store.connection() as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'watchers'"
        ).fetchone()[0]
        sql = sql.replace(", job_id TEXT", "").replace(", worker_id TEXT", "")
        for column in ("check_command", "log_path", "cwd"):
            sql = sql.replace(f"{column} TEXT", f"{column} TEXT NOT NULL")
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'watchers' AND sql IS NOT NULL"
            )
        ]
        store._rebuild_storage_table(connection, "watchers", sql)
        for statement in indexes:
            connection.execute(statement)
        connection.execute("DROP TABLE compute_backend_probes")
        connection.execute(
            "DELETE FROM storage_schema_migrations WHERE migration_version IN (10, 11)"
        )
    upgraded = AppStore(store.path)
    assert upgraded.watcher(shell.watcher_id) == expected
    with upgraded.connection() as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(watchers)")}
        assert "job_id" in columns
        assert all(columns[name][3] == 0 for name in ("check_command", "log_path", "cwd"))
        assert set(indexes) == {
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'watchers' AND sql IS NOT NULL"
            )
        }
    assert upgraded.storage_schema_ledger_head() == upgraded.storage_schema_registry_head()


@pytest.fixture
def observer_environment(tmp_path, manifest, monkeypatch):
    store = AppStore(tmp_path / "app.sqlite")
    job = job_record(origin_operation_id="origin")
    store.create_compute_job(job)
    refreshed = []

    def refresh(store, _manifest, job_id, *, data_dir):
        refreshed.append(job_id)
        return store.compute_job(job_id)

    monkeypatch.setattr("rcp.watchers.refresh_compute_job", refresh)
    return store, manifest, refreshed


@pytest.mark.parametrize(
    "status,expected", [("running", "active"), ("exited", "completed"), ("cancelled", "completed")]
)
def test_job_observer_arming_refreshes_and_stores_without_shell(
    observer_environment, status, expected
):
    store, manifest, refreshed = observer_environment
    if status != "running":
        store.record_compute_job_refresh(
            "job-1", status=status, exit_status=7, ended_at=store.now()
        )
    records = arm_watchers(store, [WatchSpec(job_id="job-1")], _binding(), manifest=manifest)
    assert refreshed == ["job-1"]
    watcher = store.watcher(records[0].watcher_id)
    assert watcher.status == expected
    assert watcher.job_id == "job-1"
    assert watcher.check_command is watcher.log_path is watcher.cwd is None


@pytest.mark.parametrize(
    "update,error",
    [
        ({"project_id": "other"}, "this project"),
        ({"origin_operation_id": "other"}, "task lineage"),
    ],
)
def test_job_observer_arming_refuses_unowned_job(observer_environment, update, error):
    store, manifest, refreshed = observer_environment
    job = job_record("foreign", origin_operation_id="origin").model_copy(update=update)
    store.create_compute_job(job)
    with pytest.raises(ValueError, match=error):
        arm_watchers(store, [WatchSpec(job_id=job.job_id)], _binding(), manifest=manifest)
    assert not refreshed and not store.watchers("project")


def test_job_observer_same_episode_is_only_experiment_authority(observer_environment):
    store, manifest, refreshed = observer_environment
    store.create_compute_job(job_record("prior", episode_id="episode", origin_operation_id="prior"))
    binding = _binding().model_copy(update={"episode_id": "episode"})
    with pytest.raises(ValueError, match="lineage"):
        arm_watchers(store, [WatchSpec(job_id="prior")], binding, manifest=manifest)
    binding.continuation = binding.continuation.model_copy(
        update={
            "patch_kind": "experiment_loop",
            "control_episode_id": "episode",
            "control_node_id": "exp-one",
            "control_invocation": 1,
            "control_invocation_ceiling": 2,
        }
    )
    assert arm_watchers(store, [WatchSpec(job_id="prior")], binding, manifest=manifest)
    assert refreshed == ["prior"]


def test_missing_and_lost_job_cannot_arm(observer_environment):
    store, manifest, _ = observer_environment
    with pytest.raises(ValueError, match="this project"):
        arm_watchers(store, [WatchSpec(job_id="missing")], _binding(), manifest=manifest)
    store.record_compute_job_refresh("job-1", status="lost", diagnostic="Owner disappeared")
    with pytest.raises(WatcherInitialCheckError, match="Owner disappeared"):
        arm_watchers(store, [WatchSpec(job_id="job-1")], _binding(), manifest=manifest)
    assert not store.watchers("project")


@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "active"),
        ("exited", "completed"),
        ("cancelled", "completed"),
        ("lost", "degraded"),
    ],
)
def test_job_observer_poller_refreshes_without_shell(observer_environment, status, expected):
    store, manifest, refreshed = observer_environment
    watcher = arm_watchers(store, [WatchSpec(job_id="job-1")], _binding(), manifest=manifest)[0]
    if status != "running":
        store.record_compute_job_refresh(
            "job-1", status=status, diagnostic="Owner disappeared" if status == "lost" else None
        )

    def no_shell(*_):
        pytest.fail("job observers must never execute a shell check")

    poller = WatcherPoller(
        store,
        check_runner=no_shell,
        manifest_for_project=lambda _: manifest,
        clock=lambda: "9999-01-01T00:00:00Z",
    )
    poller.poll_once()
    record = store.watcher(watcher.watcher_id)
    assert refreshed == ["job-1", "job-1"]
    assert record.status == expected
    if status == "lost":
        assert record.last_error == "Owner disappeared"
        assert not store.completed_watcher_groups()


def test_job_observer_restore_preserves_closed_form(observer_environment, tmp_path):
    store, manifest, _ = observer_environment
    watcher = arm_watchers(store, [WatchSpec(job_id="job-1")], _binding(), manifest=manifest)[0]
    with store.connection() as connection:
        _validate_path_column_inventory(connection)
        _rebind_local_stage_paths(connection, tmp_path / "absent")
        _validate_rebound_paths(connection, root=tmp_path, projects=[])
    assert store.watcher(watcher.watcher_id) == watcher
    assert store.compute_job("job-1").log_path.startswith(str(tmp_path / "absent"))


def test_compute_job_payload_in_work_and_experiment_wakes(observer_environment):
    store, manifest, _ = observer_environment
    store.record_compute_job_refresh(
        "job-1",
        status="exited",
        exit_status=7,
        started_at="2026-09-06T00:00:00Z",
        ended_at="2026-09-06T00:02:00Z",
    )
    work = arm_watchers(store, [WatchSpec(job_id="job-1")], _binding(), manifest=manifest)
    request = _generic_watcher_delivery_request(work, store=store)
    evidence = json.loads(request.message.split(": ", 2)[-1])
    expected = {
        "job_id": "job-1",
        "exit_status": 7,
        "started_at": "2026-09-06T00:00:00Z",
        "ended_at": "2026-09-06T00:02:00Z",
        "duration_seconds": 120,
        "log_path": "/jobs/job-1/log",
        "backend_id": "systemd_user",
    }
    assert evidence == expected
    store.create_agent_task(_task(store, "wake", []))
    execution = SimpleNamespace(store=store, operation_id="wake")
    binding = _binding()
    binding.continuation = binding.continuation.model_copy(
        update={
            "patch_kind": "experiment_loop",
            "control_episode_id": "episode",
            "control_node_id": "exp-one",
            "control_invocation": 1,
            "control_invocation_ceiling": 2,
        }
    )
    specs = [ExperimentWatchSpec(job_id="job-1")]
    checks = validate_watch_specs(specs, "", store=store, binding=binding, manifest=manifest)
    records = prepare_experiment_watcher_records(execution, specs, checks, binding)
    stored = store.create_watchers(records)
    state = _watcher_state(execution, "exp-one", [stored[0].watcher_id], "episode", "watcher_wake")
    assert {key: state[0][key] for key in expected} == expected
    assert "check_command" not in state[0] and "cwd" not in state[0]
