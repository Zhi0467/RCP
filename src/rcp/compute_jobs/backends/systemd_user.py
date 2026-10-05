from __future__ import annotations

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


def _quote_path(path: str) -> str:
    # systemd's list-valued property parser needs quotes independently
    # of the argv/SSH quoting boundary.
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
                command.extend(["-p", f"ReadWritePaths={_quote_path(root)}"])
            for path in context.protected_paths:
                command.extend(["-p", f"ReadOnlyPaths={_quote_path(path)}"])
        command.extend(["--", "sh", wrapper_path])
        try:
            context.run(command, timeout=COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS, check=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            # A timeout can occur after the manager accepted the unit.
            try:
                self.cancel(handle, context)
            except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
                raise ComputeLaunchUncertainError(
                    "Compute launch failed and stopping the possible job could not be confirmed"
                ) from cleanup_error
            if isinstance(exc, (subprocess.TimeoutExpired, ComputeTransportError)):
                # A short unit may already have run and been collected; keep its receipts.
                raise ComputeLaunchUncertainError(
                    "Compute launch lost contact after the manager may have run the unit"
                ) from exc
            raise
        return handle

    def start_account_service(
        self, service_root: str, argv: list[str], context: BackendContext
    ) -> None:
        """Start one stable account service without stopping a concurrent owner.

        systemd owns uniqueness. A duplicate or uncertain submission is followed
        by the caller's bounded readiness check, never by cancellation.
        """
        handle = f"rcp-{PurePosixPath(service_root).name}"
        context.run(
            self.command(
                context,
                "systemd-run",
                "--unit",
                handle,
                "--collect",
                "-p",
                "UMask=0077",
                "--",
                *argv,
            ),
            timeout=COMPUTE_JOB_LAUNCH_TIMEOUT_SECONDS,
        )

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
        facility = facility_probe(
            self,
            context,
            self.command(context, "systemctl", "show-environment"),
            "enable the execution account's systemd user manager and linger",
        )
        if not facility.ready:
            return facility
        # Without linger the user manager stops ten seconds after the account's
        # last session ends, and every job it owns stops with it.
        try:
            linger = context.run(
                ["loginctl", "show-user", context.target_uid(), "-p", "Linger", "--value"]
            )
            if linger.returncode == 0 and linger.stdout.strip() == "yes":
                return facility
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        return facility.model_copy(
            update={
                "state": "failed",
                "ready": False,
                "diagnostic": "Jobs stop when this account's last login session ends.",
                "required_action": "Allow background processes on this machine's card in Settings.",
                "status_label": "Failed",
                "status_tone": "error",
            }
        )
