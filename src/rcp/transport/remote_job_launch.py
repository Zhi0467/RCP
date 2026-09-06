"""Shipped stdlib-only Linux job session launch and identity checks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def process_identity(pid: int) -> tuple[str, str]:
    # comm (field 2) may contain spaces and parentheses; fields after its final
    # ')' begin at state (field 3), so starttime (field 22) is index 19.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19], fields[0]


def parse_handle(handle: str) -> tuple[int, str]:
    pid_text, starttime = handle.split(":")
    pid = int(pid_text)
    if pid <= 1 or pid == os.getpgrp() or not starttime.isdecimal():
        raise ValueError("Invalid compute process identity")
    return pid, starttime


def alive(handle: str) -> bool:
    pid, expected = parse_handle(handle)
    try:
        actual, state = process_identity(pid)
    except FileNotFoundError:
        return _group_alive(pid)
    if actual != expected:
        # A recycled leader pid is proof this is not the recorded job.
        return False
    return state not in {"Z", "X"} or _group_alive(pid)


def launch(job_root: str, wrapper: str) -> str:
    root = Path(job_root)
    with (root / "log").open("ab") as log:
        process = subprocess.Popen(
            ["sh", wrapper],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    try:
        starttime, _ = process_identity(process.pid)
        (root / "pid").write_text(str(process.pid))
        (root / "starttime").write_text(starttime)
        return f"{process.pid}:{starttime}"
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise


def _group_alive(pid: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        # Group and session both equal the leader pid: launch() used setsid.
        if fields[2] == str(pid) and fields[3] == str(pid) and fields[0] not in {"Z", "X"}:
            return True
    return False


def cancel(handle: str, grace: float, poll_interval: float) -> None:
    if not alive(handle):
        return
    pid, _ = parse_handle(handle)
    for requested_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, requested_signal)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + grace
        while _group_alive(pid):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
        else:
            return
    raise RuntimeError("Could not confirm that the compute process group stopped")


def main(argv: list[str]) -> int:
    try:
        action = argv[1]
        if action == "start" and len(argv) == 4:
            print(launch(argv[2], argv[3]), flush=True)
        elif action == "alive" and len(argv) == 3:
            print("alive" if alive(argv[2]) else "gone")
        elif action == "cancel" and len(argv) == 5:
            grace, poll_interval = float(argv[3]), float(argv[4])
            if grace <= 0 or poll_interval <= 0:
                raise ValueError("Compute cancellation intervals must be positive")
            cancel(argv[2], grace, poll_interval)
        elif action == "probe" and len(argv) == 2:
            process_identity(os.getpid())
        else:
            return 2
    except (OSError, ValueError, IndexError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
