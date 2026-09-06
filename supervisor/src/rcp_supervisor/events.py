"""The existing sealed server wizard envelope, encoded without importing RCP."""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from typing import TextIO

from rcp_supervisor.limits import INTERACTIVE_FIELD_LIMIT

_OPERATIONS = {
    "server install": ("Install server", "team_space_init"),
    "server update": ("Update server", "supervisor_update"),
    "server restore": ("Restore server", "supervisor_restore"),
    "server supervisor update": ("Update supervisor", "supervisor_self_update"),
    "server doctor": ("Inspect server", "supervisor_doctor"),
}
# Every installed server command re-enters through the root-owned public wrapper,
# which delegates to the supervisor. Continue commands therefore never name the
# supervisor entry point directly.
PUBLIC_WRAPPER = "/usr/local/bin/rcp"
_TERMINAL_STATES = {"succeeded", "failed", "operator_action_needed"}

_ANSI_RESET = "0"
_ANSI_BOLD = "1"
_ANSI_DIM = "2"
_ANSI_RED = "31"
_ANSI_GREEN = "32"
_ANSI_YELLOW = "33"
_ANSI_CYAN = "36"
_STATUS = {
    "pending": ("PENDING", _ANSI_DIM),
    "running": ("RUNNING", _ANSI_CYAN),
    "succeeded": ("DONE", _ANSI_GREEN),
    "failed": ("FAILED", _ANSI_RED),
    "operator_action_needed": ("ACTION REQUIRED", _ANSI_YELLOW),
}


def os_account() -> str:
    """Name the effective account; an unmapped UID still yields a valid target."""
    uid = os.geteuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def plan(command: str) -> dict:
    title, phase = _OPERATIONS[command]
    return {
        "version": 1,
        "event": "plan",
        "command": command,
        "timestamp": datetime.now(UTC).isoformat(),
        "steps": [
            {
                "number": 1,
                "title": title,
                "purpose": "Perform the requested machine operation through the independent supervisor.",
                "performed_by": "system",
                "target": {
                    "kind": "machine",
                    "host": socket.gethostname(),
                    "os_account": os_account(),
                },
                "phase": phase,
                "state": "pending",
                "expected_success": "The requested operation is verified or paused at an explicit operator boundary.",
                "message": "The supervisor will verify the installed state before changing it.",
                "actions": [],
                "fields": [],
                "resume_argv": [],
            }
        ],
    }


def failure_recovery(invocation: list[str]) -> tuple[list[dict], list[str]]:
    """Exact commands for a failed convergent operation: rerun, diagnose, inspect.

    Every installed server command verifies before it changes anything, so the
    same invocation is its own continue command, and the machine-readable rerun
    is its complete diagnostic record. The operator never rebuilds these from prose.
    """
    resume = ["sudo", PUBLIC_WRAPPER, *invocation]
    actions = [{"kind": "command", "argv": [*resume, "--machine-readable"]}]
    inspect = ["sudo", "-u", "rcp", "-H", PUBLIC_WRAPPER, "server", "doctor"]
    if inspect != resume:
        actions.append({"kind": "command", "argv": inspect})
    return actions, resume


