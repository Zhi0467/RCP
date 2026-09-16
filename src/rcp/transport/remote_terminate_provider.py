"""Stop and confirm one task-owned provider process group.

The launcher ships this module's source to the execution machine. The pidfile
belongs to its existing remote process wrapper; no process discovery is used.
"""

from __future__ import annotations

import json
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


def provider_stopped(pid_file: str) -> bool | None:
    """Inspect the owned group without waiting or delivering a signal.

    True proves absence; False means the group exists. None means ownership or
    process state could not be established and must not authorize another run.
    """
    pid = _read_pid(pid_file, timeout=0, poll_interval=1)
    if pid is None:
        return None
    try:
        if os.getpgid(pid) != pid:
            return None
    except ProcessLookupError:
        # The leader can exit while its descendants retain the group.
        pass
    except OSError:
        return None
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return None
    return False


def journal_idle_seconds(pid_file: str) -> float | None:
    """Seconds since this turn's journal last grew, or None if it never has.

    A provider that is working writes something; one that is wedged does not.
    Nothing here judges which of those it is looking at. The probe answers it
    because the probe is already here, on the shared connection that survives
    the run's own: asking the stage separately would ask over the very link this
    situation exists because of.
    """

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    root, name = os.path.split(pid_file)
    try:
        root_fd = os.open(root, flags)
    except OSError:
        return None
    try:
        journal_fd = os.open(name + ".turn", flags, dir_fd=root_fd)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    written = []
    try:
        for entry in ("events.jsonl", "stderr.txt"):
            try:
                written.append(os.stat(entry, dir_fd=journal_fd, follow_symlinks=False).st_mtime)
            except OSError:
                continue
    finally:
        os.close(journal_fd)
    if not written:
        return None
    # A host clock behind the reader's would otherwise report the future.
    return max(0.0, time.time() - max(written))


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
    if len(argv) == 3 and argv[1] == "--probe":
        stopped = provider_stopped(argv[2])
        if stopped is False:
            # Only a live group raises the question this answers, and only the
            # exit code decides the verdict; unreadable silence prints nothing.
            print(json.dumps({"idle_seconds": journal_idle_seconds(argv[2])}))
        return 2 if stopped is None else (0 if stopped else 1)
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
