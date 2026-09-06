"""Machine-owned compute backends, registered once per profile."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from rcp.compute_jobs.backend_context import ComputeBackendProfile
from rcp.compute_jobs.backends.launchd import LaunchdBackend
from rcp.compute_jobs.backends.slurm import SlurmBackend
from rcp.compute_jobs.backends.ssh_session import SSHSessionBackend
from rcp.compute_jobs.backends.systemd_user import SystemdUserBackend

if TYPE_CHECKING:
    from rcp.config import MachineComputeConfig

COMPUTE_BACKENDS: dict[str, ComputeBackendProfile] = {
    profile.id: profile
    for profile in (SystemdUserBackend(), LaunchdBackend(), SSHSessionBackend(), SlurmBackend())
}
ComputeBackendId = Literal[*COMPUTE_BACKENDS]


def resolve_backend(
    machine_compute: MachineComputeConfig | None,
    os_name: str,
    is_remote: bool,
    has_user_manager: bool,
) -> ComputeBackendProfile | None:
    if machine_compute is not None and machine_compute.backend:
        return COMPUTE_BACKENDS[machine_compute.backend]
    name = os_name.casefold()
    if name == "linux":
        if has_user_manager:
            return COMPUTE_BACKENDS["systemd_user"]
        if is_remote:
            return COMPUTE_BACKENDS["ssh_session"]
    if name in {"darwin", "macos"}:
        return COMPUTE_BACKENDS["launchd"]
    return None
