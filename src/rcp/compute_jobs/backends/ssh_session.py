from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from rcp.compute_jobs.backend_context import (
    BackendContext,
    ComputeLaunchUncertainError,
    ComputeTransportError,
    facility_probe,
)
from rcp.limits import (
    COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS,
    COMPUTE_JOB_POLL_INTERVAL_SECONDS,
    COMPUTE_JOB_TERMINATE_GRACE_SECONDS,
)

if TYPE_CHECKING:
    from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest


def _command(*arguments: str) -> list[str]:
    from rcp.transport.state import _remote_script

    return ["python3", "-c", _remote_script("remote_job_launch.py"), *arguments]


class SSHSessionBackend:
    id = "ssh_session"
    display_name = "SSH session"

    def supports(self, os_name: str, is_remote: bool) -> bool:
        return is_remote and os_name.casefold() == "linux"

    def start(
        self,
        job_root: str,
        wrapper_path: str,
        request: ComputeLaunchRequest,
        context: BackendContext,
    ) -> str:
        if not context.execution_host:
            raise ValueError("SSH session compute requires a remote execution machine")
        try:
            result = context.run(
                _command("start", job_root, wrapper_path),
                timeout=COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS,
                check=True,
            )
        except (subprocess.TimeoutExpired, ComputeTransportError) as exc:
            raise ComputeLaunchUncertainError(
                "Compute launch response unavailable; backend acceptance is unknown"
            ) from exc
        handle = result.stdout.strip()
        if not re.fullmatch(r"[1-9][0-9]*:[0-9]+", handle):
            raise RuntimeError("SSH launcher returned an invalid process identity")
        return handle

    def alive(self, handle: str, context: BackendContext) -> bool | None:
        try:
            result = context.run(_command("alive", handle))
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        value = result.stdout.strip()
        return True if value == "alive" else False if value == "gone" else None

    def cancel(self, handle: str, context: BackendContext) -> None:
        context.run(
            _command(
                "cancel",
                handle,
                str(COMPUTE_JOB_TERMINATE_GRACE_SECONDS),
                str(COMPUTE_JOB_POLL_INTERVAL_SECONDS),
            ),
            check=True,
        )

    def probe(self, context: BackendContext) -> ComputeBackendProbe:
        if not context.execution_host:
            raise ValueError("SSH session compute requires a remote execution machine")
        return facility_probe(
            self,
            context,
            _command("probe"),
            "configure Python 3 and Linux process access on the SSH machine",
        )
