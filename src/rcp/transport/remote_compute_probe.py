"""Probe one compute resource from the account where an RCP agent runs.

The backend ships this module's source to a remote execution machine when
needed. It never accepts credentials and keeps OpenSSH host-key verification
strict.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Literal

ComputeProbeState = Literal[
    "reachable",
    "unreachable",
    "authentication_failed",
    "host_key_failed",
]


def classify_ssh_failure(stderr: str) -> ComputeProbeState:
    detail = stderr.casefold()
    if any(
        marker in detail
        for marker in (
            "host key verification failed",
            "remote host identification has changed",
            "no host key is known",
            "offending key",
        )
    ):
        return "host_key_failed"
    if any(
        marker in detail
        for marker in (
            "permission denied",
            "authentication failed",
            "no supported authentication methods",
            "too many authentication failures",
        )
    ):
        return "authentication_failed"
    return "unreachable"


def probe_connection(
    kind: Literal["local", "ssh"],
    ssh_target: str,
    *,
    runner=subprocess.run,
) -> dict[str, str]:
    if kind == "local":
        return {"state": "reachable", "diagnostic": "Available on the agent machine."}
    result = runner(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "StrictHostKeyChecking=yes",
            # OpenSSH documents ``-S none`` as disabling connection sharing.
            "-S",
            "none",
            ssh_target,
            "true",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode == 0:
        return {"state": "reachable", "diagnostic": "SSH connection succeeded."}
    diagnostic = (result.stderr or result.stdout).strip() or "SSH connection failed."
    return {
        "state": classify_ssh_failure(diagnostic),
        "diagnostic": diagnostic[:600],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"local", "ssh"}:
        return 2
    try:
        result = probe_connection(argv[1], argv[2])
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {"state": "unreachable", "diagnostic": str(exc)[:600]}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
