"""Command-line entry point for independent release preparation."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from rcp_supervisor import __version__
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.install import install_release
from rcp_supervisor.releases import fetch_release, verify_release


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare verified RCP release artifacts.")
    parser.add_argument("--version", action="version", version=f"rcp-supervisor {__version__}")
    parser.add_argument("--machine-readable", action="store_true", help="Emit NDJSON step events.")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Fetch stable or one named promoted release.")
    fetch.add_argument("release", help="stable or vX.Y.Z")
    fetch.add_argument("destination", type=Path, help="Verified artifact directory to publish.")
    verify = commands.add_parser(
        "verify", help="Verify a local release bundle without network access."
    )
    verify.add_argument("bundle", type=Path)
    install = commands.add_parser(
        "install", help="Prepare a new isolated release as an unprivileged account."
    )
    install.add_argument("bundle", type=Path)
    install.add_argument("--releases-root", type=Path, required=True)
    return parser.parse_args(argv)


def _emit(command: str, state: str, message: str, *, machine_readable: bool) -> None:
    if not machine_readable:
        print(message, file=sys.stderr if state == "failed" else sys.stdout, flush=True)
        return
    # Same versioned step envelope as the server CLI, independently encoded.
    # Operator CLI delegation and its command decoder land with cutover.
    event = {
        "version": 1,
        "event": "step",
        "command": f"supervisor {command}",
        "timestamp": datetime.now(UTC).isoformat(),
        "step": {
            "number": 1,
            "title": f"Supervisor {command}",
            "purpose": "Prepare and verify release artifacts independently of the running application.",
            "performed_by": "system",
            "target": {
                "kind": "machine",
                "host": socket.gethostname(),
                "os_account": pwd.getpwuid(os.geteuid()).pw_name,
            },
            "phase": command,
            "state": state,
            "expected_success": "The requested release preparation operation succeeds.",
            "message": message,
            "actions": [],
            "fields": [],
            "resume_argv": [],
        },
    }
    print(json.dumps(event), flush=True)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    _emit(
        arguments.command,
        "running",
        f"Starting {arguments.command}.",
        machine_readable=arguments.machine_readable,
    )
    try:
        if arguments.command == "fetch":
            release = fetch_release(arguments.release, arguments.destination)
            message = f"Verified RCP {release.version} in {release.directory}."
        elif arguments.command == "verify":
            release = verify_release(arguments.bundle)
            message = f"Verified RCP {release.version} and supervisor {release.supervisor_version}."
        else:
            target = install_release(arguments.bundle, arguments.releases_root)
            message = f"Prepared RCP release at {target}."
    except (SupervisorError, OSError) as exc:
        _emit(arguments.command, "failed", str(exc), machine_readable=arguments.machine_readable)
        return 1
    _emit(arguments.command, "succeeded", message, machine_readable=arguments.machine_readable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
