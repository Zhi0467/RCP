"""Parsing, entry-identity checks, and rendering for ``rcp server``."""

from __future__ import annotations

import argparse
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, TextIO

from pydantic import ValidationError

from rcp.server_ops.config import (
    DEFAULT_BACKUP_RETENTION,
    DEFAULT_BACKUP_SCHEDULE,
    validate_age_recipient,
    validate_backup_destination,
    validate_backup_retention,
    validate_backup_schedule,
)
from rcp.server_ops.models import (
    SERVER_CLI_MAX_EXECUTION_BYTES,
    MachineTarget,
    NonsecretField,
    ServerCommandExecution,
    ServerCommandName,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStep,
    ServerStepEvent,
    absolute_path,
    canonical_uuid4,
    server_event_stream_size,
    validate_server_event_prefix,
)

SERVER_CLI_EXIT_FAILED = 1
SERVER_CLI_EXIT_OPERATOR_ACTION = 3
SERVER_CLI_EXIT_WRONG_IDENTITY = 77
SERVER_CLI_TERMINAL_RESERVE_BYTES = 64 * 1024
SERVER_CLI_INTERACTIVE_FIELD_LIMIT = 8
_FULL_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")

_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD = "1"
_ANSI_DIM = "2"
_ANSI_RED = "31"
_ANSI_GREEN = "32"
_ANSI_YELLOW = "33"
_ANSI_CYAN = "36"

_ROOT_COMMANDS: frozenset[ServerCommandName] = frozenset(
    {
        "server install",
        "server backup configure",
        "server restore",
        "server provider update",
        "server update",
    }
)
_SERVICE_COMMANDS: frozenset[ServerCommandName] = frozenset(
    {
        "server doctor",
        "server provider check",
        "server project provision",
        "server project transfer-import",
        "server backup run",
        "server member remove",
    }
)


@dataclass(frozen=True)
class CallerIdentity:
    uid: int
    username: str
    host: str


ServerCommandExecutor = Callable[["ServerEventEmitter", BinaryIO], None]
WizardCommandRunner = Callable[[tuple[str, ...]], int]


@dataclass(frozen=True)
class PreparedServerCommand:
    """A side-effect-free plan plus work that starts only after plan emission."""

    plan: ServerPlanEvent
    execute: ServerCommandExecutor
    failed_exit_code: int = SERVER_CLI_EXIT_FAILED

    def __post_init__(self) -> None:
        if not 1 <= self.failed_exit_code <= 125:
            raise ValueError("failed server command exit codes must be between 1 and 125")


ServerCommandHandler = Callable[
    [ServerCommandRequest, CallerIdentity],
    PreparedServerCommand,
]


