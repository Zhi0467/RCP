"""Command-line entry point for independent release preparation."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from rcp_supervisor import __version__
from rcp_supervisor.errors import SupervisorError
from rcp_supervisor.events import os_account
from rcp_supervisor.install import install_release
from rcp_supervisor.releases import fetch_release, verify_release


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare verified RCP release artifacts.")
    parser.add_argument("--version", action="version", version=f"rcp-supervisor {__version__}")
    parser.add_argument(
        "--plan", action="store_true", help="Describe the requested command without side effects."
    )
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
    recover = commands.add_parser(
        "recover", help="Recover an interrupted deployment using local state."
    )
    recover.add_argument("--startup", action="store_true")
    launch = commands.add_parser("launch", help="Execute the root-selected application as rcp.")
    launch.add_argument("arguments", nargs=argparse.REMAINDER)
    operator = commands.add_parser(
        "operator", help="Run a retained privileged console owner from protected code."
    )
    operator.add_argument("arguments", nargs=argparse.REMAINDER)
    server = commands.add_parser("server", help="Operate the installed machine.")
    operations = server.add_subparsers(dest="server_command", required=True)
    initial = operations.add_parser("install")
    initial.add_argument("--team-name", default="Research team")
    update = operations.add_parser("update")
    update.add_argument("--confirm-target")
    restore = operations.add_parser("restore")
    restore.add_argument("archive_path", type=Path)
    restore.add_argument("--identity-file", type=Path)
    restore.add_argument("--confirm-data-dir")
    restore.add_argument(
        "--old-authority-disposition",
        choices=("old-machine-destroyed", "old-machine-fenced-and-credentials-revoked"),
    )
    restore.add_argument("--confirm-old-authority")
    restore.add_argument("--confirm-member-roster")
    restore.add_argument("--remove-stale-member")
    doctor = operations.add_parser("doctor")
    supervisor = operations.add_parser("supervisor")
    self_update = supervisor.add_subparsers(dest="supervisor_command", required=True).add_parser(
        "update"
    )
    # Public wrappers preserve the application's flags at any server command
    # depth. Passthrough launch/operator arguments belong to the app parser.
    for command in (server, initial, update, restore, doctor, supervisor, self_update):
        command.add_argument("--plan", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("--machine-readable", action="store_true", default=argparse.SUPPRESS)
    values = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(values) and values[index] in {"--plan", "--machine-readable"}:
        index += 1
    if index < len(values) and values[index] in {"launch", "operator"}:
        arguments = parser.parse_args(values[: index + 1])
        arguments.arguments = values[index + 1 :]
        return arguments
    return parser.parse_args(values)


def _invocation(argv: list[str] | None) -> list[str]:
    """The exact installed server command as typed, without wizard-mode flags."""
    values = list(sys.argv[1:] if argv is None else argv)
    return [value for value in values if value not in {"--plan", "--machine-readable"}]


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
                "os_account": os_account(),
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
    if arguments.command == "operator":
        from rcp_supervisor.launch import launch_operator

        try:
            if arguments.plan:
                raise SupervisorError(
                    "Operator commands provide their own application console plan."
                )
            return launch_operator(arguments.arguments)
        except (SupervisorError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if arguments.command == "launch":
        if arguments.plan:
            print("Launch does not have an operator plan.", file=sys.stderr)
            return 1
        from rcp_supervisor.launch import main as launch_main

        return launch_main(arguments.arguments)
    if arguments.command == "recover":
        from rcp_supervisor.driver import recover

        try:
            if arguments.plan:
                raise SupervisorError("Recovery is a startup guard, not an operator plan.")
            recover(startup=arguments.startup)
            return 0
        except (SupervisorError, OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if arguments.command == "server":
        from rcp_supervisor import driver
        from rcp_supervisor.events import EventEmitter, plan

        command = f"server {arguments.server_command}"
        if arguments.server_command == "supervisor":
            command += " update"
        if arguments.plan:
            print(json.dumps(plan(command)), flush=True)
            return 0
        emitter = EventEmitter(
            command,
            machine_readable=arguments.machine_readable,
            invocation=_invocation(argv),
        )
        emitter.emit("running", "Verifying the installed machine and requested operation.")
        try:
            owner = {
                "server install": driver.install,
                "server update": driver.update,
                "server restore": driver.restore,
                "server supervisor update": driver.supervisor_update,
                "server doctor": driver.doctor,
            }[command]
            code = owner(arguments, emitter)
        except (SupervisorError, OSError, ValueError, subprocess.SubprocessError) as exc:
            if emitter.state in {"pending", "running"}:
                # A failure is a usable breakpoint: the bounded cause, the retained
                # deployment state, and exact diagnostic/continue commands.
                emitter.emit("failed", str(exc), fields=driver.safe_state_fields())
            else:
                print(str(exc), file=sys.stderr)
            return 1
        if code == 3:
            from rcp_supervisor.events import run_operator_pause

            try:
                resumed = run_operator_pause(emitter)
            except (SupervisorError, OSError, ValueError, subprocess.SubprocessError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if resumed is not None:
                return resumed
        return code
    if arguments.plan:
        print("Plans are available for installed server commands.", file=sys.stderr)
        return 1
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
