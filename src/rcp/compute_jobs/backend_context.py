from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol

from rcp.limits import COMPUTE_JOB_STATUS_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from rcp.compute_jobs.models import ComputeBackendProbe, ComputeJobRecord, ComputeLaunchRequest
    from rcp.config import MachineComputeConfig
    from rcp.storage import AppStore
    from rcp.storage.models import SpaceMachineRecord


class ComputeLaunchUncertainError(RuntimeError):
    """The backend may own a job; retain its launch intent for recovery."""


class ComputeProbeStaleError(ValueError):
    """The stored probe no longer matches the resolved execution backend."""


class ComputeTransportError(RuntimeError):
    """SSH transport failed; the remote command's outcome is unknown."""


@dataclass
class BackendContext:
    execution_host: str
    machine_id: str
    compute: MachineComputeConfig | None
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    uid: str = ""
    os_name: str = ""
    containment: Literal["mirrored", "cooperative"] = "cooperative"
    writable_roots: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()

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
        if result.returncode and (check or (self.execution_host and result.returncode == 255)):
            diagnostic = result.stderr.strip() or result.stdout.strip() or "Compute command failed"
            if self.execution_host and result.returncode == 255:
                raise ComputeTransportError(diagnostic)
            raise RuntimeError(diagnostic)
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
    def start_account_service(
        self, service_root: str, argv: list[str], context: BackendContext
    ) -> None: ...
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
        machine_id=context.machine_id,
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


def resolve_context(
    machine: SpaceMachineRecord,
    runner=subprocess.run,
) -> tuple[BackendContext, ComputeBackendProfile | None]:
    """Resolve identity and OS on the card's machine, never on its client.

    The route and compute settings are read from the card now, as the job runs.
    """
    from rcp.compute_jobs.backends import resolve_backend
    from rcp.config import MachineComputeConfig

    context = BackendContext(
        machine_id=machine.machine_id,
        execution_host=machine.host,
        compute=machine.compute_config() or MachineComputeConfig(),
        runner=runner,
    )
    os_result = context.run(["uname", "-s"])
    uid_result = context.run(["id", "-u"])
    if os_result.returncode or uid_result.returncode:
        raise RuntimeError(
            os_result.stderr or uid_result.stderr or "Cannot resolve compute machine identity."
        )
    uid = uid_result.stdout.strip()
    if not uid.isdecimal():
        raise RuntimeError("The compute machine returned an invalid user id.")
    context = replace(context, os_name=os_result.stdout.strip(), uid=uid)
    has_user_manager = False
    if context.os_name == "Linux":
        try:
            manager = context.run(
                [
                    "env",
                    f"XDG_RUNTIME_DIR=/run/user/{context.uid}",
                    "systemctl",
                    "--user",
                    "show-environment",
                ]
            )
        except FileNotFoundError:
            pass
        else:
            has_user_manager = manager.returncode == 0
    profile = resolve_backend(
        context.compute,
        context.os_name,
        bool(context.execution_host),
        has_user_manager,
    )
    return context, profile


def recorded_job_context(store: AppStore, record: ComputeJobRecord) -> BackendContext:
    """A launched job's context on its card's current route.

    Its root and handle are recorded, so compute settings do not matter.
    """
    from rcp.machine_cards import host_of

    return BackendContext(
        execution_host=host_of(store, record.machine_id),
        machine_id=record.machine_id,
        compute=None,
        containment=record.containment,
    )