class EventEmitter:
    def __init__(
        self,
        command: str,
        *,
        machine_readable: bool = False,
        stream: TextIO | None = None,
        invocation: list[str] | None = None,
    ) -> None:
        self.command = command
        self.machine_readable = machine_readable
        self.stream = stream or sys.stdout
        self.invocation = list(invocation) if invocation is not None else None
        self.plan = plan(command)
        self.state = "pending"
        self.last_step: dict | None = None
        self._renderer = None if machine_readable else _TerminalRenderer(self.stream)
        if machine_readable:
            self._write(self.plan)
        else:
            self._renderer.heading(command)

    def _write(self, event: dict) -> None:
        print(json.dumps(event, allow_nan=False), file=self.stream, flush=True)

    def emit(
        self,
        state: str,
        message: str,
        *,
        actions: list[dict] | None = None,
        fields: list[dict] | None = None,
        resume_argv: list[str] | None = None,
    ) -> None:
        if state not in {"running", "succeeded", "failed", "operator_action_needed"}:
            raise ValueError("unsupported supervisor event state")
        if (
            self.state in _TERMINAL_STATES
            or (state == "running" and self.state != "pending")
            or (state == "succeeded" and self.state != "running")
        ):
            raise ValueError("supervisor step events must follow the sealed wizard lifecycle")
        if state == "failed" and not actions and not resume_argv and self.invocation is not None:
            actions, resume_argv = failure_recovery(self.invocation)
        step = dict(
            self.plan["steps"][0],
            state=state,
            message=message,
            actions=actions or [],
            fields=fields or [],
            resume_argv=resume_argv or [],
        )
        if state == "operator_action_needed":
            step["performed_by"] = "human"
        self.state = state
        self.last_step = step
        if self.machine_readable:
            self._write(
                {
                    "version": 1,
                    "event": "step",
                    "command": self.command,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "step": step,
                }
            )
        else:
            self._renderer.step(step)


