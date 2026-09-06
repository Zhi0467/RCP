"""Exec a service child whose lifetime cannot outlive its Linux coordinator.

Run this helper after subprocess has dropped credentials. Linux clears the
parent-death setting on credential changes; ordinary exec preserves it.
See https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys

_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38


def arm_parent_death(parent_pid: int) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Service child parent-death ownership requires Linux.")
    if parent_pid <= 1 or os.getppid() != parent_pid:
        raise RuntimeError("The service child coordinator disappeared before launch.")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    # prctl is variadic: explicitly pass the kernel's unsigned-long arguments.
    prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    prctl.restype = ctypes.c_int
    for option, value in (
        (_PR_SET_NO_NEW_PRIVS, 1),
        (_PR_SET_PDEATHSIG, signal.SIGKILL),
    ):
        if prctl(option, value, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "Could not establish service child process ownership")
    # No signal is delivered retroactively if the parent died before prctl.
    if os.getppid() != parent_pid:
        raise RuntimeError("The service child coordinator disappeared while arming ownership.")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if len(arguments) < 2 or not os.path.isabs(arguments[1]):
            raise ValueError(
                "Service child launch requires its parent PID and absolute executable."
            )
        arm_parent_death(int(arguments[0]))
        # Exec retains the PID checked by the coordinator and the kernel death
        # signal. no_new_privs prevents an executable from acquiring privileges.
        os.execv(arguments[1], arguments[1:])
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