def add_server_parser(subcommands: argparse._SubParsersAction) -> argparse.ArgumentParser:
    server = subcommands.add_parser(
        "server",
        help="Install, inspect, and maintain a source-built RCP team server",
    )
    server.add_argument(
        "--machine-readable",
        action="store_true",
        default=False,
        help="Stream the same bounded progress as one JSON object per line",
    )
    server_commands = server.add_subparsers(dest="server_group", required=True)

    install = _leaf(server_commands, "install", "Install or converge the source-built service")
    install.add_argument(
        "--team-name",
        required=True,
        type=_team_name,
        help="Human-readable name used by the exact team-space initialization command",
    )
    install.set_defaults(server_operation="server install")

    doctor = _leaf(server_commands, "doctor", "Inspect installed service and operation health")
    doctor.set_defaults(server_operation="server doctor")

    provider = server_commands.add_parser("provider", help="Inspect provider readiness")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_check = _leaf(
        provider_commands,
        "check",
        "Check the provider profile already bound to one request or project",
    )
    selector = provider_check.add_mutually_exclusive_group(required=True)
    selector.add_argument("--request", dest="request_id", type=_request_id)
    selector.add_argument("--project", dest="project_id", type=_project_id)
    provider_check.set_defaults(server_operation="server provider check")
    provider_update = _leaf(
        provider_commands,
        "update",
        "Update one server-local provider CLI under the rcp account",
    )
    provider_update.add_argument(
        "provider_update_provider",
        choices=("codex", "claude"),
        metavar="{codex,claude}",
    )
    provider_update.set_defaults(server_operation="server provider update")

    project = server_commands.add_parser("project", help="Prepare or import a team project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    provision = _leaf(
        project_commands,
        "provision",
        "Resume the machine preparation recorded by a provisioning request",
    )
    provision.add_argument("request_id", type=_request_id)
    provision.set_defaults(server_operation="server project provision")
    transfer_import = _leaf(
        project_commands,
        "transfer-import",
        "Stream one authorized transfer archive on stdin into its target request",
    )
    transfer_import.add_argument("request_id", type=_request_id)
    transfer_import.set_defaults(server_operation="server project transfer-import")

    backup = server_commands.add_parser("backup", help="Configure or run protected backups")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_configure = _leaf(
        backup_commands,
        "configure",
        "Explicitly configure destination, encryption, schedule, and retention",
    )
    backup_configure.add_argument(
        "--destination",
        required=True,
        type=_backup_destination,
        help="Explicit absolute local or mounted directory writable by the rcp account",
    )
    backup_configure.add_argument(
        "--recipient",
        dest="backup_age_recipient",
        required=True,
        type=_backup_age_recipient,
        help="Native X25519 age1 public recipient; never provide the private identity",
    )
    backup_configure.add_argument(
        "--schedule",
        dest="backup_schedule",
        default=DEFAULT_BACKUP_SCHEDULE,
        type=_backup_schedule,
        help="Daily server-local time in HH:MM form (default: 02:00)",
    )
    backup_configure.add_argument(
        "--retention",
        dest="backup_retention",
        default=DEFAULT_BACKUP_RETENTION,
        type=_backup_retention,
        help="Number of newest integrity-readback archives to retain (default: 30)",
    )
    backup_configure.add_argument(
        "--confirm",
        dest="backup_confirmed",
        action="store_true",
        required=True,
        help="Confirm the destination, recipient, schedule, and retention supplied here",
    )
    backup_configure.set_defaults(server_operation="server backup configure")
    backup_run = _leaf(backup_commands, "run", "Capture and verify one configured backup")
    backup_run.set_defaults(server_operation="server backup run")

    restore = _leaf(server_commands, "restore", "Restore one verified archive to a fresh server")
    restore.add_argument("archive_path", type=_archive_path)
    restore.add_argument(
        "--identity-file",
        dest="recovery_identity_file",
        type=_identity_file,
        required=True,
        help="Absolute path to a protected off-server age identity file",
    )
    restore.add_argument(
        "--confirm-data-dir",
        dest="restore_confirmed_data_dir",
        type=lambda value: _absolute_path(value, "confirmed restore data directory"),
        help="Confirm the exact installed RCP_DATA_DIR displayed by the first restore call",
    )
    restore.add_argument(
        "--old-authority-disposition",
        dest="restore_old_authority_disposition",
        choices=("old-machine-destroyed", "old-machine-fenced-and-credentials-revoked"),
        help="Record how the archived server authority was permanently excluded",
    )
    restore.add_argument(
        "--confirm-old-authority",
        dest="restore_confirmed_old_authority",
        type=_member_boundary,
        help="Confirm the exact SHA-256 archived-authority inventory displayed by RCP",
    )
    restore.add_argument(
        "--confirm-member-roster",
        dest="restore_confirmed_member_roster",
        type=_member_boundary,
        help="Confirm the exact active member and permanent-token roster displayed by RCP",
    )
    restore.add_argument(
        "--remove-stale-member",
        dest="restore_stale_member_id",
        type=_member_id,
        help="Remove one known-stale member offline, then display the changed roster again",
    )
    restore.set_defaults(server_operation="server restore")

    member = server_commands.add_parser("member", help="Remove a member under a durable fence")
    member_commands = member.add_subparsers(dest="member_command", required=True)
    member_remove = _leaf(
        member_commands,
        "remove",
        "Preview and resume removal of one canonical team member",
    )
    member_remove.add_argument("member_id", type=_member_id)
    member_remove.add_argument(
        "--confirm-boundary",
        dest="member_confirmed_boundary",
        type=_member_boundary,
        help="Confirm the exact SHA-256 consequence boundary displayed by the preview",
    )
    member_remove.set_defaults(server_operation="server member remove")

    update = _leaf(server_commands, "update", "Prepare a source-built origin/main candidate")
    update.add_argument(
        "--confirm-target",
        dest="update_confirmed_commit",
        type=_git_commit,
        help="Confirm exactly the fetched 40-character origin/main commit shown by RCP",
    )
    update.set_defaults(server_operation="server update")
    return server


