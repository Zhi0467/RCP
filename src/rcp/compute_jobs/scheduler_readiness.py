"""Scheduler access checks; submission and resource choices belong to the agent."""

from __future__ import annotations

from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.limits import COMPUTE_PROBE_TIMEOUT_SECONDS


def probe_slurm_access(machine_alias: str, execution_host: str) -> ComputeBackendProbe:
    from rcp.watchers import WatchSpec, run_watcher_check

    # Use the watcher's login shell and bounded process owner. Check access
    # without inventing a resource request or waiting for a queued job.
    result = run_watcher_check(
        WatchSpec(
            check_command=(
                "for tool in sbatch squeue scancel; do "
                'command -v "$tool" >/dev/null || { echo "Missing Slurm tool: $tool" >&2; exit 2; }; '
                "done; squeue -h -o '%A' >/dev/null || exit 2"
            ),
            log_path="/dev/null",
            cwd="/",
        ),
        execution_host,
        COMPUTE_PROBE_TIMEOUT_SECONDS,
    )
    ready = result.state == "complete"
    return ComputeBackendProbe(
        execution_machine=machine_alias,
        backend_id="slurm",
        state="ready" if ready else "failed",
        ready=ready,
        diagnostic=(
            "Slurm tools and queue are reachable under the execution account. Submission permissions and resources are checked by Slurm when the agent submits."
            if ready
            else result.error or "The execution account could not validate Slurm access."
        ),
        required_action=(
            None
            if ready
            else "Check Slurm tools and account access under the execution account. "
            "Ask the cluster administrator to repair missing access, then check again. "
            "Job-specific resources remain in the agent's submission command."
        ),
        containment="cooperative",
        status_label="Ready" if ready else "Failed",
        status_tone="ready" if ready else "error",
    )
