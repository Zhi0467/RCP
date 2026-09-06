from __future__ import annotations

import plistlib
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
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # Bootstrap may have succeeded before the transport stopped answering.
            try:
                self.cancel(handle, context)
            except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
                raise ComputeLaunchUncertainError(
                    "Compute launch failed and stopping the possible job could not be confirmed"
                ) from cleanup_error
            raise
        return handle

    def alive(self, handle: str, context: BackendContext) -> bool | None:
        try:
            result = context.run(["launchctl", "print", f"gui/{context.target_uid()}/{handle}"])
        except (ComputeTransportError, subprocess.TimeoutExpired, OSError):
            raise
        except (RuntimeError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return False if "could not find service" in result.stderr.casefold() else None
        state = re.search(r"^\s*state\s*=\s*(.+?)\s*$", result.stdout, re.MULTILINE)
        if not state:
            return None
        return state.group(1) in {"running", "spawn scheduled", "spawning"}

    def cancel(self, handle: str, context: BackendContext) -> None:
        result = context.run(["launchctl", "bootout", f"gui/{context.target_uid()}/{handle}"])
        if result.returncode and not any(
            marker in result.stderr.casefold()
            for marker in ("could not find service", "no such process")
        ):
            raise RuntimeError(result.stderr or "launchd cancellation failed")

    def probe(self, context: BackendContext) -> ComputeBackendProbe:
        return facility_probe(
            self,
            context,
            ["launchctl", "print", f"gui/{context.target_uid()}"],
            "make the execution account's launchd GUI domain available",
        )
