"""Probe the execution account on its own machine; shipped as module source."""

from __future__ import annotations

import json
import os
import platform
import pwd
import re
import shutil
import subprocess
import sys


def probe_machine(*, command_timeout: float, runner=subprocess.run) -> dict[str, str]:
    os_name = platform.system()

    # The destination may carry no user, in which case the account comes from
    # the client's SSH configuration and can drift away from the registered
    # one. Report what this actually is so the server can refuse a mismatch.
    try:
        os_account = pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError):
        os_account = ""

    def result(state: str, diagnostic: str) -> dict[str, str]:
        return {
            "os_name": os_name,
            "state": state,
            "diagnostic": diagnostic,
            "os_account": os_account,
        }

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
    systemd_version = 0
    commands = [
        ["systemd-run", "--version"],
        ["findmnt", "--version"],
        # `show-environment` proves the user manager answers, which is what a
        # launch needs. Linger is deliberately NOT required: the PTY lives
        # inside the SSH session that owns a session-scoped manager, and when
        # the link drops that manager tears down and takes the shell with it,
        # which is the behavior a terminal wants. A `degraded` manager still
        # answers and still runs transient units.
        ["systemctl", "--user", "show-environment"],
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
        if command[0] == "systemd-run" and not completed.returncode:
            match = re.search(r"systemd\s+(\d+)", completed.stdout or "")
            if match:
                systemd_version = int(match.group(1))
        if completed.returncode:
            detail = completed.stderr.strip() or f"exit {completed.returncode}"
            return result("incapable", f"Terminal prerequisite {command[0]} is unusable: {detail}")
    payload = result(
        "reachable",
        "Remote terminal prerequisites are available; launch must verify canonical-state read-only mounts.",
    )
    payload["systemd_version"] = systemd_version
    return payload


def main(argv: list[str]) -> int:
    print(json.dumps(probe_machine(command_timeout=float(argv[1])), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through shipped source
    raise SystemExit(main(sys.argv))
