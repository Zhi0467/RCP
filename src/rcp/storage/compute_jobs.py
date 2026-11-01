"""Durable compute ownership and observed lifecycle updates."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Literal

from rcp.compute_jobs.models import (
    ComputeBackendProbe,
    ComputeJobRecord,
    ComputeJobStatus,
)
from rcp.compute_jobs.routes import ComputeRoute
from rcp.core.models import EpisodeUnfinishedJob
from rcp.limits import COMPUTE_JOBS_PER_PROJECT_LIST_LIMIT
from rcp.storage.digest import append_digest_event
from rcp.storage.mixin_base import StoreMixinBase


def _compute_job_record(row: sqlite3.Row) -> ComputeJobRecord:
    values = dict(row)
    values["argv"] = json.loads(values["argv"])
    return ComputeJobRecord.model_validate(values)


class ComputeJobStoreMixin(StoreMixinBase):
    def compute_command_receipts(
        self, operation_ids: Sequence[str], verb: str, key: str
    ) -> list[dict[str, object]]:
        """Return one key's receipts across the given task attempts, oldest first."""

        if not operation_ids:
            return []
        placeholders = ", ".join("?" for _ in operation_ids)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id, payload_json FROM graph_run_receipts "
                f"WHERE operation_id IN ({placeholders}) "
                "AND category IN ('compute_command_started', 'compute_command_result') "
                "AND json_extract(payload_json, '$.verb') = ? "
                "AND json_extract(payload_json, '$.key') = ? ORDER BY receipt_id",
                (*operation_ids, verb, key),
            ).fetchall()
        return [{**json.loads(row[1]), "operation_id": row[0]} for row in rows]

    def compute_backend_probe(
        self, project_id: str, execution_machine: str, route: ComputeRoute
    ) -> ComputeBackendProbe | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT probe_json FROM compute_backend_probes "
                "WHERE project_id = ? AND execution_machine = ? AND route = ?",
                (project_id, execution_machine, route),
            ).fetchone()
        return ComputeBackendProbe.model_validate_json(row[0]) if row is not None else None

    def delete_compute_backend_probe(
        self, project_id: str, execution_machine: str, *, route: ComputeRoute | None = None
    ) -> None:
        """Forget one machine's route checks, or only ``route`` when it is given."""
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM compute_backend_probes WHERE project_id = ? AND execution_machine = ? "
                "AND (? IS NULL OR route = ?)",
                (project_id, execution_machine, route, route),
            )

    def record_compute_backend_probe(
        self, project_id: str, probe: ComputeBackendProbe, route: ComputeRoute
    ) -> ComputeBackendProbe:
        probe = ComputeBackendProbe.model_validate(probe.model_dump())
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO compute_backend_probes "
                "(project_id, execution_machine, route, probe_json, probed_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id, execution_machine, route) DO UPDATE SET "
                "probe_json = excluded.probe_json, probed_at = excluded.probed_at",
                (project_id, probe.execution_machine, route, probe.model_dump_json(), self.now()),
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

    def compute_jobs_for_log_path(
        self, project_id: str, execution_host: str, log_path: str
    ) -> list[ComputeJobRecord]:
        """Resolve durable ownership without the display list's age/count cap."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM compute_jobs WHERE project_id = ? "
                "AND execution_host = ? AND log_path = ?",
                (project_id, execution_host, log_path),
            ).fetchall()
        return [_compute_job_record(row) for row in rows]

    def record_compute_job_cancelled(self, job_id: str, *, ended_at: str) -> ComputeJobRecord:
        """A fresh human cancellation may settle a lost job, preserving its diagnostics."""
        return self._record_compute_job_completion(job_id, status="cancelled", ended_at=ended_at)

    def record_compute_job_exited(
        self, job_id: str, *, exit_status: int, ended_at: str
    ) -> ComputeJobRecord:
        """Explicit recovery preserves a verified exit receipt even after an earlier loss."""
        return self._record_compute_job_completion(
            job_id, status="exited", exit_status=exit_status, ended_at=ended_at
        )

    def _record_compute_job_completion(
        self,
        job_id: str,
        *,
        status: Literal["cancelled", "exited"],
        ended_at: str,
        exit_status: int | None = None,
    ) -> ComputeJobRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM compute_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = _compute_job_record(row)
            if current.status not in {"running", "lost"}:
                return current
            completed = current.model_copy(
                update={
                    "status": status,
                    "exit_status": exit_status,
                    "ended_at": ended_at,
                }
            )
            connection.execute(
                "UPDATE compute_jobs SET status = ?, exit_status = ?, ended_at = ? "
                "WHERE job_id = ?",
                (status, exit_status, ended_at, job_id),
            )
            if current.status == "running":
                append_digest_event(
                    connection,
                    project_id=current.project_id,
                    kind="job_ended",
                    item_id=job_id,
                    created_at=self.now(),
                    payload={
                        "title": current.label or job_id,
                        "status": status,
                        "deep_link": None,
                    },
                )
            return completed

    @staticmethod
    def _episode_binding_unfinished_jobs(
        connection, project_id, owner_episode_id
    ) -> list[EpisodeUnfinishedJob]:
        """Jobs that may still write the owner's worktree; a lost job is unobservable."""
        jobs = [
            EpisodeUnfinishedJob(
                kind="compute_job",
                id=row["job_id"],
                status=row["status"],
                execution_host=row["execution_host"] or "",
                command=" ".join(json.loads(row["argv"])),
                log_path=row["log_path"],
            )
            for row in connection.execute(
                "SELECT j.job_id, j.status, j.execution_host, j.argv, j.log_path "
                "FROM compute_jobs j JOIN episodes e ON e.episode_id = j.episode_id "
                "WHERE j.project_id = ? AND COALESCE(e.isolation_owner_episode_id, e.episode_id) "
                "= ? AND j.status IN ('running', 'lost') ORDER BY j.job_id",
                (project_id, owner_episode_id),
            )
        ]
        # Scheduler jobs have no registry row; their watcher is the only observer. Only a
        # worktree can be written by one, so a graph-only binding has no such jobs.
        # A graph-condition watcher observes the canonical graph and runs no job.
        jobs += [
            EpisodeUnfinishedJob(
                kind="watcher",
                id=row["watcher_id"],
                status=row["status"],
                execution_host=row["execution_host"] or "",
                command=row["check_command"],
                log_path=row["log_path"],
            )
            for row in connection.execute(
                "SELECT w.watcher_id, w.status, w.execution_host, w.check_command, w.log_path "
                "FROM watchers w JOIN episodes e ON e.episode_id = w.episode_id "
                "JOIN episode_isolations i ON i.project_id = w.project_id "
                "AND i.owner_episode_id = COALESCE(e.isolation_owner_episode_id, e.episode_id) "
                "WHERE w.project_id = ? AND i.owner_episode_id = ? "
                "AND json_extract(i.binding_json, '$.worktree') IS NOT NULL "
                "AND w.graph_condition_json IS NULL "
                # Stop ends observation, not the job: only a recorded completion is finished.
                "AND (w.status IN ('active', 'degraded') "
                "OR (w.status = 'stopped' AND w.completed_at IS NULL)) "
                "ORDER BY w.watcher_id",
                (project_id, owner_episode_id),
            )
        ]
        return jobs

    def episode_binding_unfinished_jobs(
        self, project_id: str, owner_episode_id: str
    ) -> list[EpisodeUnfinishedJob]:
        with self.connection() as connection:
            return self._episode_binding_unfinished_jobs(connection, project_id, owner_episode_id)

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
            if refreshed.status != "running":
                append_digest_event(
                    connection,
                    project_id=current.project_id,
                    kind="job_ended",
                    item_id=job_id,
                    created_at=self.now(),
                    payload={
                        "title": current.label or job_id,
                        "status": refreshed.status,
                        "deep_link": None,
                    },
                )
        return refreshed
