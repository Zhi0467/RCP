"""Member shell lifetime and metadata; shell bytes are never stored on disk."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import struct
import termios
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from rcp.agents.write_scope import RegisteredRepositoryRoot
from rcp.config import Manifest
from rcp.limits import (
    TERMINAL_IDLE_TIMEOUT_SECONDS,
    TERMINAL_MAX_DIMENSION,
    TERMINAL_POLL_INTERVAL_SECONDS,
    TERMINAL_SUBSCRIBER_QUEUE_SIZE,
)
from rcp.terminals import launch
from rcp.terminals.backends import machine_capability
from rcp.terminals.models import TerminalRuntime, TerminalSession, TerminalUnavailable
from rcp.terminals.runtime import end_runtime, get_runtime, read_ready, sweep_loop
from rcp.terminals.utilities import resolve_repository, save_metadata, timestamp


class TerminalManager:
    def __init__(self, data_dir: Path, membership_check: Callable[[str, str], bool]) -> None:
        self.data_dir = data_dir
        self.directory = data_dir / "terminals"
        self.membership_check = membership_check
        self.sessions: dict[str, TerminalRuntime] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task[None] | None = None
        self._started = False
        self._unit_prefix = (
            "rcp-terminal-" + hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
        )

    async def start(self) -> None:
        if self._started:
            return
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.directory / "empty").mkdir(exist_ok=True, mode=0o500)
        # Only this application's persisted unit names are stopped. Persisting
        # intent before launch also covers a crash during systemd admission.
        for path in self.directory.glob("*.json"):
            session = TerminalSession(**json.loads(path.read_text()))
            if session.ended_at:
                continue
            if session.unit != f"{self._unit_prefix}-{session.session_id}":
                raise TerminalUnavailable("Terminal cleanup found an unexpected unit identity.")
            if session.containment == "mirrored":
                await asyncio.to_thread(launch.stop_unit, session.unit)
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
    ) -> TerminalSession:
        async with self._lock:
            if not self._started:
                raise TerminalUnavailable("Terminal manager has not completed startup cleanup.")
            if not self.membership_check(project_id, member_id):
                raise PermissionError("Project membership is required to open a terminal.")
            for runtime in self.sessions.values():
                session = runtime.session
                if session.project_id == project_id and session.repository_id == repository_alias:
                    return session
            repository = manifest.repository_map[repository_alias]
            capability = await asyncio.to_thread(
                machine_capability, manifest.machine_map[repository.machine]
            )
            if capability.backend is None:
                raise TerminalUnavailable(capability.reason)
            root, protected = resolve_repository(
                manifest=manifest,
                project_id=project_id,
                repository_id=repository_alias,
                inventory=repository_inventory,
                data_dir=self.data_dir,
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
            )
            save_metadata(self.directory, session)
            pending = asyncio.create_task(
                asyncio.to_thread(
                    capability.backend.start,
                    unit=session.unit,
                    repository=root,
                    protected_paths=protected,
                    git_read_paths=git_read_paths,
                    git_environment=git_environment or {},
                    empty_directory=self.directory / "empty",
                )
            )
            try:
                process, master_fd = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # A disconnected request cannot abandon a launch still running
                # in its worker thread. Finish admission, then stop its unit.
                process, master_fd = await pending
                runtime = TerminalRuntime(session, process, master_fd, time.monotonic())
                self.sessions[session_id] = runtime
                await end_runtime(self, runtime, "opening_cancelled")
                raise
            runtime = TerminalRuntime(session, process, master_fd, time.monotonic())
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

    def attach(self, project_id: str, session_id: str) -> asyncio.Queue[bytes | None]:
        runtime = get_runtime(self, project_id, session_id)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(TERMINAL_SUBSCRIBER_QUEUE_SIZE)
        if runtime.replay:
            queue.put_nowait(bytes(runtime.replay))
        runtime.subscribers.add(queue)
        runtime.session.state = "live"
        return queue

    def detach(self, project_id: str, session_id: str, queue: asyncio.Queue[bytes | None]) -> None:
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
                    reason = "shell_exited"
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
