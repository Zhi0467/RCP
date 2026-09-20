from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import TYPE_CHECKING

from rcp.limits import (
    TERMINAL_IO_CHUNK_BYTES,
    TERMINAL_OUTPUT_BUFFER_BYTES,
    TERMINAL_STOP_TIMEOUT_SECONDS,
    TERMINAL_SWEEP_INTERVAL_SECONDS,
)
from rcp.terminals import launch, remote
from rcp.terminals.models import (
    DETACHED,
    TerminalRuntime,
    TerminalSession,
    TerminalUnavailable,
)
from rcp.terminals.utilities import save_metadata, timestamp

if TYPE_CHECKING:
    from rcp.terminals.manager import TerminalManager

logger = logging.getLogger(__name__)


def get_runtime(manager: TerminalManager, project_id: str, session_id: str) -> TerminalRuntime:
    runtime = manager.sessions.get(session_id)
    if runtime is None or runtime.session.project_id != project_id:
        raise KeyError("Terminal session not found.")
    return runtime


async def end_runtime(manager: TerminalManager, runtime: TerminalRuntime, reason: str) -> None:
    pending = asyncio.create_task(_finish_end(manager, runtime, reason))
    try:
        await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Complete descriptor/process retirement before releasing the manager
        # lock, even if shutdown cancels a sweep during systemctl stop.
        await pending
        raise


async def _finish_end(manager: TerminalManager, runtime: TerminalRuntime, reason: str) -> None:
    session = runtime.session
    stopped = True
    if session.execution_host:
        # Closing this exact SSH PTY hangs up its remote supervisor, which stops
        # the unit, and that hangup is never acknowledged. An SSH that exited on
        # its own is no better evidence: the supervisor writes its completion
        # marker before the cleanup that can still fail, and it reports that
        # failure only through an exit status a dropped link produces too. A
        # finished shell therefore says nothing about whether its unit went with
        # it, so the stop is confirmed over a fresh connection either way.
        if runtime.process.poll() is None:
            runtime.process.terminate()
        # Only a mirrored session has a unit. A cooperative supervisor owns
        # a plain PTY, and its machine may have no systemctl to ask.
        if session.containment == "mirrored":
            stopped = await _confirm_remote_stop(manager, session)
    elif session.containment == "mirrored":
        await asyncio.to_thread(launch.stop_unit, session.unit)
    else:
        await asyncio.to_thread(launch.stop_cooperative, runtime.process)
    # Retire once before any subsequent cleanup/audit operation can fail. The
    # unfinished persisted intent still causes startup to retry the unit stop.
    manager.sessions.pop(session.session_id, None)
    asyncio.get_running_loop().remove_reader(runtime.master_fd)
    os.close(runtime.master_fd)
    if runtime.process.poll() is None:
        runtime.process.terminate()
        try:
            await asyncio.to_thread(runtime.process.wait, timeout=TERMINAL_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            runtime.process.kill()
            await asyncio.to_thread(runtime.process.wait)
    for queue in runtime.subscribers:
        while queue.full():
            queue.get_nowait()
        queue.put_nowait(None)
    runtime.replay.clear()
    session.state = "idle"
    session.termination_reason = reason
    if stopped:
        session.ended_at = timestamp()
    else:
        # A unit that may still own the checkout must also block a reopen, not
        # only wait for the next startup.
        manager.mark_unresolved(session)
    save_metadata(manager.directory, session)


async def _confirm_remote_stop(manager: TerminalManager, session: TerminalSession) -> bool:
    """Report whether the remote unit is known to be gone.

    An unfinished persisted intent is what makes the next startup reconcile a
    unit, so a stop this server cannot confirm must leave one behind.
    """
    if not manager._started:
        # Shutdown must not wait on the network for every live session.
        return False
    try:
        await asyncio.to_thread(
            remote.stop_remote_unit,
            session.execution_host,
            session.unit,
            session.declared_account,
        )
    except TerminalUnavailable as exc:
        logger.warning(
            "Terminal unit %s may survive on its execution machine; startup will retry: %s",
            session.unit,
            exc,
        )
        return False
    return True


def read_ready(runtime: TerminalRuntime) -> bool:
    try:
        chunk = os.read(runtime.master_fd, TERMINAL_IO_CHUNK_BYTES)
    except BlockingIOError:
        return False
    except OSError:
        chunk = b""
    if not chunk:
        asyncio.get_running_loop().remove_reader(runtime.master_fd)
        if runtime.completion is not None:
            chunk = runtime.completion.finish()
        if not chunk:
            return False
    elif runtime.completion is not None:
        chunk = runtime.completion.feed(chunk)
    if not chunk:
        return True
    runtime.replay.extend(chunk)
    del runtime.replay[:-TERMINAL_OUTPUT_BUFFER_BYTES]
    # Output does not reset idle expiry: a forgotten noisy command must not
    # leak its shell forever. Only the member's input renews the lifetime.
    for queue in list(runtime.subscribers):
        if queue.full():
            # This viewer cannot keep up. Detaching it is not an ending.
            runtime.subscribers.remove(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(DETACHED)
        else:
            queue.put_nowait(chunk)
    if not runtime.subscribers:
        runtime.session.state = "idle"
    return True


async def sweep_loop(manager: TerminalManager) -> None:
    while True:
        await asyncio.sleep(TERMINAL_SWEEP_INTERVAL_SECONDS)
        try:
            await manager.sweep()
        except Exception:
            logger.exception("Terminal lifecycle cleanup failed; it will be retried")
