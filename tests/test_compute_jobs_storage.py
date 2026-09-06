from __future__ import annotations

from pathlib import Path

import pytest

from rcp.compute_jobs.models import ComputeJobRecord
from rcp.server_ops.application_validation import (
    CandidateRehearsalRefused,
    _rebind_local_stage_paths,
    _validate_path_column_inventory,
    _validate_rebound_paths,
)
from rcp.storage import AppStore


def job_record(job_id: str = "job-1", **updates) -> ComputeJobRecord:
    return ComputeJobRecord.model_validate(
        {
            "job_id": job_id,
            "project_id": "project",
            "origin_operation_id": "operation",
            "execution_machine": "local",
            "backend_id": "systemd_user",
            "backend_handle": f"rcp-job-{job_id}",
            "job_root": f"/jobs/{job_id}",
            "cwd": "/project",
            "argv": ["sh", "-c", "echo 'a b'"],
            "log_path": f"/jobs/{job_id}/log",
            "exit_path": f"/jobs/{job_id}/exit",
            "created_at": "2026-09-06T00:00:00Z",
            **updates,
        }
    )


def test_compute_job_storage_roundtrip_listing_and_terminal_guard(tmp_path, monkeypatch) -> None:
    store = AppStore(tmp_path / "app.sqlite")
    first = job_record()
    second = job_record("job-2", created_at="2026-09-06T00:00:01Z")
    assert store.create_compute_job(first) == first
    store.create_compute_job(second)
    store.create_compute_job(job_record("other", project_id="other"))
    assert store.compute_job(first.job_id) == first
    assert store.compute_job("missing") is None
    assert [record.job_id for record in store.compute_jobs("project")] == ["job-2", "job-1"]
    monkeypatch.setattr("rcp.storage.compute_jobs.COMPUTE_JOBS_PER_PROJECT_LIST_LIMIT", 1)
    assert len(store.compute_jobs("project")) == 1
    exited = store.record_compute_job_refresh(
        "job-1", status="exited", exit_status=3, started_at=store.now(), ended_at=store.now()
    )
    assert exited.exit_status == 3
    assert exited.started_at is not None
    assert {record.job_id for record in store.running_compute_jobs()} == {"job-2", "other"}
    assert store.record_compute_job_refresh("job-1", status="running") == exited


def test_compute_job_cancel_request_is_idempotent_and_attributed(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite")
    store.create_compute_job(job_record())
    requested = store.request_compute_job_cancel("job-1", "human-a")
    assert requested.cancel_requested_by == "human-a"
    assert requested.cancel_requested_at is not None
    assert requested.status == "running"
    assert store.request_compute_job_cancel("job-1", "human-b") == requested
    cancelled = store.record_compute_job_refresh("job-1", status="cancelled", ended_at=store.now())
    assert store.request_compute_job_cancel("job-1", "human-c") == cancelled
    with pytest.raises(KeyError):
        store.request_compute_job_cancel("missing", "human")
    with pytest.raises(KeyError):
        store.record_compute_job_refresh("missing", status="lost")


@pytest.mark.parametrize("observed_status", ["exited", "lost"])
def test_terminal_refresh_uses_cancel_intent_from_its_transaction(
    tmp_path, observed_status
) -> None:
    store = AppStore(tmp_path / "app.sqlite")
    store.create_compute_job(job_record())
    store.request_compute_job_cancel("job-1", "human")
    refreshed = store.record_compute_job_refresh(
        "job-1",
        status=observed_status,
        ended_at=store.now(),
        diagnostic="Job is gone without an exit file." if observed_status == "lost" else None,
    )
    assert refreshed.status == "cancelled"
    assert refreshed.cancel_requested_by == "human"
    assert refreshed.diagnostic is None


def test_compute_jobs_migration_upgrades_version_eight_without_changing_records(tmp_path) -> None:
    path = tmp_path / "app.sqlite"
    previous = AppStore(path)
    identity = previous.space_id
    with previous.connection() as connection:
        connection.execute("DROP TABLE compute_jobs")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 9")
    upgraded = AppStore(path)
    assert upgraded.space_id == identity
    assert upgraded.storage_schema_ledger_head() == upgraded.storage_schema_registry_head()
    assert upgraded.running_compute_jobs() == []
    upgraded.create_compute_job(job_record())
    reopened = AppStore(path)
    assert reopened.compute_job("job-1") == job_record()
    with reopened.connection() as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(compute_jobs)")}
    assert {"compute_jobs_status", "compute_jobs_project", "compute_jobs_origin"} <= indexes


def test_restore_rebinds_all_local_job_paths_and_preserves_remote_paths(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite")
    local = job_record()
    remote = job_record("remote", execution_host="worker", execution_machine="worker")
    store.create_compute_job(local)
    store.create_compute_job(remote)
    absent = tmp_path / "absent"
    with store.connection() as connection:
        _validate_path_column_inventory(connection)
        with pytest.raises(CandidateRehearsalRefused, match="compute job path"):
            _validate_rebound_paths(connection, root=tmp_path, projects=[])
        _rebind_local_stage_paths(connection, absent)
        _validate_rebound_paths(connection, root=tmp_path, projects=[])
    rebound = store.compute_job(local.job_id)
    for field in ("job_root", "cwd", "log_path", "exit_path"):
        path = Path(getattr(rebound, field))
        assert path.is_relative_to(absent)
        assert not path.exists()
    assert store.compute_job(remote.job_id) == remote


def test_restore_rejects_unrelated_table_using_job_paths(tmp_path) -> None:
    store = AppStore(tmp_path / "app.sqlite")
    with store.connection() as connection:
        connection.execute("CREATE TABLE unexpected (exit_path TEXT)")
        with pytest.raises(CandidateRehearsalRefused, match="unexpectedly owns"):
            _validate_path_column_inventory(connection)
