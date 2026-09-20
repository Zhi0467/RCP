"""Machine-scoped remote readiness, with nonblocking projection reads."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from rcp.compute_jobs.text import safe_compute_diagnostic
from rcp.config import MachineConfig
from rcp.limits import (
    TERMINAL_PROBE_COMMAND_TIMEOUT_SECONDS,
    TERMINAL_PROBE_THREADS,
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
    # `--expand-environment=no` arrived in systemd 254; 0 means unknown.
    systemd_version: int = 0
    # The account the machine actually answered as; "" when it could not say.
    os_account: str = ""

    @property
    def expand_environment_option(self) -> bool:
        return self.systemd_version >= 254

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
    version = payload.get("systemd_version")
    account = payload.get("os_account")
    return TerminalProbe(
        payload["os_name"],
        payload["state"],
        safe_compute_diagnostic(payload["diagnostic"]),
        version if isinstance(version, int) and version >= 0 else 0,
        account if isinstance(account, str) else "",
    )


class TerminalProbeCache:
    """One cache per terminal manager; all access belongs to its event loop."""

    def __init__(self, probe: Callable[[MachineConfig], TerminalProbe] | None = None) -> None:
        self._probe = probe or probe_remote_terminal
        self._entries: dict[tuple[str, str, str], asyncio.Task[TerminalProbe]] = {}
        self._tasks: set[asyncio.Task[TerminalProbe]] = set()
        self._slots = asyncio.Semaphore(TERMINAL_PROBE_WORKERS)
        self._threads = ThreadPoolExecutor(
            max_workers=TERMINAL_PROBE_THREADS, thread_name_prefix="rcp-terminal-probe"
        )
        self._closed = False

    @staticmethod
    def _key(machine: MachineConfig) -> tuple[str, str, str]:
        return machine.alias, machine.host, machine.os_account

    async def _run(self, machine: MachineConfig) -> TerminalProbe:
        async with self._slots:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(self._threads, self._probe, machine)
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
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A refresh cancels the probe it supersedes, so wait for the
                # one that replaced it. A cancellation of this caller leaves
                # the task running and belongs to the caller.
                if not task.cancelled():
                    raise
                continue
            # An explicit refresh must not let an older in-flight answer win.
            if self._entries.get(self._key(machine)) is task:
                return result

    def invalidate(self, machine: MachineConfig | None = None) -> None:
        """Drop cached answers, and the probes still producing them.

        A superseded probe holds one of the few worker slots until its own
        subprocess timeout, so leaving it to run makes each refresh of an
        unreachable machine queue behind the timeouts of the refreshes before
        it, and the newest answer is the one kept waiting. Cancelling gives
        the slot back at once; one already inside its worker thread ends
        there with nobody reading it, as at close.
        """
        if machine is None:
            superseded = list(self._entries.values())
            self._entries.clear()
        else:
            task = self._entries.pop(self._key(machine), None)
            superseded = [task] if task is not None else []
        for task in superseded:
            task.cancel()

    async def close(self) -> None:
        # Shutdown reads no probe result, and the lifespan's `finally` holds the
        # instance lock until this returns. Draining instead of cancelling let
        # each batch of unreachable machines spend a full probe timeout in turn,
        # so a project with enough of them outlasted the window a replacement
        # server waits for the old one to go. Cancelling drops the probes still
        # queued for a worker outright and detaches the few already in a thread,
        # which end on their own subprocess timeout with nobody reading them.
        self._closed = True
        self._entries.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        # Queued probes are dropped; the few already in a thread are left to
        # end on their own subprocess timeout, because shutdown holds the
        # instance lock and must not wait on the network.
        self._threads.shutdown(wait=False, cancel_futures=True)