class _TerminalRenderer:
    """One live current-step line plus bounded terminal guidance, as the app renders."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        terminal = bool(getattr(stream, "isatty", lambda: False)())
        dumb = os.environ.get("TERM") == "dumb"
        self.live = terminal and not dumb
        self.color = terminal and not dumb and "NO_COLOR" not in os.environ

    def heading(self, command: str) -> None:
        print(self._style(f"RCP  {command}", _ANSI_BOLD, _ANSI_CYAN), file=self.stream)
        print(file=self.stream, flush=True)

    def step(self, step: dict) -> None:
        label, ansi = _STATUS[step["state"]]
        headline = f"{self._style(label, _ANSI_BOLD, ansi)}  1/1  {step['title']}"
        if step["state"] == "running":
            self._current_line(headline, finish=False)
            return
        self._current_line(headline, finish=True)
        self._wrapped(step["message"], indent="  ")
        stopped = step["state"] in {"failed", "operator_action_needed"}
        if stopped:
            target = step["target"]
            print(f"  On: {target['host']} (as {target['os_account']})", file=self.stream)
            self._wrapped(
                step["expected_success"],
                indent="  Continue when: ",
                subsequent_indent="                 ",
            )
        self._fields(step["fields"])
        if stopped:
            self._actions(step)
        self.stream.flush()

    def _current_line(self, text: str, *, finish: bool) -> None:
        if self.live:
            self.stream.write(f"\r\x1b[2K{text}")
            if finish:
                self.stream.write("\n")
            self.stream.flush()
            return
        print(text, file=self.stream, flush=True)

    def _fields(self, fields: list[dict]) -> None:
        if not fields:
            return
        print(file=self.stream)
        shown = fields[:INTERACTIVE_FIELD_LIMIT]
        for field in shown:
            print(f"  {field['name'].replace('_', ' ')}: {field['value']}", file=self.stream)
        hidden = len(fields) - len(shown)
        if hidden:
            print(
                f"  … {hidden} more field(s); use --machine-readable for the complete record.",
                file=self.stream,
            )

    def _actions(self, step: dict) -> None:
        resume = step["resume_argv"]
        if step["actions"]:
            print(file=self.stream)
            print(self._style("Next", _ANSI_BOLD, _ANSI_YELLOW), file=self.stream)
            for index, action in enumerate(step["actions"], start=1):
                if action.get("kind") == "command":
                    if action["argv"] == resume:
                        continue
                    print(f"  {index}. $ {shlex.join(action['argv'])}", file=self.stream)
                else:
                    self._wrapped(
                        action["instruction"], indent=f"  {index}. ", subsequent_indent="     "
                    )
        if resume:
            print(file=self.stream)
            print("Continue:", file=self.stream)
            print(f"  $ {shlex.join(resume)}", file=self.stream)

    def _wrapped(self, text: str, *, indent: str, subsequent_indent: str | None = None) -> None:
        width = max(60, min(shutil.get_terminal_size(fallback=(100, 24)).columns, 120))
        print(
            textwrap.fill(
                text,
                width=width,
                initial_indent=indent,
                subsequent_indent=subsequent_indent or indent,
                break_long_words=False,
                break_on_hyphens=False,
            ),
            file=self.stream,
        )

    def _style(self, text: str, *codes: str) -> str:
        if not self.color:
            return text
        return f"\x1b[{';'.join(codes)}m{text}\x1b[{_ANSI_RESET}m"


def run_operator_pause(emitter: EventEmitter) -> int | None:
    """Run reviewed terminal actions and replace this process with exact re-entry."""
    from rcp_supervisor.install import _environment
    from rcp_supervisor.limits import INSTALL_TIMEOUT_SECONDS

    if (
        emitter.machine_readable
        or emitter.state != "operator_action_needed"
        or not sys.stdin.isatty()
        or not emitter.stream.isatty()
    ):
        return None
    step = emitter.last_step
    if step is None:
        return None
    commands = [action["argv"] for action in step["actions"] if action.get("kind") == "command"]
    if step["phase"] == "supervisor_restore" and commands:
        dispositions = {"old-machine-destroyed", "old-machine-fenced-and-credentials-revoked"}
        choices = []
        for command in commands:
            if (
                command[:4]
                not in (
                    ["sudo", "rcp", "server", "restore"],
                    ["sudo", PUBLIC_WRAPPER, "server", "restore"],
                )
                or command.count("--old-authority-disposition") != 1
            ):
                return 3
            index = command.index("--old-authority-disposition") + 1
            if index >= len(command) or command[index] not in dispositions:
                return 3
            choices.append((command[index], command[:index] + command[index + 1 :]))
        if len(commands) == 2:
            if {choice[0] for choice in choices} != dispositions or choices[0][1] != choices[1][1]:
                return 3
            for number, (disposition, _) in enumerate(choices, 1):
                print(f"{number}. {disposition}", file=emitter.stream, flush=True)
            print(
                "Choose 1 or 2 to authorize exactly one old-authority disposition; no default is selected.",
                file=emitter.stream,
                flush=True,
            )
            answer = sys.stdin.readline(1024).strip()
            if answer not in {"1", "2"}:
                return 3
            command = commands[int(answer) - 1]
        elif len(commands) == 1:
            print(
                "Press Enter to run the displayed restore confirmation, or type q to stop.",
                file=emitter.stream,
                flush=True,
            )
            answer = sys.stdin.readline(1024)
            if not answer or answer.strip():
                return 3
            command = commands[0]
        else:
            return 3
        try:
            os.execvpe(command[0], command, _environment())
        except OSError:
            return 1
        return 0
    print(
        "Press Enter to perform the displayed actions, or type q to stop.",
        file=emitter.stream,
        flush=True,
    )
    answer = sys.stdin.readline(1024)
    if not answer or answer.strip():
        return 3
    for action in step["actions"]:
        if action["kind"] != "command":
            continue
        try:
            result = subprocess.run(
                action["argv"], env=_environment(), check=False, timeout=INSTALL_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired):
            print(
                "The operator action did not finish. Use the displayed Continue command after correcting it.",
                file=emitter.stream,
                flush=True,
            )
            return 1
        if result.returncode:
            return result.returncode
    if step["phase"] == "team_space_init" and any(
        action.get("kind") == "command" and "init" in action.get("argv", [])
        for action in step["actions"]
    ):
        print(
            "Save the one-time bootstrap code outside logs and command history. Press Enter only after saving it, or type q to stop.",
            file=emitter.stream,
            flush=True,
        )
        answer = sys.stdin.readline(1024)
        if not answer or answer.strip():
            return 3
    try:
        os.execvpe(step["resume_argv"][0], step["resume_argv"], _environment())
    except OSError:
        print(
            "The Continue command could not start; run the displayed command yourself.",
            file=emitter.stream,
            flush=True,
        )
        return 1
    return 0
