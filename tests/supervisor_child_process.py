"""Real process fixture for the Linux service-child parent-death regression."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path


def main() -> None:
    mode, directory, *arguments = sys.argv[1:]
    root = Path(directory)
    if mode == "parent":
        with (root / "deployment.lock").open("w") as deployment_lock:
            fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = subprocess.Popen(
                [
                    sys.executable,
                    arguments[0],
                    str(os.getpid()),
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "hold",
                    str(root),
                    str(deployment_lock.fileno()),
                ],
                pass_fds=(deployment_lock.fileno(),),
            )
        # Only the execed child now keeps this open file description locked.
        (root / "parent-closed-lock").touch()
        with child:
            child.wait(timeout=30)
        return
    if mode != "hold":
        raise ValueError("Unknown child process fixture mode.")
    with (root / "data.lock").open("w") as lock, socket.socket(socket.AF_UNIX) as listener:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        listener.bind(str(root / "control.sock"))
        listener.listen()
        receipt = {
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "deployment_lock_inode": os.fstat(int(arguments[0])).st_ino,
        }
        temporary = root / "ready.tmp"
        temporary.write_text(json.dumps(receipt))
        temporary.replace(root / "ready.json")
        signal.pause()


if __name__ == "__main__":
    main()
