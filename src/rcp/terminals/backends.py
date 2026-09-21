"""Machine capabilities for member PTYs; mount profiles resist accidents only."""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rcp.config import MachineConfig
from rcp.terminals import launch
from rcp.terminals.probe import TerminalProbe

Containment = Literal["mirrored", "cooperative"]
COOPERATIVE_NOTICE = (
    "Canonical-state protection is unavailable on this machine. "
    "There is no filesystem fence around canonical state."
)


@dataclass(frozen=True)
class TerminalBackend:
    id: str
    display_name: str
    containment: Containment

    def supports(self, os_name: str, is_remote: bool) -> bool:
        if self.containment == "mirrored":
            return os_name.casefold() == "linux"
        return os_name.casefold() != "linux"

    def start(
        self,
        *,
        unit: str,
        repository: Path,
        protected_paths: list[str],
        git_read_paths: tuple[str, ...],
        git_environment: dict[str, str],
        empty_directory: Path,
        expand_environment_option: bool = True,
    ) -> tuple[subprocess.Popen[bytes], int]:
        if self.containment == "cooperative":
            return launch.launch(launch.cooperative_command(git_environment), None, cwd=repository)
        command = launch.launch_command(
            unit=unit,
            repository=repository,
            protected_paths=protected_paths,
            git_read_paths=git_read_paths,
            git_environment=git_environment,
            empty_directory=empty_directory,
            # A local host has no probe to carry its version, so read it here.
            expand_environment_option=(
                expand_environment_option and launch.local_systemd_version() >= 254
            ),
        )
        return launch.launch(command, unit)


TERMINAL_BACKENDS = {
    backend.id: backend
    for backend in (
        TerminalBackend("systemd_user", "systemd user manager", "mirrored"),
        TerminalBackend("pty", "PTY", "cooperative"),
    )
}


@dataclass(frozen=True)
class TerminalCapability:
    backend: TerminalBackend | None
    reason: str
    probe_state: str = "reachable"
    os_name: str | None = None

    @property
    def containment(self) -> Containment | None:
        return self.backend.containment if self.backend else None


def resolve_backend(os_name: str, is_remote: bool) -> TerminalBackend | None:
    return next(
        (backend for backend in TERMINAL_BACKENDS.values() if backend.supports(os_name, is_remote)),
        None,
    )


def machine_capability(
    machine: MachineConfig, probe: TerminalProbe | None = None
) -> TerminalCapability:
    if machine.host:
        if probe is None:
            return TerminalCapability(None, "Checking remote terminal capability…", "pending")
        if not probe.ready or not probe.os_name:
            return TerminalCapability(None, probe.diagnostic, probe.state, probe.os_name)
        if machine.os_account and probe.os_account and probe.os_account != machine.os_account:
            # A destination without a user takes its account from the client's
            # SSH configuration. Opening there would give the member a shell
            # under another account's home, credentials and write authority,
            # and the mount profile was computed for the registered one.
            return TerminalCapability(
                None,
                "This machine answers as a different account than the registered one, "
                "so a terminal would run with the wrong home and credentials.",
                probe.state,
                probe.os_name,
            )
        backend = resolve_backend(probe.os_name, True)
        return TerminalCapability(
            backend,
            COOPERATIVE_NOTICE if backend.containment == "cooperative" else probe.diagnostic,
            probe.state,
            probe.os_name,
        )
    backend = resolve_backend(platform.system(), False)
    if not os.access("/bin/bash", os.X_OK):
        # The cooperative helper writes its readiness marker before execv, so a
        # shell that cannot run would otherwise admit a session that is already
        # gone. The remote probe applies the same prerequisite.
        return TerminalCapability(None, "This account requires an executable /bin/bash.")
    if backend.containment == "mirrored":
        diagnostic = launch.availability_diagnostic()
        if diagnostic:
            return TerminalCapability(None, diagnostic)
        return TerminalCapability(
            backend,
            "Canonical paths require verified read-only mounts for accident resistance; "
            "the shell retains the account's authority.",
        )
    return TerminalCapability(backend, COOPERATIVE_NOTICE)
