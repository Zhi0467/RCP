"""Probe the execution account on its own machine; shipped as module source."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys


def probe_machine(*, command_timeout: float, runner=subprocess.run) -> dict[str, str]:
    os_name = platform.system()

    def result(state: str, diagnostic: str) -> dict[str, str]:
        return {"os_name": os_name, "state": state, "diagnostic": diagnostic}

    if not os_name:
        return result("incapable", "The execution machine did not identify its operating system.")
    if not os.access("/bin/bash", os.X_OK):
        return result("incapable", "The execution account requires an executable /bin/bash.")
    if os_name.casefold() != "linux":
        return result("reachable", "Canonical-state protection is unavailable on this machine.")
    missing = [name for name in ("systemd-run", "systemctl", "findmnt") if not shutil.which(name)]
    if missing:
        return result(
            "incapable", f"Mirrored terminal launches require {', '.join(missing)}; not installed."
        )
    environment = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    commands = [
        ["systemd-run", "--version"],
        ["findmnt", "--version"],
        ["systemctl", "--user", "show-environment"],
        ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
    ]
    for command in commands:
        try:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                env=environment,
                timeout=command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return result("incapable", f"Terminal prerequisite {command[0]} timed out.")
        except (OSError, subprocess.SubprocessError) as exc:
            return result("incapable", f"Terminal prerequisite {command[0]} is unusable: {exc}")
        if completed.returncode:
            detail = completed.stderr.strip() or f"exit {completed.returncode}"
            return result("incapable", f"Terminal prerequisite {command[0]} is unusable: {detail}")
        if command[0] == "loginctl" and completed.stdout.strip() != "yes":
            return result(
                "incapable", "The execution account requires a lingering systemd user manager."
            )
    return result(
        "reachable",
        "Remote terminal prerequisites are available; launch must verify canonical-state read-only mounts.",
    )


def main(argv: list[str]) -> int:
    print(json.dumps(probe_machine(command_timeout=float(argv[1])), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