def _leaf(
    subcommands: argparse._SubParsersAction,
    name: str,
    help_text: str,
) -> argparse.ArgumentParser:
    parser = subcommands.add_parser(name, help=help_text, description=help_text)
    parser.add_argument(
        "--machine-readable",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Stream the same bounded progress as one JSON object per line",
    )
    return parser


def _canonical_identifier(value: str, label: str) -> str:
    try:
        return canonical_uuid4(value, label=label)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _request_id(value: str) -> str:
    return _canonical_identifier(value, "request id")


def _project_id(value: str) -> str:
    return _canonical_identifier(value, "project id")


def _member_id(value: str) -> str:
    return _canonical_identifier(value, "member id")


def _member_boundary(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError(
            "confirmed member boundary must be a lowercase 64-character SHA-256 digest"
        )
    return value


def _team_name(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 120
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise argparse.ArgumentTypeError(
            "team name must be one nonempty line of at most 120 characters"
        )
    return normalized


def _git_commit(value: str) -> str:
    if _FULL_GIT_COMMIT.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "confirmed update target must be a full lowercase 40-character Git object id"
        )
    return value


def _absolute_path(value: str, label: str) -> str:
    try:
        path = Path(value)
        if ".." in path.parts:
            raise ValueError(f"{label} must be absolute and normalized")
        return absolute_path(str(path), label=label)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _archive_path(value: str) -> str:
    return _absolute_path(value, "archive path")


def _identity_file(value: str) -> str:
    return _absolute_path(value, "recovery identity file")


def _validated_argument(value: object, validator) -> object:
    try:
        return validator(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _backup_destination(value: str) -> str:
    return str(_validated_argument(value, validate_backup_destination))


def _backup_age_recipient(value: str) -> str:
    return str(_validated_argument(value, validate_age_recipient))


def _backup_schedule(value: str) -> str:
    return str(_validated_argument(value, validate_backup_schedule))


def _backup_retention(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "backup retention must be a positive archive count"
        ) from exc
    return int(_validated_argument(parsed, validate_backup_retention))


def request_from_namespace(args: argparse.Namespace) -> ServerCommandRequest:
    try:
        return ServerCommandRequest(
            command=args.server_operation,
            team_name=getattr(args, "team_name", None),
            request_id=getattr(args, "request_id", None),
            project_id=getattr(args, "project_id", None),
            provider_update_provider=getattr(args, "provider_update_provider", None),
            member_id=getattr(args, "member_id", None),
            member_confirmed_boundary=getattr(args, "member_confirmed_boundary", None),
            archive_path=getattr(args, "archive_path", None),
            recovery_identity_file=getattr(args, "recovery_identity_file", None),
            restore_confirmed_data_dir=getattr(args, "restore_confirmed_data_dir", None),
            restore_old_authority_disposition=getattr(
                args, "restore_old_authority_disposition", None
            ),
            restore_confirmed_old_authority=getattr(args, "restore_confirmed_old_authority", None),
            restore_confirmed_member_roster=getattr(args, "restore_confirmed_member_roster", None),
            restore_stale_member_id=getattr(args, "restore_stale_member_id", None),
            backup_destination=getattr(args, "destination", None),
            backup_schedule=getattr(args, "backup_schedule", None),
            backup_retention=getattr(args, "backup_retention", None),
            backup_age_recipient=getattr(args, "backup_age_recipient", None),
            backup_confirmed=getattr(args, "backup_confirmed", None),
            update_confirmed_commit=getattr(args, "update_confirmed_commit", None),
        )
    except (AttributeError, ValidationError) as exc:  # pragma: no cover - parser owns public input
        raise RuntimeError("argparse produced an invalid server command") from exc


def current_caller_identity() -> CallerIdentity:
    uid = os.geteuid()
    return CallerIdentity(uid=uid, username=pwd.getpwuid(uid).pw_name, host=socket.gethostname())


def required_os_account(command: ServerCommandName) -> str:
    if command in _ROOT_COMMANDS:
        return "root"
    if command in _SERVICE_COMMANDS:
        return "rcp"
    raise AssertionError(f"Unhandled server command {command!r}")


def run_server_command(
    args: argparse.Namespace,
    *,
    handler: ServerCommandHandler | None = None,
    identity: CallerIdentity | None = None,
    input_stream: BinaryIO | None = None,
    stream: TextIO | None = None,
    wizard_input: TextIO | None = None,
    wizard_runner: WizardCommandRunner | None = None,
) -> int:
    """Validate identity, emit the plan, then begin concrete machine work."""

    request = request_from_namespace(args)
    resolved_identity = identity or current_caller_identity()
    resolved_input = input_stream
    if resolved_input is None:
        resolved_input = getattr(sys.stdin, "buffer", sys.stdin)
    output = stream if stream is not None else sys.stdout
    required_account = required_os_account(request.command)
    if not _identity_matches(resolved_identity, required_account):
        prepared = _wrong_identity_command(request, resolved_identity, required_account)
    else:
        try:
            prepared = (handler or _dispatch_server_command)(request, resolved_identity)
        except Exception:
            prepared = _preparation_failed_command(request, resolved_identity)
    try:
        plan = ServerPlanEvent.model_validate(prepared.plan)
        if plan.command != request.command:
            raise ValueError("server command handler prepared a different command")
        emitter = ServerEventEmitter(
            plan,
            machine_readable=bool(getattr(args, "machine_readable", False)),
            stream=output,
        )
    except Exception:
        prepared = _preparation_failed_command(request, resolved_identity)
        emitter = ServerEventEmitter(
            prepared.plan,
            machine_readable=bool(getattr(args, "machine_readable", False)),
            stream=output,
        )
    try:
        prepared.execute(emitter, resolved_input)
    except Exception:
        emitter.fail_unexpected()
    execution = emitter.finish(failed_exit_code=prepared.failed_exit_code)
    prompt_input = wizard_input if wizard_input is not None else sys.stdin
    if (
        execution.exit_code == SERVER_CLI_EXIT_OPERATOR_ACTION
        and not bool(getattr(args, "machine_readable", False))
        and _supports_live_updates(output)
        and _supports_terminal_input(prompt_input)
    ):
        return _continue_interactive_wizard(
            execution,
            identity=resolved_identity,
            input_stream=prompt_input,
            output_stream=output,
            runner=wizard_runner or _run_wizard_command,
        )
    return execution.exit_code


def _continue_interactive_wizard(
    execution: ServerCommandExecution,
    *,
    identity: CallerIdentity,
    input_stream: TextIO,
    output_stream: TextIO,
    runner: WizardCommandRunner,
) -> int:
    final = execution.events[-1]
    if not isinstance(final, ServerStepEvent):  # pragma: no cover - execution owns this
        return execution.exit_code
    step = final.step
    output_stream.write("\nComplete the step above, then press Enter to continue (q quits): ")
    output_stream.flush()
    answer = input_stream.readline()
    if not answer or answer.strip().lower() == "q":
        print(
            "Setup paused safely. Run the shown continue command when you are ready.",
            file=output_stream,
        )
        return execution.exit_code
    resume = step.resume_argv
    commands = [action.argv for action in step.actions if action.kind == "command"]
    for command in commands:
        if command != resume:
            runner(_wizard_command_for_identity(command, identity))
    if step.phase == "team_space_init" and commands:
        output_stream.write("Save the one-time enrollment code, then press Enter to continue: ")
        output_stream.flush()
        answer = input_stream.readline()
        if not answer or answer.strip().lower() == "q":
            print(
                "Setup paused safely. Run the shown continue command after saving the code.",
                file=output_stream,
            )
            return execution.exit_code
    if not resume:
        return execution.exit_code
    return runner(_wizard_command_for_identity(resume, identity))


def _wizard_command_for_identity(
    argv: tuple[str, ...],
    identity: CallerIdentity,
) -> tuple[str, ...]:
    own_account_prefix = ("sudo", "-n", "-u", identity.username, "-H")
    if identity.uid != 0 and argv[: len(own_account_prefix)] == own_account_prefix:
        return argv[len(own_account_prefix) :]
    return argv


def _run_wizard_command(argv: tuple[str, ...]) -> int:
    return subprocess.run(argv, check=False).returncode


def _supports_terminal_input(stream: TextIO) -> bool:
    is_terminal = getattr(stream, "isatty", lambda: False)
    return bool(is_terminal())


def _identity_matches(identity: CallerIdentity, required_account: str) -> bool:
    if required_account == "root":
        return identity.uid == 0 and identity.username == "root"
    return identity.uid != 0 and identity.username == required_account


def _dispatch_server_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
) -> PreparedServerCommand:
    """Explicit seam replaced one concrete owner at a time by later packets."""

    match request.command:
        case "server install":
            from rcp.server_ops.install import prepare_install_command

            return prepare_install_command(request, identity)
        case "server doctor":
            from rcp.server_ops.doctor import prepare_doctor_command

            return prepare_doctor_command(request, identity)
        case "server provider check":
            from rcp.server_ops.provider_readiness import prepare_provider_check_command

            return prepare_provider_check_command(request, identity)
        case "server provider update":
            from rcp.server_ops.provider_update import prepare_provider_update_command

            return prepare_provider_update_command(request, identity)
        case "server project provision":
            from rcp.server_ops.project_provision import prepare_project_provision_command

            return prepare_project_provision_command(request, identity)
        case "server project transfer-import":
            from rcp.transfer.target import prepare_transfer_import_command

            return prepare_transfer_import_command(request, identity)
        case "server backup configure":
            from rcp.server_ops.backup_config import prepare_backup_configure_command

            return prepare_backup_configure_command(request, identity)
        case "server backup run":
            from rcp.server_ops.backup import prepare_backup_run_command

            return prepare_backup_run_command(request, identity)
        case "server restore":
            from rcp.server_ops.restore import prepare_restore_command

            return prepare_restore_command(request, identity)
        case "server member remove":
            from rcp.server_ops.members import prepare_member_remove_command

            return prepare_member_remove_command(request, identity)
        case "server update":
            from rcp.server_ops.update import prepare_update_command

            return prepare_update_command(request, identity)
    raise AssertionError(f"Unhandled server command {request.command!r}")


def _wrong_identity_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    required_account: str,
) -> PreparedServerCommand:
    target = MachineTarget(host=identity.host, os_account=required_account)
    pending = ServerStep(
        number=1,
        title="Validate machine entry identity",
        purpose="Refuse the operation before durable or external work under the wrong OS account.",
        performed_by="system",
        target=target,
        phase="entry_identity",
        state="pending",
        expected_success=f"The command is entered as operating-system account {required_account}.",
        message=f"RCP will validate the caller before running {request.command}.",
    )
    failed = pending.model_copy(
        update={
            "state": "failed",
            "message": (
                f"{request.command} requires operating-system account {required_account}; "
                f"the current caller is {identity.username} (uid {identity.uid})."
            ),
        }
    )
    return _single_step_command(
        request.command,
        pending,
        failed,
        failed_exit_code=SERVER_CLI_EXIT_WRONG_IDENTITY,
    )


