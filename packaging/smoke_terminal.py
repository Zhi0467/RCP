"""Exercise the real frozen terminal helper, including its bootloader's signal forwarding."""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

from rcp.limits import TERMINAL_STOP_TIMEOUT_SECONDS
from rcp.terminals import launch
from rcp.terminals.profile import shell_environment


def _read_until(master: int, pattern: bytes) -> re.Match[bytes]:
    output = bytearray()
    deadline = time.monotonic() + TERMINAL_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if select.select([master], [], [], max(0, deadline - time.monotonic()))[0]:
            chunk = os.read(master, 65536)
            if not chunk:
                break
            output.extend(chunk)
            match = re.search(pattern, output)
            if match:
                return match
    raise RuntimeError(f"Frozen terminal did not produce {pattern!r}: {bytes(output)!r}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _start_job(master: int) -> int:
    os.write(master, b"sleep 120 & job=$!; printf '\\036job:%s\\037' \"$job\"\n")
    job = int(_read_until(master, rb"\x1ejob:(\d+)\x1f")[1])
    # `$!` names the job once Bash forks it. Until the job execs sleep, it
    # still runs Bash's own handlers, which swallow a Ctrl-C or hangup.
    deadline = time.monotonic() + TERMINAL_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        command = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(job)], capture_output=True, text=True
        ).stdout.strip()
        if command == "sleep":
            return job
        time.sleep(0.01)
    raise RuntimeError(f"Frozen terminal job {job} did not start sleep.")


def verify(backend: Path, directory: Path) -> None:
    for ending in ("stop", "master-close"):
        # This is the frozen cooperative command, with the production env -i
        # scrub. In particular, no PyInstaller process metadata reaches it.
        process, master = launch.launch(
            [*shell_environment({}), str(backend), "_terminal-shell"], None, cwd=directory
        )
        pids: list[int] = []
        try:
            os.write(master, b"set +H; printf '\\036shell:%s\\037' \"$$\"\n")
            pids.append(int(_read_until(master, rb"\x1eshell:(\d+)\x1f")[1]))
            if os.getsid(pids[0]) != pids[0]:
                raise RuntimeError("The frozen shell does not own its session.")
            pids.append(_start_job(master))
            if os.getpgid(pids[1]) != pids[1]:
                raise RuntimeError("The background job has no job-control process group.")
            # Exercise foreground job control too; Bash must regain the PTY
            # after Ctrl-C before it can accept the next command.
            os.write(master, b"fg\n")
            # Bash 5 ends bracketed paste with a bare \r before the job's line.
            _read_until(master, rb"[\r\n]sleep 120\r?\n")
            # Bash echoes the job before it hands it the terminal; a Ctrl-C
            # sent earlier reaches Bash and leaves the job running.
            deadline = time.monotonic() + TERMINAL_STOP_TIMEOUT_SECONDS
            while os.tcgetpgrp(master) != pids[1] and time.monotonic() < deadline:
                time.sleep(0.05)
            os.write(master, b"\x03")
            # Typed before the prompt returns, the next line can be discarded.
            _read_until(master, rb"\$ $")
            os.write(master, b"printf '\\036interactive\\037'\n")
            _read_until(master, rb"\x1einteractive\x1f")
            pids.append(_start_job(master))
            if ending == "stop":
                # The server keeps reading the PTY while it stops a session. An
                # exiting shell that leads its session waits for that read.
                stop = threading.Thread(target=launch.stop_cooperative, args=(process,))
                stop.start()
                while stop.is_alive():
                    if select.select([master], [], [], 0.05)[0]:
                        try:
                            os.read(master, 65536)
                        except OSError:
                            break
                stop.join()
            else:
                os.close(master)
                master = -1
                process.wait(timeout=TERMINAL_STOP_TIMEOUT_SECONDS)
            deadline = time.monotonic() + TERMINAL_STOP_TIMEOUT_SECONDS
            while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.05)
            if any(_alive(pid) for pid in pids):
                raise RuntimeError(f"Frozen terminal {ending} left a shell or job alive.")
        finally:
            if master >= 0:
                os.close(master)
            for pid in pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=TERMINAL_STOP_TIMEOUT_SECONDS)
