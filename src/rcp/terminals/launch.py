"""PTY launch with accident-resistant mounts, never service-account isolation."""

from __future__ import annotations

import os
import pty
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
from rcp.terminals.models import TerminalUnavailable

_READY_MARKER = b"\x1ercp-terminal-ready\x1f"
_SHELL_PREFLIGHT = r"""
if ! test -w "$PWD"; then
    printf '%s\n' 'Terminal repository is not writable under the required mount profile.' >&2
    exit 1
fi
if ! command -v findmnt >/dev/null; then
    printf '%s\n' 'findmnt is required to verify canonical-state read-only mounts.' >&2
    exit 1
fi
for protected_path do
    if ! test -e "$protected_path" || test -w "$protected_path"; then
        printf '%s\n' 'Terminal canonical-state read-only mounts are unavailable.' >&2
        exit 1
    fi
    case ",$(findmnt --noheadings --output VFS-OPTIONS --target "$protected_path")," in
        *,ro,*) ;;
        *) printf '%s\n' 'Terminal canonical-state mount verification failed.' >&2; exit 1 ;;
    esac
done
printf '\036rcp-terminal-ready\037'
exec /bin/bash --noprofile --norc -i
"""


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


def launch_command(
    *,
    unit: str,
    repository: Path,
    protected_paths: list[str],
    git_read_paths: tuple[str, ...],
    git_environment: dict[str, str],
    empty_directory: Path,
) -> list[str]:
    """Build the required profile; failed properties refuse launch."""
    properties = [
        "PrivateUsers=yes",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "NoNewPrivileges=yes",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        f"TimeoutStopSec={TERMINAL_STOP_TIMEOUT_SECONDS}",
        f"BindPaths={_path(str(repository))}",
        "StandardOutput=tty",
        "StandardError=tty",
    ]
    for path in git_read_paths:
        properties.extend([f"BindReadOnlyPaths={_path(path)}", f"ReadOnlyPaths={_path(path)}"])
    for path in protected_paths:
        if Path(path).exists():
            properties.extend([f"BindReadOnlyPaths={_path(path)}", f"ReadOnlyPaths={_path(path)}"])
        else:
            # Mount a read-only empty directory rather than ignoring an absent
            # deny and allowing the shell to create writable canonical state.
            properties.append(f"BindReadOnlyPaths={_path(str(empty_directory))}:{_path(path)}")
    command = [
        "systemd-run",
        "--user",
        "--pty",
        "--wait",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--expand-environment=no",
        f"--unit={unit}",
        f"--working-directory={repository}",
    ]
    for property_value in properties:
        command.extend(["--property", property_value])
    command.extend(["--", *shell_environment(git_environment)])
    command.extend(
        [
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            _SHELL_PREFLIGHT,
            "rcp-terminal",
            *protected_paths,
        ]
    )
    return command


def shell_environment(git_environment: dict[str, str]) -> list[str]:
    # Discard ambient provider secrets for either backend. Git receives only
    # its concrete repository access settings.
    environment = {
        "HOME": str(Path.home()),
        "USER": os.environ.get("USER", "rcp"),
        "LOGNAME": os.environ.get("LOGNAME", "rcp"),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "HISTFILE": "/dev/null",
        **git_environment,
    }
    return ["/usr/bin/env", "-i", *(f"{name}={value}" for name, value in environment.items())]


def cooperative_command(git_environment: dict[str, str]) -> list[str]:
    return [
        *shell_environment(git_environment),
        sys.executable,
        *([] if getattr(sys, "frozen", False) else ["-m", "rcp"]),
        "_terminal-shell",
    ]


def launch(
    command: list[str], unit: str | None, *, cwd: Path | None = None
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
                        else "Local PTY could not start: "
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
            else "Local PTY did not confirm shell startup in time."
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
    diagnostic = availability_diagnostic()
    if diagnostic:
        raise TerminalUnavailable(diagnostic)
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


def _path(value: str) -> str:
    if any(character in value for character in (":", "\n", "\r", "\0")):
        raise TerminalUnavailable(
            "Terminal mount paths cannot contain colons or control characters."
        )
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
