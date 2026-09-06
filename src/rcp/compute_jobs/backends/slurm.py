from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from rcp.compute_jobs.backend_context import (
    BackendContext,
    ComputeLaunchUncertainError,
    ComputeTransportError,
    facility_probe,
)
from rcp.limits import COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest


class SlurmBackend:
    id = "slurm"
    display_name = "Slurm"

    def supports(self, os_name: str, is_remote: bool) -> bool:
        return os_name.casefold() == "linux"

    def start(
        self,
        job_root: str,
        wrapper_path: str,
        request: ComputeLaunchRequest,
        context: BackendContext,
    ) -> str:
        command = [
            "sbatch",
            "--parsable",
            "--job-name",
            f"rcp-job-{PurePosixPath(job_root).name}",
            "--output",
            f"{job_root}/log",
            "--error",
            f"{job_root}/log",
        ]
        compute = context.compute
        if compute:
            if compute.slurm_account:
                command.extend(["--account", compute.slurm_account])
            if compute.slurm_partition:
                command.extend(["--partition", compute.slurm_partition])
            command.extend(compute.slurm_submit_args)
        command.append(wrapper_path)
        try:
            result = context.run(command, timeout=COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS, check=True)
        except (subprocess.TimeoutExpired, ComputeTransportError) as exc:
            raise ComputeLaunchUncertainError(
                "Compute launch response unavailable; backend acceptance is unknown"
            ) from exc
        handle = result.stdout.strip().split(";", 1)[0]
        if not re.fullmatch(r"[0-9]+", handle):
            raise RuntimeError("Slurm submission returned no valid job id")
        return handle

    def alive(self, handle: str, context: BackendContext) -> bool | None:
        try:
            result = context.run(["squeue", "-h", "-o", "%A"])
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        return handle in result.stdout.split()

    def cancel(self, handle: str, context: BackendContext) -> None:
        result = context.run(["scancel", handle])
        if result.returncode and "invalid job id specified" not in result.stderr.casefold():
            raise RuntimeError(result.stderr or "Slurm cancellation failed")

    def probe(self, context: BackendContext) -> ComputeBackendProbe:
        return facility_probe(
            self,
            context,
            ["squeue", "-h", "-o", "%A"],
            "configure Slurm submission access for this machine",
        )