def _preparation_failed_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
) -> PreparedServerCommand:
    target = MachineTarget(host=identity.host, os_account=identity.username)
    pending = ServerStep(
        number=1,
        title="Prepare the server operation",
        purpose="Validate the complete operation plan before any server work begins.",
        performed_by="system",
        target=target,
        phase="operation_prepare",
        state="pending",
        expected_success=f"{request.command} publishes a valid plan before starting work.",
        message=f"RCP will prepare the plan for {request.command}.",
    )
    failed = pending.model_copy(
        update={
            "state": "failed",
            "message": (
                "RCP could not prepare a valid operation plan. No server work was started; "
                "check the server log and rerun this command."
            ),
        }
    )
    return _single_step_command(request.command, pending, failed)


def _single_step_command(
    command: ServerCommandName,
    pending: ServerStep,
    final: ServerStep,
    *,
    failed_exit_code: int = SERVER_CLI_EXIT_FAILED,
) -> PreparedServerCommand:
    def execute(emitter: ServerEventEmitter, _input_stream: BinaryIO) -> None:
        emitter.emit_step(final)

    return PreparedServerCommand(
        plan=ServerPlanEvent(command=command, timestamp=datetime.now(UTC), steps=(pending,)),
        execute=execute,
        failed_exit_code=failed_exit_code,
    )


