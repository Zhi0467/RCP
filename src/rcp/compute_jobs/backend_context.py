from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from rcp.limits import COMPUTE_JOB_STATUS_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest
    from rcp.config import MachineComputeConfig


class ComputeLaunchUncertainError(RuntimeError):
    """The backend may own a job; retain its launch intent for recovery."""


@dataclass
class BackendContext:
    execution_host: str
    execution_machine: str
    compute: MachineComputeConfig | None
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    uid: str = ""
    os_name: str = ""
    containment: Literal["mirrored", "cooperative"] = "cooperative"
    writable_roots: tuple[str, ...] = ()

    def run(
        self,
        argv: list[str],
        *,
        timeout: float = COMPUTE_JOB_STATUS_TIMEOUT_SECONDS,
        input: str | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        from rcp.transport.ssh import ssh_arguments

        command = (
            ssh_arguments(self.execution_host, shlex.join(argv)) if self.execution_host else argv
        )
        result = self.runner(command, capture_output=True, text=True, timeout=timeout, input=input)
        if check and result.returncode:
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or "Compute command failed"
            )
        return result

    def target_uid(self) -> str:
        if not self.uid:
            self.uid = self.run(["id", "-u"], check=True).stdout.strip()
        if not self.uid.isdecimal():
            raise ValueError("Execution account uid is not numeric")
        return self.uid


class ComputeBackendProfile(Protocol):
    id: str
    display_name: str

    def supports(self, os_name: str, is_remote: bool) -> bool: ...
    def start(
        self,
        job_root: str,
        wrapper_path: str,
        request: ComputeLaunchRequest,
        context: BackendContext,
    ) -> str: ...
    def alive(self, handle: str, context: BackendContext) -> bool | None: ...
    def cancel(self, handle: str, context: BackendContext) -> None: ...
    def probe(self, context: BackendContext) -> ComputeBackendProbe: ...


def facility_probe(
    profile: ComputeBackendProfile,
    context: BackendContext,
    command: list[str],
    required_action: str,
) -> ComputeBackendProbe:
    from rcp.compute_jobs.models import ComputeBackendProbe

    try:
        result = context.run(command)
        ready = result.returncode == 0
        diagnostic = (
            "Backend facility is reachable; a job probe is still required."
            if ready
            else (result.stderr or result.stdout or "Backend facility is unavailable.")
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        ready, diagnostic = False, str(exc)
    return ComputeBackendProbe(
        execution_machine=context.execution_machine,
        backend_id=profile.id,
        state="ready" if ready else "failed",
        ready=ready,
        diagnostic=diagnostic,
        required_action=None if ready else required_action,
        containment=context.containment,
        cgroup_isolated=None,
        status_label="Ready" if ready else "Failed",
        status_tone="ready" if ready else "error",
    )
