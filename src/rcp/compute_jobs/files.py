"""Job files live on the execution machine, outside stage retention."""

from __future__ import annotations

import json
from pathlib import Path

from rcp.compute_jobs.backend_context import BackendContext
from rcp.compute_jobs.models import ComputeLaunchRequest
from rcp.compute_jobs.wrapper import command_provenance, render_wrapper
from rcp.limits import COMPUTE_JOB_LOG_TAIL_MAX_BYTES
from rcp.transport.remote_job_files import operate
from rcp.transport.state import _remote_script


def _operate(context: BackendContext, operation: str, path: str, payload: str = "") -> str | None:
    if not context.execution_host:
        return operate(operation, path, payload)
    result = context.run(
        ["python3", "-c", _remote_script("remote_job_files.py"), operation, path, payload],
        check=True,
    )
    return json.loads(result.stdout)


def resolve_jobs_root(context: BackendContext, data_dir: Path) -> str:
    if not context.execution_host:
        return str(data_dir.resolve() / "jobs")
    configured = context.compute.jobs_root if context.compute else ""
    root = _operate(context, "resolve", configured or "~/.rcp/jobs")
    if root is None or not Path(root).is_absolute():
        raise ValueError("compute jobs root must resolve to an absolute path")
    return root


def prepare_job_root(
    context: BackendContext,
    job_root: str,
    request: ComputeLaunchRequest,
    *,
    project_id: str,
    origin_operation_id: str,
    episode_id: str | None,
) -> None:
    _operate(
        context,
        "prepare",
        job_root,
        json.dumps(
            {
                "run.sh": render_wrapper(job_root, request),
                "owner.py": _remote_script("compute_process_owner.py"),
                "command.json": command_provenance(
                    request,
                    project_id=project_id,
                    origin_operation_id=origin_operation_id,
                    episode_id=episode_id,
                ),
            }
        ),
    )


def read_job_file(
    context: BackendContext,
    path: str,
    max_bytes: int = COMPUTE_JOB_LOG_TAIL_MAX_BYTES,
) -> str | None:
    if not 0 < max_bytes <= COMPUTE_JOB_LOG_TAIL_MAX_BYTES:
        raise ValueError("compute file read exceeds the byte limit")
    return _operate(context, "read", path, str(max_bytes))


def write_job_file(context: BackendContext, path: str, content: str) -> None:
    _operate(context, "write", path, content)


def remove_job_root(context: BackendContext, root: str) -> None:
    _operate(context, "remove", root)
