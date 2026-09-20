"""Member shell lifetime and metadata; shell bytes are never stored on disk."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import signal
import struct
import termios
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from rcp.agents.write_scope import RegisteredRepositoryRoot
from rcp.config import MachineConfig, Manifest
from rcp.limits import (
    TERMINAL_IDLE_TIMEOUT_SECONDS,
    TERMINAL_MAX_DIMENSION,
    TERMINAL_POLL_INTERVAL_SECONDS,
    TERMINAL_SUBSCRIBER_QUEUE_SIZE,
)
from rcp.terminals import launch, remote
from rcp.terminals.backends import TerminalCapability, machine_capability
from rcp.terminals.models import (
    TerminalFrame,
    TerminalRuntime,
    TerminalSession,
    TerminalUnavailable,
)
from rcp.terminals.probe import TerminalProbe, TerminalProbeCache
from rcp.terminals.runtime import end_runtime, get_runtime, read_ready, sweep_loop
from rcp.terminals.utilities import resolve_repository, save_metadata, timestamp
from rcp.transport.run_stage import RemoteRunStage

logger = logging.getLogger(__name__)


class TerminalManager:
    def __init__(self, data_dir: Path, membership_check: Callable[[str, str], bool]) -> None:
        self.probes = TerminalProbeCache()
        self.data_dir = data_dir
        self.directory = data_dir / "terminals"
        self.membership_check = membership_check
        self.sessions: dict[str, TerminalRuntime] = {}
        self._opening: set[tuple[str, str]] = set()
        self._local_capability: TerminalCapability | None = None
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self._started = False
        self._unit_prefix = (
            "rcp-terminal-" + hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
        )

    async def capability(
        self, machine: MachineConfig, probe: TerminalProbe | None
    ) -> TerminalCapability:
        """Answer a machine's terminal capability, caching the local one.

        A local answer spawns three probe processes and cannot change while
        this process runs, but the repositories view polls it every few
        seconds. Refresh is what re-reads it, alongside the remote probes.
        """
        if machine.host:
            return await asyncio.to_thread(machine_capability, machine, probe)
        if self._local_capability is None:
            self._local_capability = await asyncio.to_thread(machine_capability, machine, None)
        return self._local_capability

    def invalidate_local_capability(self) -> None:
        self._local_capability = None

    async def start(self) -> None:
        if self._started:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "empty").mkdir(exist_ok=True, mode=0o500)
        # Only this application's persisted unit names are stopped. Persisting
        # intent before launch also covers a crash during systemd admission.
        # Reconciliation is best effort per record. This runs before every other
        # startup owner, so a stale record, a moved data directory, or an
        # unreachable machine must never be able to refuse the server a boot.
        for path in self.directory.glob("*.json"):
            try:
                session = TerminalSession(**json.loads(path.read_text()))
            except (OSError, ValueError) as exc:
                logger.warning("Terminal record %s is unreadable; leaving it: %s", path.name, exc)
                continue
            if session.ended_at:
                continue
            if session.unit != f"{self._unit_prefix}-{session.session_id}":
                # Another data directory wrote this record. Its unit is not ours
                # to stop, and refusing to boot would need a hand deletion.
                logger.warning(
                    "Terminal record %s names unit %s, which this data directory does not own.",
                    session.session_id,
                    session.unit,
                )
                session.ended_at = timestamp()
                session.termination_reason = "unit_identity_mismatch"
                save_metadata(self.directory, session)
                continue
            if session.containment == "mirrored":
                try:
                    if session.execution_host:
                        await asyncio.to_thread(
                            remote.stop_remote_unit, session.execution_host, session.unit
                        )
                    else:
                        await asyncio.to_thread(launch.stop_unit, session.unit)
                except (TerminalUnavailable, OSError, RuntimeError) as exc:
                    # The unfinished record is itself the retry. Keep it.
                    logger.warning(
                        "Could not stop terminal unit %s; a later startup retries it: %s",
                        session.unit,
                        exc,
                    )
                    continue
            session.ended_at = timestamp()
            session.termination_reason = "server_restart"
            save_metadata(self.directory, session)
        self._started = True
        self._sweeper = asyncio.create_task(sweep_loop(self))

    async def close(self) -> None:
        self._started = False
        if self._sweeper:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        errors = []
        for runtime in list(self.sessions.values()):
            try:
                await self.end(
                    runtime.session.project_id, runtime.session.session_id, "server_shutdown"
                )
            except Exception as exc:
                errors.append(str(exc))
        self._started = False
        await self.probes.close()
        if errors:
            raise TerminalUnavailable("; ".join(errors))

    async def open(
        self,
        *,
        project_id: str,
        member_id: str,
        manifest: Manifest,
        repository_alias: str,
        repository_inventory: list[RegisteredRepositoryRoot],
        git_read_paths: tuple[str, ...] = (),
        git_environment: dict[str, str] | None = None,
        remote_git_key_relative: str | None = None,
    ) -> TerminalSession:
        key = (project_id, repository_alias)
        async with self._lock:
            if not self._started:
                raise TerminalUnavailable("Terminal manager has not completed startup cleanup.")
            if not self.membership_check(project_id, member_id):
                raise PermissionError("Project membership is required to open a terminal.")
            for runtime in self.sessions.values():
                session = runtime.session
                if session.project_id == project_id and session.repository_id == repository_alias:
                    return session
            if key in self._opening:
                raise TerminalUnavailable("A terminal for this repository is already opening.")
            self._opening.add(key)
        try:
            return await self._open(
                project_id=project_id,
                member_id=member_id,
                manifest=manifest,
                repository_alias=repository_alias,
                repository_inventory=repository_inventory,
                git_read_paths=git_read_paths,
                git_environment=git_environment,
                remote_git_key_relative=remote_git_key_relative,
            )
        finally:
            self._opening.discard(key)

    async def _open(
        self,
        *,
        project_id: str,
        member_id: str,
        manifest: Manifest,
        repository_alias: str,
        repository_inventory: list[RegisteredRepositoryRoot],
        git_read_paths: tuple[str, ...],
        git_environment: dict[str, str] | None,
        remote_git_key_relative: str | None,
    ) -> TerminalSession:
        """Probe, resolve and launch without the manager lock.

        One unreachable machine can cost a probe timeout plus a launch timeout.
        Holding the lock across that would stall every other project's open,
        end and sweep behind it, so only admission and publication take it.
        """
        repository = manifest.repository_map[repository_alias]
        machine = manifest.machine_map[repository.machine]
        probe = await self.probes.ensure(machine) if machine.host else None
        capability = await self.capability(machine, probe)
        if capability.backend is None:
            raise TerminalUnavailable(capability.reason)
        root, protected = await asyncio.to_thread(
            resolve_repository,
            manifest=manifest,
            project_id=project_id,
            repository_id=repository_alias,
            inventory=repository_inventory,
            data_dir=self.data_dir,
            remote_stage=RemoteRunStage(machine.host) if machine.host else None,
        )
        session_id = uuid.uuid4().hex
        session = TerminalSession(
            session_id=session_id,
            project_id=project_id,
            member_id=member_id,
            repository_id=repository_alias,
            path=str(root),
            started_at=timestamp(),
            last_activity_at=timestamp(),
            unit=f"{self._unit_prefix}-{session_id}",
            containment=capability.backend.containment,
            execution_host=machine.host,
        )
        save_metadata(self.directory, session)
        if machine.host:
            start = asyncio.to_thread(
                remote.start_remote,
                machine.host,
                unit=session.unit,
                repository=root,
                protected_paths=protected,
                containment=session.containment,
                expand_environment_option=(probe.expand_environment_option if probe else True),
                git_key_relative=remote_git_key_relative,
            )
        else:
            start = asyncio.to_thread(
                capability.backend.start,
                unit=session.unit,
                repository=root,
                protected_paths=protected,
                git_read_paths=git_read_paths,
                git_environment=git_environment or {},
                empty_directory=self.directory / "empty",
            )
        pending = asyncio.create_task(start)
        try:
            process, master_fd = await asyncio.shield(pending)
        except asyncio.CancelledError:
            # A disconnected request cannot abandon a launch still running
            # in its worker thread. Finish admission, then stop its unit.
            process, master_fd = await pending
            runtime = TerminalRuntime(session, process, master_fd, time.monotonic())
            async with self._lock:
                self.sessions[session_id] = runtime
                await end_runtime(self, runtime, "opening_cancelled")
            raise
        except Exception:
            # The intent was persisted before the launch, and an unfinished
            # record is what startup tries to reconcile. A launch that never
            # produced a unit must not leave one behind.
            session.ended_at = timestamp()
            session.termination_reason = "launch_failed"
            save_metadata(self.directory, session)
            raise
        runtime = TerminalRuntime(session, process, master_fd, time.monotonic())
        if machine.host:
            runtime.completion = remote.CompletionParser()
        async with self._lock:
            self.sessions[session_id] = runtime
            asyncio.get_running_loop().add_reader(master_fd, read_ready, runtime)
            if not self.membership_check(project_id, member_id):
                await end_runtime(self, runtime, "membership_lost")
                raise PermissionError("Project membership ended while the terminal was opening.")
        return session

    async def end_all(self, reason: str = "server_maintenance") -> None:
        errors = []
        async with self._lock:
            for runtime in list(self.sessions.values()):
                try:
                    await end_runtime(self, runtime, reason)
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise TerminalUnavailable("; ".join(errors))

    def list(self, project_id: str) -> list[TerminalSession]:
        return [
            runtime.session
            for runtime in self.sessions.values()
            if runtime.session.project_id == project_id
        ]

    def get(self, project_id: str, session_id: str) -> TerminalSession:
        return get_runtime(self, project_id, session_id).session

    async def end(self, project_id: str, session_id: str, reason: str = "ended") -> None:
        async with self._lock:
            await end_runtime(self, get_runtime(self, project_id, session_id), reason)

    def attach(self, project_id: str, session_id: str) -> asyncio.Queue[TerminalFrame]:
        runtime = get_runtime(self, project_id, session_id)
        queue: asyncio.Queue[TerminalFrame] = asyncio.Queue(TERMINAL_SUBSCRIBER_QUEUE_SIZE)
        if runtime.replay:
            queue.put_nowait(bytes(runtime.replay))
        runtime.subscribers.add(queue)
        runtime.session.state = "live"
        return queue

    def detach(self, project_id: str, session_id: str, queue: asyncio.Queue[TerminalFrame]) -> None:
        runtime = self.sessions.get(session_id)
        if runtime and runtime.session.project_id == project_id:
            runtime.subscribers.discard(queue)
            if not runtime.subscribers:
                runtime.session.state = "idle"

    async def write(self, project_id: str, session_id: str, data: bytes) -> None:
        runtime = get_runtime(self, project_id, session_id)
        view = memoryview(data)
        while view:
            try:
                sent = os.write(runtime.master_fd, view)
                view = view[sent:]
            except BlockingIOError:
                await asyncio.sleep(TERMINAL_POLL_INTERVAL_SECONDS)
                if get_runtime(self, project_id, session_id) is not runtime:
                    raise KeyError("Terminal session ended during input.") from None
        runtime.last_activity = time.monotonic()
        runtime.session.last_activity_at = timestamp()

    def resize(self, project_id: str, session_id: str, cols: int, rows: int) -> None:
        if not (1 <= cols <= TERMINAL_MAX_DIMENSION and 1 <= rows <= TERMINAL_MAX_DIMENSION):
            raise ValueError("Terminal dimensions are outside the supported range.")
        runtime = get_runtime(self, project_id, session_id)
        fcntl.ioctl(runtime.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        # systemd-run forwards SIGWINCH to its inner PTY. Its outer PTY is not
        # the controlling terminal after start_new_session, so notify explicitly.
        runtime.process.send_signal(signal.SIGWINCH)

    async def sweep(self) -> None:
        errors = []
        for runtime in list(self.sessions.values()):
            session = runtime.session
            try:
                if not self.membership_check(session.project_id, session.member_id):
                    reason = "membership_lost"
                elif runtime.process.poll() is not None:
                    # Drain completion evidence before classifying SSH's ambiguous 255.
                    while read_ready(runtime):
                        pass
                    reason = (
                        "link_dropped"
                        if session.execution_host
                        and runtime.process.poll() == 255
                        and runtime.completion.exit_code is None
                        else "shell_exited"
                    )
                elif time.monotonic() - runtime.last_activity >= TERMINAL_IDLE_TIMEOUT_SECONDS:
                    reason = "idle_timeout"
                else:
                    continue
                # A route may end this session while an earlier stop yields.
                async with self._lock:
                    if self.sessions.get(session.session_id) is runtime:
                        await end_runtime(self, runtime, reason)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise TerminalUnavailable("; ".join(errors))
