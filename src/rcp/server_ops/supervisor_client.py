"""Bounded subprocess delegation to the independently installed supervisor."""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Generator

from rcp.limits import (
    SERVER_INSTALL_PROBE_TIMEOUT_SECONDS,
    SERVER_SUPERVISOR_CHILD_STOP_TIMEOUT_SECONDS,
    SERVER_SUPERVISOR_CHILD_WAIT_MIN_SECONDS,
    SERVER_SUPERVISOR_COMMAND_TIMEOUT_SECONDS,
    SERVER_SUPERVISOR_PIPE_CHUNK_BYTES,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.models import (
    SERVER_CLI_MAX_EXECUTION_BYTES,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStepEvent,
)


class SupervisorUnavailable(RuntimeError):
    """The independent root-owned coordinator is unavailable or refused its contract."""


def supervisor_argv(
    request: ServerCommandRequest, layout: ServerLayout = DEFAULT_SERVER_LAYOUT
) -> list[str]:
    wrapper = layout.supervisor_wrapper
    for parent in reversed(wrapper.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise SupervisorUnavailable(
                "The supervisor launcher ancestry is not root-owned and protected."
            )
    info = wrapper.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_mode & 0o022
        or not info.st_mode & 0o111
    ):
        raise SupervisorUnavailable("The independent supervisor launcher is not installed safely.")
    argv = [str(wrapper), "--machine-readable", *request.command.split()]
    for field, flag in (
        ("team_name", "--team-name"),
        ("update_confirmed_target", "--confirm-target"),
        ("recovery_identity_file", "--identity-file"),
        ("restore_confirmed_data_dir", "--confirm-data-dir"),
        ("restore_old_authority_disposition", "--old-authority-disposition"),
        ("restore_confirmed_old_authority", "--confirm-old-authority"),
        ("restore_confirmed_member_roster", "--confirm-member-roster"),
        ("restore_stale_member_id", "--remove-stale-member"),
    ):
        value = getattr(request, field, None)
        if value is not None:
            argv.extend((flag, value))
    if request.archive_path is not None:
        argv.append(request.archive_path)
    return argv


def _lines(argv: list[str], *, timeout: float) -> Generator[bytes, None, int]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "UV_", "PIP_"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
        start_new_session=True,
    ) as process:
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        buffer = b""
        total = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SupervisorUnavailable(
                            "The supervisor command exceeded its time limit; recover the durable operation before retrying."
                        )
                    if not selector.select(remaining):
                        continue
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(
                            SERVER_SUPERVISOR_PIPE_CHUNK_BYTES,
                            SERVER_CLI_MAX_EXECUTION_BYTES - total + 1,
                        ),
                    )
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > SERVER_CLI_MAX_EXECUTION_BYTES:
                        raise SupervisorUnavailable(
                            "The supervisor event stream exceeded its size limit."
                        )
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line:
                            yield line
                if buffer:
                    raise SupervisorUnavailable("The supervisor returned an incomplete event line.")
                code = process.wait(
                    timeout=max(
                        SERVER_SUPERVISOR_CHILD_WAIT_MIN_SECONDS, deadline - time.monotonic()
                    )
                )
                if code not in (0, 1, 3, 77):
                    raise SupervisorUnavailable(
                        "The supervisor terminated unexpectedly; recover its durable operation before retrying."
                    )
                return code
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=SERVER_SUPERVISOR_CHILD_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def prepare_supervisor_command(request, identity, *, layout: ServerLayout = DEFAULT_SERVER_LAYOUT):
    from rcp.server_ops.cli import PreparedServerCommand

    argv = supervisor_argv(request, layout)
    lines = list(
        _lines([argv[0], "--plan", *argv[1:]], timeout=SERVER_INSTALL_PROBE_TIMEOUT_SECONDS)
    )
    if len(lines) != 1:
        raise SupervisorUnavailable("The supervisor did not return one complete operation plan.")
    plan = ServerPlanEvent.model_validate_json(lines[0])
    if plan.command != request.command:
        raise SupervisorUnavailable("The supervisor returned another command's plan.")

    def execute(emitter, _input_stream):
        execute_supervisor_command(request, emitter, layout=layout)

    return PreparedServerCommand(plan=plan, execute=execute)


def execute_supervisor_command(
    request, emitter, *, layout: ServerLayout = DEFAULT_SERVER_LAYOUT, already_running: bool = False
) -> None:
    lines = iter(
        _lines(supervisor_argv(request, layout), timeout=SERVER_SUPERVISOR_COMMAND_TIMEOUT_SECONDS)
    )
    try:
        actual_plan = ServerPlanEvent.model_validate_json(next(lines))
    except StopIteration as exc:
        raise SupervisorUnavailable("The supervisor returned no operation plan.") from exc
    expected_plan = emitter.events[0]
    if actual_plan.command != expected_plan.command or actual_plan.steps != expected_plan.steps:
        raise SupervisorUnavailable("The supervisor changed its reviewed operation plan.")
    terminal = None
    while True:
        try:
            line = next(lines)
        except StopIteration as stopped:
            code = stopped.value
            break
        if terminal is not None:
            raise SupervisorUnavailable("The supervisor returned output after its terminal event.")
        event = ServerStepEvent.model_validate_json(line)
        if event.command != request.command:
            raise SupervisorUnavailable("The supervisor returned another command's event.")
        if already_running and event.step.state == "running":
            continue
        if event.step.state in {"failed", "operator_action_needed"} or (
            event.step.state == "succeeded" and event.step.number == len(actual_plan.steps)
        ):
            terminal = event
        else:
            emitter.emit_step(event.step, timestamp=event.timestamp)
    if terminal is None:
        raise SupervisorUnavailable("The supervisor returned no terminal event.")
    expected_codes = {
        "succeeded": (0,),
        "failed": (1, 77),
        "operator_action_needed": (3,),
    }
    if code not in expected_codes[terminal.step.state]:
        raise SupervisorUnavailable("The supervisor exit code disagreed with its terminal event.")
    emitter.emit_step(terminal.step, timestamp=terminal.timestamp)
