"""One non-reconnecting SSH PTY, with an explicit remote shell completion."""

from __future__ import annotations

import importlib.resources
import json
import shlex
import subprocess
from pathlib import Path

from rcp.limits import TERMINAL_STOP_TIMEOUT_SECONDS
from rcp.terminals import launch
from rcp.terminals.models import TerminalUnavailable
from rcp.transport import remote_terminal
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script


def terminal_source(name: str) -> str:
    return importlib.resources.files("rcp.terminals").joinpath(name).read_text(encoding="utf-8")


def _command(host: str, settings: dict, *, pty: bool) -> list[str]:
    command = shlex.join(
        ["python3", "-c", _remote_script("remote_terminal.py"), json.dumps(settings)]
    )
    argv = ssh_arguments(host, "exec " + command, strict_host_key_checking=True)
    if pty:
        argv.insert(1, "-tt")
    return argv


def start_remote(
    host: str,
    *,
    unit: str,
    repository: Path,
    protected_paths: list[str],
    containment: str,
    expand_environment_option: bool = True,
    git_key_relative: str | None = None,
) -> tuple[subprocess.Popen[bytes], int]:
    settings = {
        "unit": unit,
        "repository": str(repository),
        "protected_paths": protected_paths,
        "containment": containment,
        "expand_environment_option": expand_environment_option,
        "git_key_relative": git_key_relative,
        "stop_timeout": TERMINAL_STOP_TIMEOUT_SECONDS,
        "profile_source": terminal_source("profile.py"),
        "git_access_source": terminal_source("git_access.py"),
    }
    return launch.launch(_command(host, settings, pty=True), None, label="SSH PTY")


def stop_remote_unit(host: str, unit: str) -> None:
    settings = {"action": "stop", "unit": unit, "stop_timeout": TERMINAL_STOP_TIMEOUT_SECONDS}
    try:
        result = subprocess.run(
            _command(host, settings, pty=False),
            capture_output=True,
            text=True,
            timeout=TERMINAL_STOP_TIMEOUT_SECONDS * 3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TerminalUnavailable(f"Could not stop remote terminal unit {unit}: {exc}") from exc
    if result.returncode:
        raise TerminalUnavailable(
            f"Could not stop remote terminal unit {unit}: {result.stderr.strip()}"
        )


class CompletionParser:
    """Remove a completion marker even when it straddles PTY reads."""

    def __init__(self) -> None:
        self.exit_code: int | None = None
        self.pending = b""

    def feed(self, chunk: bytes) -> bytes:
        self.pending += chunk
        visible = bytearray()
        prefix, suffix = remote_terminal.EXIT_PREFIX, remote_terminal.EXIT_SUFFIX
        while self.pending:
            start = self.pending.find(prefix)
            if start < 0:
                keep = min(len(self.pending), len(prefix) - 1)
                while keep and not prefix.startswith(self.pending[-keep:]):
                    keep -= 1
                boundary = len(self.pending) - keep
                visible.extend(self.pending[:boundary])
                self.pending = self.pending[boundary:]
                break
            visible.extend(self.pending[:start])
            self.pending = self.pending[start:]
            end = self.pending.find(suffix, len(prefix))
            if end < 0 and len(self.pending) <= len(prefix) + 3:
                break
            status = self.pending[len(prefix) : end]
            if end >= 0 and status.isdigit() and 0 <= int(status) <= 255:
                self.exit_code = int(status)
                self.pending = self.pending[end + len(suffix) :]
                continue
            visible.append(self.pending[0])
            self.pending = self.pending[1:]
        return bytes(visible)

    def finish(self) -> bytes:
        pending, self.pending = self.pending, b""
        return pending
