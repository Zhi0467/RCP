"""Own one SSH terminal and stop its unit when its SSH channel hangs up.

This module and the shared terminal profile are shipped as source. It needs
only Python's standard library on the execution machine.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXIT_PREFIX = b"\x1ercp-terminal-exit:"
EXIT_SUFFIX = b"\x1f"


def load_source(source: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": "rcp_terminal_shipped"}
    exec(compile(source, "<rcp-terminal-shipped>", "exec"), namespace)
    return namespace


def manager_environment() -> dict[str, str]:
    return {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def stop_unit(unit: str, timeout: float) -> None:
    result = subprocess.run(
        ["systemctl", "--user", "stop", unit],
        capture_output=True,
        text=True,
        env=manager_environment(),
        timeout=timeout * 2,
    )
    if result.returncode:
        check = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"],
            capture_output=True,
            text=True,
            env=manager_environment(),
            timeout=timeout,
        )
        if check.stdout.strip() != "not-found":
            raise RuntimeError(f"Could not stop terminal unit {unit}: {result.stderr.strip()}")


def hangup(signum: int, _frame: Any) -> None:
    raise InterruptedError(f"SSH terminal received signal {signum}.")


def run_session(settings: dict[str, Any]) -> int:
    profile = load_source(settings["profile_source"])
    git_access = load_source(settings["git_access_source"])
    repository = Path(settings["repository"]).expanduser().resolve(strict=True)
    protected = list(
        dict.fromkeys(
            value
            for path in settings["protected_paths"]
            for value in (str(Path(path).expanduser()), str(Path(path).expanduser().resolve()))
        )
    )
    if any(repository == Path(path) or Path(path) in repository.parents for path in protected):
        raise ValueError("Canonical state cannot be a terminal repository.")
    timeout = settings["stop_timeout"]
    mirrored = settings["containment"] == "mirrored"
    if settings["containment"] not in {"mirrored", "cooperative"}:
        raise ValueError("Unknown terminal profile.")
    child: subprocess.Popen[bytes] | None = None
    signal.signal(signal.SIGHUP, hangup)
    signal.signal(signal.SIGTERM, hangup)
    # The shell handles Ctrl-C itself, including while a foreground job runs.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with tempfile.TemporaryDirectory(prefix="rcp-terminal-") as empty:
            # The far side is the only place that knows this account's home,
            # so the server sends the key's path relative to it. A team
            # repository's deploy key is the only Git credential a remote
            # session gets; the shell scrubs the ambient environment.
            relative = settings.get("git_key_relative")
            key = (Path.home() / relative) if relative else None
            paths, environment = git_access["terminal_git_access"](key)
            if mirrored:
                command = profile["launch_command"](
                    unit=settings["unit"],
                    repository=repository,
                    protected_paths=protected,
                    git_read_paths=paths,
                    git_environment=environment,
                    empty_directory=Path(empty),
                    stop_timeout=timeout,
                    expand_environment_option=bool(settings.get("expand_environment_option", True)),
                )
            else:
                command = [
                    *profile["shell_environment"](environment),
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    "printf '\\036rcp-terminal-ready\\037'; exec /bin/bash --noprofile --norc -i",
                ]
            child = subprocess.Popen(command, cwd=repository, env=manager_environment())
            status = child.wait()
            status = status if status >= 0 else 128 - status
            # Shell completion remains evidence even if subsequent cleanup fails.
            os.write(1, EXIT_PREFIX + str(status).encode("ascii") + EXIT_SUFFIX)
    finally:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if mirrored:
                stop_unit(settings["unit"], timeout)
        finally:
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGHUP)
                try:
                    child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=timeout)
    return status


def main() -> int:
    try:
        settings = json.loads(sys.argv[1])
        if settings.get("action") == "stop":
            stop_unit(settings["unit"], settings["stop_timeout"])
            return 0
        return run_session(settings)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"SSH terminal: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
