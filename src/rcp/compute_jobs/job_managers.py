"""Each job manager registers its instructions and readiness together."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rcp.compute_jobs.models import ComputeBackendProbe
from rcp.compute_jobs.scheduler_readiness import probe_slurm_access


@dataclass(frozen=True)
class JobManagerProfile:
    id: str
    instructions: str
    probe: Callable[[str, str], ComputeBackendProbe]


JOB_MANAGERS = {
    profile.id: profile
    for profile in (
        JobManagerProfile(
            id="slurm",
            instructions=(
                "Use Slurm first for compute jobs. Use the helper for processes that are not "
                "compute, such as a dashboard or a local server. Submit Slurm jobs directly "
                "with your own commands or scripts; choose the job's resources and submission "
                "arguments yourself. Slurm owns the detached job. Supply a shell check_command, "
                "log_path and cwd in watch.json, and a cancel_command such as scancel for the "
                "exact submitted job if human Cancel should be available. Queued jobs are still "
                "active. RCP checks account prerequisites but does not submit, size, or "
                "interpret scheduler jobs."
            ),
            probe=probe_slurm_access,
        ),
    )
}
