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
from rcp.terminals import launch
from rcp.terminals.models import TerminalRuntime
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
    await asyncio.to_thread(launch.stop_unit, session.unit)
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
    session.ended_at = timestamp()
    session.termination_reason = reason
    save_metadata(manager.directory, session)


def read_ready(runtime: TerminalRuntime) -> None:
    try:
        chunk = os.read(runtime.master_fd, TERMINAL_IO_CHUNK_BYTES)
    except BlockingIOError:
        return
    except OSError:
        chunk = b""
    if not chunk:
        asyncio.get_running_loop().remove_reader(runtime.master_fd)
        return
    runtime.replay.extend(chunk)
    del runtime.replay[:-TERMINAL_OUTPUT_BUFFER_BYTES]
    # Output does not reset idle expiry: a forgotten noisy command must not
    # leak its shell forever. Only the member's input renews the lifetime.
    for queue in list(runtime.subscribers):
        if queue.full():
            runtime.subscribers.remove(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)
        else:
            queue.put_nowait(chunk)
    if not runtime.subscribers:
        runtime.session.state = "idle"


async def sweep_loop(manager: TerminalManager) -> None:
    while True:
        await asyncio.sleep(TERMINAL_SWEEP_INTERVAL_SECONDS)
        try:
            await manager.sweep()
        except Exception:
            logger.exception("Terminal lifecycle cleanup failed; it will be retried")
