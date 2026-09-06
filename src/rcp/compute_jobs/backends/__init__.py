"""Machine-owned compute backends, registered once per profile."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from rcp.compute_jobs.backend_context import ComputeBackendProfile
from rcp.compute_jobs.backends.systemd_user import SystemdUserBackend

if TYPE_CHECKING:
    from rcp.config import MachineComputeConfig

COMPUTE_BACKENDS: dict[str, ComputeBackendProfile] = {
    profile.id: profile for profile in (SystemdUserBackend(),)
}
ComputeBackendId = Literal[*COMPUTE_BACKENDS]
UNAVAILABLE_BACKEND_GUIDANCE = (
    "Use a Linux machine with a reachable systemd user manager, "
    "or opt into Slurm and submit jobs directly."
)


def resolve_backend(
    machine_compute: MachineComputeConfig | None,
    os_name: str,
    is_remote: bool,
    has_user_manager: bool,
) -> ComputeBackendProfile | None:
    if machine_compute is not None and machine_compute.job_manager is not None:
        return None
    name = os_name.casefold()
    if name == "linux" and has_user_manager:
        return COMPUTE_BACKENDS["systemd_user"]
    return None
