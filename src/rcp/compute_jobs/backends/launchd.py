from __future__ import annotations

import plistlib
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
from rcp.transport.compute_process_owner import owner_alive, owner_command, require_cancel_success

if TYPE_CHECKING:
    from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest


class LaunchdBackend:
    id = "launchd"
    display_name = "launchd"

    def supports(self, os_name: str, is_remote: bool) -> bool:
        return os_name.casefold() in {"darwin", "macos"}

    def start(
        self,
        job_root: str,
        wrapper_path: str,
        request: ComputeLaunchRequest,
        context: BackendContext,
    ) -> str:
        from rcp.compute_jobs.files import write_job_file

        handle = f"rcp-job-{PurePosixPath(job_root).name}"
        plist = plistlib.dumps(
            {
                "Label": handle,
                "ProgramArguments": ["/bin/sh", wrapper_path],
                "RunAtLoad": True,
                "KeepAlive": False,
                "StandardOutPath": f"{job_root}/log",
                "StandardErrorPath": f"{job_root}/log",
            }
        ).decode()
        path = f"{job_root}/job.plist"
        write_job_file(context, path, plist)
        try:
            context.run(
                ["launchctl", "bootstrap", f"gui/{context.target_uid()}", path],
                timeout=COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            # Bootstrap may have succeeded before the transport stopped answering.
            try:
                self.cancel(handle, context)
            except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
                raise ComputeLaunchUncertainError(
                    "Compute launch failed and stopping the possible job could not be confirmed"
                ) from cleanup_error
            if isinstance(exc, (subprocess.TimeoutExpired, ComputeTransportError)):
                # The job may have finished before cleanup; retain its receipts.
                raise ComputeLaunchUncertainError(
                    "Compute launch lost contact after launchd may have run the job"
                ) from exc
            raise
        return handle

    def alive(self, handle: str, context: BackendContext) -> bool | None:
        try:
            result = context.run(owner_command(self.id, handle, context.target_uid()))
        except (ComputeTransportError, subprocess.TimeoutExpired, OSError):
            raise
        except (RuntimeError, subprocess.SubprocessError):
            return None
        return owner_alive(self.id, result)

    def cancel(self, handle: str, context: BackendContext) -> None:
        result = context.run(owner_command(self.id, handle, context.target_uid(), cancel=True))
        require_cancel_success(self.id, result)

    def probe(self, context: BackendContext) -> ComputeBackendProbe:
        return facility_probe(
            self,
            context,
            ["launchctl", "print", f"gui/{context.target_uid()}"],
            "make the execution account's launchd GUI domain available",
        )
