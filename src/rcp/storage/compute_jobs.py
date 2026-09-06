"""Durable compute ownership and observed lifecycle updates."""

from __future__ import annotations

import json
import sqlite3

from rcp.compute_jobs.models import ComputeBackendProbe, ComputeJobRecord, ComputeJobStatus
from rcp.limits import COMPUTE_JOBS_PER_PROJECT_LIST_LIMIT


def _compute_job_record(row: sqlite3.Row) -> ComputeJobRecord:
    values = dict(row)
    values["argv"] = json.loads(values["argv"])
    return ComputeJobRecord.model_validate(values)


class ComputeJobStoreMixin:
    def compute_command_receipts(
        self, operation_id: str, verb: str, key: str
    ) -> list[dict[str, object]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM graph_run_receipts WHERE operation_id = ? "
                "AND category IN ('compute_command_started', 'compute_command_result') "
                "AND json_extract(payload_json, '$.verb') = ? "
                "AND json_extract(payload_json, '$.key') = ? ORDER BY receipt_id",
                (operation_id, verb, key),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def compute_backend_probe(
        self, project_id: str, execution_machine: str
    ) -> ComputeBackendProbe | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT probe_json FROM compute_backend_probes "
                "WHERE project_id = ? AND execution_machine = ?",
                (project_id, execution_machine),
            ).fetchone()
        return ComputeBackendProbe.model_validate_json(row[0]) if row is not None else None

    def delete_compute_backend_probe(self, project_id: str, execution_machine: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM compute_backend_probes WHERE project_id = ? AND execution_machine = ?",
                (project_id, execution_machine),
            )

    def record_compute_backend_probe(
        self, project_id: str, probe: ComputeBackendProbe
    ) -> ComputeBackendProbe:
        probe = ComputeBackendProbe.model_validate(probe.model_dump())
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO compute_backend_probes "
                "(project_id, execution_machine, probe_json, probed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, execution_machine) DO UPDATE SET "
                "probe_json = excluded.probe_json, probed_at = excluded.probed_at",
                (project_id, probe.execution_machine, probe.model_dump_json(), self.now()),
            )
        return probe

    def create_compute_job(self, record: ComputeJobRecord) -> ComputeJobRecord:
        record = ComputeJobRecord.model_validate(record.model_dump())
        values = record.model_dump(mode="json")
        values["argv"] = json.dumps(record.argv)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.connection() as connection:
            connection.execute(
                f"INSERT INTO compute_jobs ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return record

    def compute_job(self, job_id: str) -> ComputeJobRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM compute_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _compute_job_record(row) if row is not None else None

    def compute_jobs(self, project_id: str) -> list[ComputeJobRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM compute_jobs WHERE project_id = ? "
                "ORDER BY created_at DESC, job_id LIMIT ?",
                (project_id, COMPUTE_JOBS_PER_PROJECT_LIST_LIMIT),
            ).fetchall()
        return [_compute_job_record(row) for row in rows]

    def running_compute_jobs(self) -> list[ComputeJobRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM compute_jobs WHERE status = 'running' ORDER BY created_at, job_id"
            ).fetchall()
        return [_compute_job_record(row) for row in rows]

    def record_compute_job_refresh(
        self,
        job_id: str,
        *,
        status: ComputeJobStatus,
        exit_status: int | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        diagnostic: str | None = None,
    ) -> ComputeJobRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM compute_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = _compute_job_record(row)
            if current.status != "running":
                return current
            if status != "running" and current.cancel_requested_at is not None:
                if status == "lost":
                    diagnostic = None
                status = "cancelled"
            refreshed = ComputeJobRecord.model_validate(
                {
                    **current.model_dump(),
                    "status": status,
                    "exit_status": exit_status,
                    "started_at": started_at or current.started_at,
                    "ended_at": ended_at,
                    "diagnostic": diagnostic,
                }
            )
            connection.execute(
                "UPDATE compute_jobs SET status = ?, exit_status = ?, started_at = ?, "
                "ended_at = ?, diagnostic = ? WHERE job_id = ?",
                (
                    refreshed.status,
                    refreshed.exit_status,
                    refreshed.started_at,
                    refreshed.ended_at,
                    refreshed.diagnostic,
                    job_id,
                ),
            )
        return refreshed

    def request_compute_job_cancel(self, job_id: str, requested_by: str) -> ComputeJobRecord:
        if not requested_by.strip():
            raise ValueError("compute cancellation requires a requester")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE compute_jobs SET cancel_requested_by = ?, cancel_requested_at = ? "
                "WHERE job_id = ? AND status = 'running' AND cancel_requested_at IS NULL",
                (requested_by, self.now(), job_id),
            )
            row = connection.execute(
                "SELECT * FROM compute_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return _compute_job_record(row)
