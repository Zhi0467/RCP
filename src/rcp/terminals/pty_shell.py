"""Acquire the server-owned PTY before replacing this process with the shell."""

from __future__ import annotations

import fcntl
import os
import termios


def main() -> None:
    # Popen already created a session. A controlling terminal gives interactive
    # job control and delivers SIGHUP when the server's last master fd closes.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.write(1, b"\x1ercp-terminal-ready\x1f")
    # A login shell finds the programs the account's own login finds, as agents do.
    os.execv("/bin/bash", ["/bin/bash", "--login", "-i"])


if __name__ == "__main__":
    main()
