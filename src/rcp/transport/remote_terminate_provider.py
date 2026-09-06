"""Stop and confirm one task-owned provider process group.

The launcher ships this module's source to the execution machine. The pidfile
belongs to its existing remote process wrapper; no process discovery is used.
"""

from __future__ import annotations

import math
import os
import signal
import stat
import sys
import time


def _read_pid(pid_file: str, timeout: float, poll_interval: float) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            descriptor = os.open(pid_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_size > 64
                ):
                    return None
                text = os.read(descriptor, 64).decode("ascii").strip()
            finally:
                os.close(descriptor)
            if text:
                pid = int(text)
                return pid if 1 < pid <= 2**31 - 1 and pid != os.getpgrp() else None
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, ValueError):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval, remaining))


def _group_stopped(pid: int, timeout: float, poll_interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval, remaining))


def terminate_provider(
    pid_file: str,
    *,
    pid_file_timeout: float,
    term_timeout: float,
    kill_timeout: float,
    poll_interval: float,
) -> bool:
    """Return true only after the owned process group is confirmed absent."""
    durations = (pid_file_timeout, term_timeout, kill_timeout, poll_interval)
    if any(not math.isfinite(value) or value < 0 for value in durations) or poll_interval == 0:
        raise ValueError(
            "Termination timeouts must be finite and nonnegative; polling must be positive."
        )
    pid = _read_pid(pid_file, pid_file_timeout, poll_interval)
    if pid is None:
        return False
    try:
        # The wrapper records its session/process-group leader. Refuse a stale or
        # malformed pidfile that currently names a member of some other group.
        if os.getpgid(pid) != pid:
            return False
    except ProcessLookupError:
        # A departed leader can leave descendants in its original group. Verify
        # and stop the group itself rather than treating leader exit as success.
        pass
    except OSError:
        return False
    for requested_signal, timeout in (
        (signal.SIGTERM, term_timeout),
        (signal.SIGKILL, kill_timeout),
    ):
        try:
            os.killpg(pid, requested_signal)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if _group_stopped(pid, timeout, poll_interval):
            return True
    return False


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        return 2
    try:
        stopped = terminate_provider(
            argv[1],
            pid_file_timeout=float(argv[2]),
            term_timeout=float(argv[3]),
            kill_timeout=float(argv[4]),
            poll_interval=float(argv[5]),
        )
    except ValueError:
        return 2
    if not stopped:
        print("Could not confirm that the provider process group stopped.", file=sys.stderr)
    return 0 if stopped else 1


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
