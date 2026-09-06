"""Launch, inspect, and cancel durable compute without provider task policy."""

from __future__ import annotations

import logging
import subprocess
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from rcp.compute_jobs.backend_context import (
    ComputeLaunchUncertainError,
    ComputeProbeStaleError,
    ComputeTransportError,
    recorded_job_context,
    resolve_context,
)
from rcp.compute_jobs.backends import COMPUTE_BACKENDS
from rcp.compute_jobs.files import (
    prepare_job_root,
    read_job_file,
    remove_job_root,
    resolve_jobs_root,
    write_job_file,
)
from rcp.compute_jobs.models import ComputeBackendProbe, ComputeJobRecord, ComputeLaunchRequest
from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import Manifest
from rcp.limits import COMPUTE_JOB_LOG_TAIL_MAX_BYTES
from rcp.storage import AppStore

logger = logging.getLogger(__name__)


def launch_compute_job(
    store: AppStore,
    manifest: Manifest,
    request: ComputeLaunchRequest,
    *,
    data_dir: Path,
    project_id: str,
    origin_operation_id: str,
    episode_id: str | None,
    execution_machine: str,
    writable_roots: list[str] | tuple[str, ...],
    protected_paths: list[str] | tuple[str, ...] = (),
    probe: ComputeBackendProbe | None = None,
) -> ComputeJobRecord:
    request = ComputeLaunchRequest.model_validate(request.model_dump())
    if any(not PurePosixPath(root).is_absolute() for root in writable_roots):
        raise ValueError("compute writable roots must be absolute")
    context, backend = resolve_context(manifest, execution_machine)
    if backend is None:
        raise RuntimeError(
            f"No compute backend is available for machine {execution_machine!r}; "
            "configure a compute backend for this machine."
        )
    job_id = uuid.uuid4().hex
    if probe is not None:
        if not probe.ready:
            raise ValueError("Compute probe does not match the resolved ready backend.")
        if probe.execution_machine != execution_machine or probe.backend_id != backend.id:
            raise ComputeProbeStaleError("Compute probe does not match the resolved ready backend.")
        context.containment = probe.containment
    root = PurePosixPath(resolve_jobs_root(context, data_dir)) / job_id
    context.writable_roots = tuple(dict.fromkeys((*writable_roots, str(root))))
    context.protected_paths = tuple(protected_paths)
    record = ComputeJobRecord(
        job_id=job_id,
        project_id=project_id,
        origin_operation_id=origin_operation_id,
        episode_id=episode_id,
        execution_machine=execution_machine,
        execution_host=context.execution_host,
        backend_id=backend.id,
        backend_handle="",
        job_root=str(root),
        cwd=request.cwd,
        argv=request.argv,
        log_path=str(root / "log"),
        exit_path=str(root / "exit"),
        containment=context.containment,
        status="running",
        created_at=store.now(),
    )
    prepare_job_root(
        context,
        str(root),
        request,
        project_id=project_id,
        origin_operation_id=origin_operation_id,
        episode_id=episode_id,
    )
    try:
        handle = backend.start(str(root), str(root / "run.sh"), request, context)
    except ComputeLaunchUncertainError:
        # Acceptance is unknown; the launch intent must survive for inspection.
        raise
    except Exception:
        try:
            remove_job_root(context, str(root))
        except Exception as cleanup_error:
            logger.warning(
                "Compute launch cleanup failed: %s", safe_compute_diagnostic(str(cleanup_error))
            )
        raise
    record = record.model_copy(update={"backend_handle": handle})
    try:
        write_job_file(context, str(root / "launch.json"), record.model_dump_json())
    except Exception as exc:
        # The backend owns the job now; the row is its durable record even without the receipt.
        record = record.model_copy(
            update={"diagnostic": safe_compute_diagnostic(f"Launch receipt not written: {exc}")}
        )
    return store.create_compute_job(record)


def refresh_compute_job(
    store: AppStore,
    manifest: Manifest,
    job_id: str,
    *,
    data_dir: Path,
    unavailable_hosts: dict[str, str] | None = None,
) -> ComputeJobRecord:
    record = store.compute_job(job_id)
    if record is None:
        raise KeyError(job_id)
    if record.status != "running":
        return record
    try:
        context = recorded_job_context(manifest, record)
        try:
            alive = COMPUTE_BACKENDS[record.backend_id].alive(record.backend_handle, context)
            if alive is None:
                raise RuntimeError("compute backend could not determine whether the job is alive")
        except (ComputeTransportError, subprocess.TimeoutExpired, OSError) as exc:
            # A local OSError (a missing scheduler binary, say) is this row's problem,
            # not an outage of the local host.
            if unavailable_hosts is not None and (
                record.execution_host or not isinstance(exc, OSError)
            ):
                unavailable_hosts[record.execution_host] = safe_compute_diagnostic(str(exc))
            raise
        started = read_job_file(context, str(PurePosixPath(record.job_root) / "started"))
        started_at = record.started_at
        if started:
            # A malformed started receipt never blocks settlement of a gone job.
            with suppress(ValueError, OverflowError, OSError):
                started_at = epoch_timestamp(started.strip())
        if alive:
            return store.record_compute_job_refresh(job_id, status="running", started_at=started_at)
        exit_text = read_job_file(context, record.exit_path)
        exit_status = None
        ended_at = store.now()
        status = "lost"
        diagnostic = "Compute job disappeared without an exit file."
        if exit_text is not None:
            try:
                status_text, epoch = exit_text.split()
                parsed_status = int(status_text)
                if not 0 <= parsed_status <= 255:
                    raise ValueError("compute exit file contains an invalid status")
                ended_at = epoch_timestamp(epoch)
            except (ValueError, OverflowError, OSError) as exc:
                diagnostic = safe_compute_diagnostic(
                    f"Compute job disappeared with a malformed exit receipt {record.exit_path}: {exc}"
                )
            else:
                status, exit_status, diagnostic = "exited", parsed_status, None
        # Storage resolves any concurrent cancellation intent in this transition.
        return store.record_compute_job_refresh(
            job_id,
            status=status,
            exit_status=exit_status,
            started_at=started_at,
            ended_at=ended_at,
            diagnostic=diagnostic,
        )
    except Exception as exc:
        return store.record_compute_job_refresh(
            job_id,
            status="running",
            diagnostic=safe_compute_diagnostic(str(exc)),
        )


def cancel_compute_job(
    store: AppStore,
    manifest: Manifest,
    job_id: str,
    requested_by: str,
    *,
    data_dir: Path,
) -> ComputeJobRecord:
    record = store.request_compute_job_cancel(job_id, requested_by)
    if record.status != "running":
        return record
    try:
        context = recorded_job_context(manifest, record)
        COMPUTE_BACKENDS[record.backend_id].cancel(record.backend_handle, context)
    except Exception as exc:
        return store.record_compute_job_refresh(
            job_id,
            status="running",
            diagnostic=safe_compute_diagnostic(str(exc)),
        )
    return refresh_compute_job(store, manifest, job_id, data_dir=data_dir)


def read_job_log_tail(
    store: AppStore,
    manifest: Manifest,
    job_id: str,
    *,
    data_dir: Path,
    max_bytes: int = COMPUTE_JOB_LOG_TAIL_MAX_BYTES,
) -> str:
    record = store.compute_job(job_id)
    if record is None:
        raise KeyError(job_id)
    return read_job_file(recorded_job_context(manifest, record), record.log_path, max_bytes) or ""


def epoch_timestamp(value: str) -> str:
    return datetime.fromtimestamp(int(value), tz=UTC).isoformat()