class ServerEventEmitter:
    """Validate, bound, render, and flush each event as the operation runs."""

    def __init__(
        self,
        plan: ServerPlanEvent,
        *,
        machine_readable: bool,
        stream: TextIO,
    ) -> None:
        validated = ServerPlanEvent.model_validate(plan)
        validate_server_event_prefix((validated,))
        _require_terminal_headroom((validated,))
        self._plan = validated
        self._events: list[ServerPlanEvent | ServerStepEvent] = [validated]
        self._machine_readable = machine_readable
        self._stream = stream
        self._interactive_renderer = (
            None
            if machine_readable
            else _InteractiveServerRenderer(
                plan_size=len(validated.steps),
                stream=stream,
            )
        )
        self._render(validated)

    @property
    def events(self) -> tuple[ServerPlanEvent | ServerStepEvent, ...]:
        return tuple(self._events)

    def emit_step(
        self,
        step: ServerStep,
        *,
        timestamp: datetime | None = None,
        announce_success: bool = False,
    ) -> None:
        event = ServerStepEvent(
            command=self._plan.command,
            timestamp=timestamp or datetime.now(UTC),
            step=step,
        )
        candidate = (*self._events, event)
        validate_server_event_prefix(candidate)
        if not self._is_terminal_event(event):
            _require_terminal_headroom(candidate)
        self._events.append(event)
        self._render(event, announce_success=announce_success)

    def fail_unexpected(self) -> None:
        """End an incomplete stream without exposing exception or subprocess text."""

        final = self._events[-1]
        if (
            isinstance(final, ServerStepEvent)
            and final.step.state
            in {
                "succeeded",
                "failed",
                "operator_action_needed",
            }
            and (final.step.number == len(self._plan.steps) or final.step.state != "succeeded")
        ):
            return
        latest_by_number = {
            event.step.number: event.step
            for event in self._events[1:]
            if isinstance(event, ServerStepEvent)
        }
        number = next(
            (
                planned.number
                for planned in self._plan.steps
                if latest_by_number.get(planned.number, planned).state != "succeeded"
            ),
            len(self._plan.steps),
        )
        current = latest_by_number.get(number, self._plan.steps[number - 1])
        failed = current.model_copy(
            update={
                "state": "failed",
                "message": (
                    "The server operation stopped unexpectedly. Check the server log and "
                    "rerun the same command. No exception or credential text is shown here."
                ),
                "actions": (),
                "fields": (),
                "resume_argv": (),
            }
        )
        self.emit_step(failed)

    def finish(self, *, failed_exit_code: int) -> ServerCommandExecution:
        final = self._events[-1]
        all_succeeded = all(
            any(
                isinstance(event, ServerStepEvent)
                and event.step.number == planned.number
                and event.step.state == "succeeded"
                for event in self._events
            )
            for planned in self._plan.steps
        )
        if not all_succeeded and (
            not isinstance(final, ServerStepEvent)
            or final.step.state not in {"failed", "operator_action_needed"}
        ):
            self.fail_unexpected()
            final = self._events[-1]
        if all_succeeded:
            exit_code = 0
        elif isinstance(final, ServerStepEvent) and final.step.state == "operator_action_needed":
            exit_code = SERVER_CLI_EXIT_OPERATOR_ACTION
        else:
            exit_code = failed_exit_code
        return ServerCommandExecution(events=tuple(self._events), exit_code=exit_code)

    def _render(
        self,
        event: ServerPlanEvent | ServerStepEvent,
        *,
        announce_success: bool = False,
    ) -> None:
        if self._machine_readable:
            print(event.model_dump_json(), file=self._stream)
        else:
            assert self._interactive_renderer is not None
            self._interactive_renderer.render(event, announce_success=announce_success)
        self._stream.flush()

    def _is_terminal_event(self, event: ServerStepEvent) -> bool:
        return event.step.state in {"failed", "operator_action_needed"} or (
            event.step.state == "succeeded" and event.step.number == len(self._plan.steps)
        )


