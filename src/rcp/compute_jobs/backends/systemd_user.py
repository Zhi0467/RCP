from __future__ import annotations

import subprocess
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from rcp.compute_jobs.backend_context import (
    BackendContext,
    ComputeLaunchUncertainError,
    facility_probe,
)
from rcp.limits import COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from rcp.compute_jobs.models import ComputeBackendProbe, ComputeLaunchRequest


class SystemdUserBackend:
    id = "systemd_user"
    display_name = "systemd user manager"

    def supports(self, os_name: str, is_remote: bool) -> bool:
        return os_name.casefold() == "linux"

    def command(self, context: BackendContext, binary: str, *args: str) -> list[str]:
        return ["env", f"XDG_RUNTIME_DIR=/run/user/{context.target_uid()}", binary, "--user", *args]

    def start(
        self,
        job_root: str,
        wrapper_path: str,
        request: ComputeLaunchRequest,
        context: BackendContext,
    ) -> str:
        handle = f"rcp-job-{PurePosixPath(job_root).name}"
        command = self.command(
            context,
            "systemd-run",
            "--unit",
            handle,
            "--collect",
            "-p",
            f"WorkingDirectory={request.cwd}",
            "-p",
            f"StandardOutput=file:{job_root}/log",
            "-p",
            f"StandardError=file:{job_root}/log",
        )
        if context.containment == "mirrored" and context.writable_roots:
            command.extend(
                [
                    "-p",
                    "PrivateUsers=yes",
                    "-p",
                    "ProtectSystem=strict",
                    "-p",
                    "ProtectHome=read-only",
                ]
            )
            for root in dict.fromkeys((*context.writable_roots, job_root)):
                # systemd's list-valued property parser needs quotes independently
                # of the argv/SSH quoting boundary.
                quoted = '"' + root.replace("\\", "\\\\").replace('"', '\\"') + '"'
                command.extend(["-p", f"ReadWritePaths={quoted}"])
        command.extend(["--", "sh", wrapper_path])
        try:
            context.run(command, timeout=COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS, check=True)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # A timeout can occur after the manager accepted the unit.
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
            result = context.run(
                self.command(context, "systemctl", "show", "-p", "ActiveState", handle)
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return False if "could not be found" in result.stderr.casefold() else None
        state = result.stdout.strip().removeprefix("ActiveState=")
        if state in {"active", "activating", "reloading", "deactivating", "refreshing"}:
            return True
        return False if state in {"inactive", "failed"} else None

    def cancel(self, handle: str, context: BackendContext) -> None:
        result = context.run(self.command(context, "systemctl", "stop", handle))
        if result.returncode and not any(
            marker in result.stderr.casefold()
            for marker in ("not loaded", "could not be found", "does not exist")
        ):
            raise RuntimeError(result.stderr or "systemd cancellation failed")

    def probe(self, context: BackendContext) -> ComputeBackendProbe:
        return facility_probe(
            self,
            context,
            self.command(context, "systemctl", "show-environment"),
            "enable the execution account's systemd user manager and linger",
        )
