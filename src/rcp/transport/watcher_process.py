"""Run a watcher check in its own process group, locally or shipped over SSH."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from contextlib import suppress


def watcher_shell_command(check_command: str, cwd: str) -> list[str]:
    # Interactive Bash otherwise places background/pipeline children in new
    # process groups, outside the timeout's cleanup group.
    return ["bash", "-lic", f"set +m\ncd -- {shlex.quote(cwd)} && {check_command}"]


def run_check_process(
    command: list[str], *, cwd: str | None, timeout: float
) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the check's group, including children still holding its pipes.
            # The external job being observed is not part of this process group.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def main() -> int:
    # A disconnected SSH client must not remove the remote timeout owner before
    # it has cleaned up the check. The watchdog remains bounded by this timeout.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    cwd, timeout_text, check_command = sys.argv[1:]
    timeout = float(timeout_text)
    try:
        result = run_check_process(
            watcher_shell_command(check_command, cwd), cwd=cwd, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        for output in (exc.stdout, exc.stderr):
            if output:
                sys.stderr.write(
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else output
                )
        print(f"check timed out after {timeout:g} seconds", file=sys.stderr)
        return 124
    except OSError as exc:
        print(f"could not execute check: {exc}", file=sys.stderr)
        return 125
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