def _require_terminal_headroom(
    events: tuple[ServerPlanEvent | ServerStepEvent, ...],
) -> None:
    live_limit = SERVER_CLI_MAX_EXECUTION_BYTES - SERVER_CLI_TERMINAL_RESERVE_BYTES
    if server_event_stream_size(events) > live_limit:
        raise ValueError("live server CLI progress exhausted its bounded terminal-event reserve")


def render_server_execution(
    execution: ServerCommandExecution,
    *,
    machine_readable: bool,
    stream: TextIO,
) -> None:
    validated = ServerCommandExecution.model_validate(execution)
    plan = validated.events[0]
    if not isinstance(plan, ServerPlanEvent):  # pragma: no cover - model owns this invariant
        raise ValueError("the first server CLI event must be the complete plan")
    interactive_renderer = (
        None
        if machine_readable
        else _InteractiveServerRenderer(plan_size=len(plan.steps), stream=stream)
    )
    for event in validated.events:
        if machine_readable:
            print(event.model_dump_json(), file=stream)
        else:
            assert interactive_renderer is not None
            interactive_renderer.render(event)
        stream.flush()


class _InteractiveServerRenderer:
    """Keep normal terminal output bounded to one live step plus terminal guidance."""

    def __init__(self, *, plan_size: int, stream: TextIO) -> None:
        self.plan_size = plan_size
        self.stream = stream
        self.color = _supports_color(stream)
        self.live_updates = _supports_live_updates(stream)

    def render(
        self,
        event: ServerPlanEvent | ServerStepEvent,
        *,
        announce_success: bool = False,
    ) -> None:
        if isinstance(event, ServerPlanEvent):
            heading = _style(
                f"RCP  {event.command}",
                _ANSI_BOLD,
                _ANSI_CYAN,
                color=self.color,
            )
            print(heading, file=self.stream)
            print(file=self.stream)
            return
        step = event.step
        label, ansi = _step_status(step.state)
        status = _style(label, _ANSI_BOLD, ansi, color=self.color)
        headline = f"{status}  {step.number}/{self.plan_size}  {step.title}"
        if step.state == "running":
            self._current_line(headline, finish=False)
            return
        terminal = step.state in {"failed", "operator_action_needed"}
        final_success = step.state == "succeeded" and step.number == self.plan_size
        self._current_line(headline, finish=terminal or final_success or announce_success)
        if not terminal and not final_success and not announce_success:
            return
        _print_wrapped(step.message, self.stream, indent="  ")
        if terminal:
            self._render_stop(step)
        self._render_fields(step.fields)
        if terminal:
            self._render_actions(step)

    def _current_line(self, text: str, *, finish: bool) -> None:
        if self.live_updates:
            self.stream.write(f"\r\x1b[2K{text}")
            if finish:
                self.stream.write("\n")
            return
        print(text, file=self.stream)

    def _render_stop(self, step: ServerStep) -> None:
        if isinstance(step.target, MachineTarget):
            print(f"  On: {step.target.host} (as {step.target.os_account})", file=self.stream)
        else:
            print(f"  Needs: {step.target.required_authority_role}", file=self.stream)
            print(f"  Open: {step.target.destination_url}", file=self.stream)
        _print_wrapped(
            step.expected_success,
            self.stream,
            indent="  Continue when: ",
            subsequent_indent="                 ",
        )

    def _render_fields(self, fields: tuple[NonsecretField, ...]) -> None:
        if not fields:
            return
        print(file=self.stream)
        shown = fields[:SERVER_CLI_INTERACTIVE_FIELD_LIMIT]
        for field in shown:
            print(f"  {field.name.replace('_', ' ')}: {field.value}", file=self.stream)
        hidden = len(fields) - len(shown)
        if hidden:
            print(
                f"  … {hidden} more field(s); use --machine-readable for the complete record.",
                file=self.stream,
            )

    def _render_actions(self, step: ServerStep) -> None:
        if step.actions:
            print(file=self.stream)
            print(_style("Next", _ANSI_BOLD, _ANSI_YELLOW, color=self.color), file=self.stream)
            for index, action in enumerate(step.actions, start=1):
                if action.kind == "command":
                    if action.argv == step.resume_argv:
                        continue
                    print(f"  {index}. $ {shlex.join(action.argv)}", file=self.stream)
                else:
                    _print_wrapped(
                        action.instruction,
                        self.stream,
                        indent=f"  {index}. ",
                        subsequent_indent="     ",
                    )
        if step.resume_argv:
            print(file=self.stream)
            print("Continue:", file=self.stream)
            print(f"  $ {shlex.join(step.resume_argv)}", file=self.stream)


