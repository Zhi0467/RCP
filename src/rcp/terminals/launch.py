"""PTY launch with accident-resistant mounts, never service-account isolation."""

from __future__ import annotations

import os
import pty
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from rcp.limits import (
    TERMINAL_IO_CHUNK_BYTES,
    TERMINAL_LAUNCH_TIMEOUT_SECONDS,
    TERMINAL_POLL_INTERVAL_SECONDS,
    TERMINAL_STOP_TIMEOUT_SECONDS,
)
from rcp.terminals import profile
from rcp.terminals.models import TerminalUnavailable
from rcp.terminals.profile import _READY_MARKER, shell_environment
from rcp.terminals.profile import _SHELL_PREFLIGHT as _SHELL_PREFLIGHT


def availability_diagnostic() -> str | None:
    missing = [
        name for name in ("systemd-run", "systemctl", "findmnt") if shutil.which(name) is None
    ]
    if missing:
        return f"Mirrored terminal launches require usable {', '.join(missing)}; not installed."
    if sys.platform != "linux":
        return (
            "The mirrored terminal backend requires Linux systemd-run with mount namespace support."
        )
    for command in (
        ["systemd-run", "--version"],
        ["findmnt", "--version"],
        ["systemctl", "--user", "show-environment"],
    ):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=_manager_environment(),
                timeout=TERMINAL_LAUNCH_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Terminal prerequisite {command[0]} is unusable: {exc}"
        if result.returncode:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            return f"Terminal prerequisite {command[0]} is unusable: {detail}"
    return None


def local_systemd_version(*, runner=subprocess.run) -> int:
    """This manager's major version, or 0 when it cannot be read.

    A local host is subject to the same systemd 254 boundary as a remote one:
    `--expand-environment=no` does not exist before it, and passing it kills
    every launch. The remote side learns this from its probe; locally there is
    no probe, so read it here rather than assuming the newer manager.
    """
    try:
        result = runner(
            ["systemd-run", "--version"],
            capture_output=True,
            text=True,
            env=_manager_environment(),
            timeout=TERMINAL_LAUNCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if result.returncode:
        return 0
    match = re.search(r"systemd\s+(\d+)", result.stdout or "")
    return int(match.group(1)) if match else 0


def launch_command(
    *,
    unit: str,
    repository: Path,
    protected_paths: list[str],
    git_read_paths: tuple[str, ...],
    git_environment: dict[str, str],
    empty_directory: Path,
    expand_environment_option: bool = True,
) -> list[str]:
    try:
        return profile.launch_command(
            unit=unit,
            repository=repository,
            protected_paths=protected_paths,
            git_read_paths=git_read_paths,
            git_environment=git_environment,
            empty_directory=empty_directory,
            stop_timeout=TERMINAL_STOP_TIMEOUT_SECONDS,
            expand_environment_option=expand_environment_option,
        )
    except ValueError as exc:
        raise TerminalUnavailable(str(exc)) from exc


def cooperative_command(git_environment: dict[str, str]) -> list[str]:
    return [
        *shell_environment(git_environment),
        sys.executable,
        *([] if getattr(sys, "frozen", False) else ["-m", "rcp"]),
        "_terminal-shell",
    ]


def launch(
    command: list[str], unit: str | None, *, cwd: Path | None = None, label: str = "Local PTY"
) -> tuple[subprocess.Popen[bytes], int]:
    if unit is not None:
        diagnostic = availability_diagnostic()
        if diagnostic:
            raise TerminalUnavailable(diagnostic)
    master, slave = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=_manager_environment(),
            start_new_session=True,
            cwd=cwd,
        )
        os.close(slave)
        slave = -1
        os.set_blocking(master, False)
        deadline = time.monotonic() + TERMINAL_LAUNCH_TIMEOUT_SECONDS
        output = bytearray()
        while time.monotonic() < deadline:
            # Read exactly through the preflight marker. The shell's prompt
            # remains in the PTY for the session's normal output reader.
            try:
                chunk = os.read(master, 1)
            except BlockingIOError:
                if process.poll() is None:
                    time.sleep(TERMINAL_POLL_INTERVAL_SECONDS)
                    continue
                chunk = b""
            except OSError:
                chunk = b""
            if not chunk:
                detail = output.decode(errors="replace").strip()
                raise TerminalUnavailable(
                    (
                        "systemd-run could not start the required terminal mount profile: "
                        if unit is not None
                        else f"{label} could not start: "
                    )
                    + (detail or f"exit {process.poll()}")
                )
            output.extend(chunk)
            if output.endswith(_READY_MARKER):
                return process, master
            if len(output) >= TERMINAL_IO_CHUNK_BYTES:
                raise TerminalUnavailable(
                    "Terminal preflight output exceeded its diagnostic limit."
                )
        raise TerminalUnavailable(
            "systemd-run did not confirm the required terminal profile in time."
            if unit is not None
            else f"{label} did not confirm shell startup in time."
        )
    except BaseException as exc:
        try:
            if unit is not None:
                stop_unit(unit)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=TERMINAL_STOP_TIMEOUT_SECONDS)
            os.close(master)
        if isinstance(exc, Exception) and not isinstance(exc, TerminalUnavailable):
            raise TerminalUnavailable(f"Terminal launch failed: {exc}") from exc
        raise
    finally:
        if slave >= 0:
            os.close(slave)


def stop_unit(unit: str) -> None:
    # Retiring a running unit needs systemctl alone. Demanding the launch
    # prerequisites would strand a live unit, and its shell, whenever
    # systemd-run or findmnt stopped being available after it started.
    if shutil.which("systemctl") is None:
        raise TerminalUnavailable(
            "Stopping a terminal unit requires usable systemctl; not installed."
        )
    try:
        result = subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            text=True,
            env=_manager_environment(),
            timeout=TERMINAL_STOP_TIMEOUT_SECONDS * 2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TerminalUnavailable(f"Could not stop terminal unit {unit}: {exc}") from exc
    if result.returncode:
        check = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"],
            capture_output=True,
            text=True,
            env=_manager_environment(),
            timeout=TERMINAL_STOP_TIMEOUT_SECONDS,
        )
        if check.stdout.strip() == "not-found":
            return
        raise TerminalUnavailable(f"Could not stop terminal unit {unit}: {result.stderr.strip()}")


def _manager_environment() -> dict[str, str]:
    return {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def stop_cooperative(process: subprocess.Popen[bytes]) -> None:
    """Hang up the interactive shell so it also hangs up its ordinary jobs."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGHUP)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINAL_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=TERMINAL_STOP_TIMEOUT_SECONDS)
