"""Machine-scoped remote readiness, with nonblocking projection reads."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import MachineConfig
from rcp.limits import (
    TERMINAL_PROBE_COMMAND_TIMEOUT_SECONDS,
    TERMINAL_PROBE_TIMEOUT_SECONDS,
    TERMINAL_PROBE_WORKERS,
)
from rcp.transport.remote_compute_probe import classify_ssh_failure
from rcp.transport.ssh import ssh_arguments
from rcp.transport.state import _remote_script

ProbeState = Literal[
    "pending", "reachable", "unreachable", "authentication_failed", "host_key_failed", "incapable"
]


@dataclass(frozen=True)
class TerminalProbe:
    os_name: str | None
    state: ProbeState
    diagnostic: str

    @property
    def ready(self) -> bool:
        return self.state == "reachable"


def probe_remote_terminal(machine: MachineConfig, *, runner=subprocess.run) -> TerminalProbe:
    command = shlex.join(
        [
            "python3",
            "-c",
            _remote_script("remote_terminal_probe.py"),
            str(TERMINAL_PROBE_COMMAND_TIMEOUT_SECONDS),
        ]
    )
    try:
        completed = runner(
            ssh_arguments(machine.host, command, strict_host_key_checking=True),
            capture_output=True,
            text=True,
            timeout=TERMINAL_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TerminalProbe(None, "unreachable", "The terminal capability probe timed out.")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return TerminalProbe(None, "unreachable", safe_compute_diagnostic(str(exc)))
    if completed.returncode:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"SSH probe exited {completed.returncode}."
        )
        return TerminalProbe(
            None,
            classify_ssh_failure(detail) if completed.returncode == 255 else "incapable",
            safe_compute_diagnostic(detail),
        )
    try:
        payload = json.loads(completed.stdout)
        if (
            not isinstance(payload, dict)
            or payload.get("state") not in {"reachable", "incapable"}
            or not isinstance(payload.get("os_name"), str)
            or not payload["os_name"]
            or not isinstance(payload.get("diagnostic"), str)
            or not payload["diagnostic"].strip()
        ):
            raise ValueError("invalid probe fields")
    except (ValueError, TypeError):
        return TerminalProbe(
            None, "incapable", "The execution machine returned an invalid terminal probe."
        )
    return TerminalProbe(
        payload["os_name"], payload["state"], safe_compute_diagnostic(payload["diagnostic"])
    )


class TerminalProbeCache:
    """One cache per terminal manager; all access belongs to its event loop."""

    def __init__(self, probe: Callable[[MachineConfig], TerminalProbe] | None = None) -> None:
        self._probe = probe or probe_remote_terminal
        self._entries: dict[tuple[str, str, str], asyncio.Task[TerminalProbe]] = {}
        self._tasks: set[asyncio.Task[TerminalProbe]] = set()
        self._slots = asyncio.Semaphore(TERMINAL_PROBE_WORKERS)
        self._closed = False

    @staticmethod
    def _key(machine: MachineConfig) -> tuple[str, str, str]:
        return machine.alias, machine.host, machine.os_account

    async def _run(self, machine: MachineConfig) -> TerminalProbe:
        async with self._slots:
            try:
                return await asyncio.to_thread(self._probe, machine)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                return TerminalProbe(None, "unreachable", safe_compute_diagnostic(str(exc)))

    def _task(self, machine: MachineConfig) -> asyncio.Task[TerminalProbe]:
        if self._closed:
            raise RuntimeError("Terminal probe cache is closed.")
        key = self._key(machine)
        task = self._entries.get(key)
        if task is None:
            task = asyncio.create_task(self._run(machine.model_copy(deep=True)))
            self._entries[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return task

    def get(self, machine: MachineConfig) -> TerminalProbe:
        task = self._task(machine)
        return (
            task.result()
            if task.done()
            else TerminalProbe(None, "pending", "Checking terminal capability on this machine.")
        )

    async def ensure(self, machine: MachineConfig) -> TerminalProbe:
        while True:
            task = self._task(machine)
            result = await asyncio.shield(task)
            # An explicit refresh must not let an older in-flight answer win.
            if self._entries.get(self._key(machine)) is task:
                return result

    def invalidate(self, machine: MachineConfig | None = None) -> None:
        if machine is None:
            self._entries.clear()
        else:
            self._entries.pop(self._key(machine), None)

    async def close(self) -> None:
        self._closed = True
        if self._tasks:
            await asyncio.gather(*self._tasks)
        self._entries.clear()