def _supports_live_updates(stream: TextIO) -> bool:
    is_terminal = getattr(stream, "isatty", lambda: False)
    return bool(is_terminal()) and os.environ.get("TERM") != "dumb"


def _supports_color(stream: TextIO) -> bool:
    is_terminal = getattr(stream, "isatty", lambda: False)
    return bool(is_terminal()) and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def _style(text: str, *codes: str, color: bool) -> str:
    if not color or not codes:
        return text
    return f"\x1b[{';'.join(codes)}m{text}{_ANSI_RESET}"


def _step_status(state: str) -> tuple[str, str]:
    return {
        "pending": ("PENDING", _ANSI_DIM),
        "running": ("RUNNING", _ANSI_CYAN),
        "succeeded": ("DONE", _ANSI_GREEN),
        "failed": ("FAILED", _ANSI_RED),
        "operator_action_needed": ("ACTION REQUIRED", _ANSI_YELLOW),
    }[state]


def _print_wrapped(
    text: str,
    stream: TextIO,
    *,
    indent: str,
    subsequent_indent: str | None = None,
) -> None:
    terminal_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    width = max(60, min(terminal_width, 120))
    print(
        textwrap.fill(
            text,
            width=width,
            initial_indent=indent,
            subsequent_indent=subsequent_indent or indent,
            break_long_words=False,
            break_on_hyphens=False,
        ),
        file=stream,
    )


__all__ = [
    "CallerIdentity",
    "PreparedServerCommand",
    "SERVER_CLI_EXIT_FAILED",
    "SERVER_CLI_EXIT_OPERATOR_ACTION",
    "SERVER_CLI_EXIT_WRONG_IDENTITY",
    "SERVER_CLI_TERMINAL_RESERVE_BYTES",
    "ServerCommandHandler",
    "ServerEventEmitter",
    "add_server_parser",
    "current_caller_identity",
    "render_server_execution",
    "request_from_namespace",
    "required_os_account",
    "run_server_command",
]
