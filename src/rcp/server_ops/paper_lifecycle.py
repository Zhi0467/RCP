"""Bridge synchronous lifecycle control workers to the server's live Paper owner."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Protocol
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from rcp.paper import PaperLive


class LivePaperLifecycle:
    def __init__(self, live: PaperLive):
        self.live = live

    def _call(self, method: str, project_id: str) -> None:
        loop = self.live.loop
        if loop is None or not loop.is_running():
            raise RuntimeError("PaperLive lifecycle owner is not running.")
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            raise RuntimeError("Synchronous Paper lifecycle requires a control worker thread.")
        asyncio.run_coroutine_threadsafe(getattr(self.live, method)(project_id), loop).result()

    def pause(self, project_id: str) -> None:
        self._call("pause", project_id)

    def drain(self, project_id: str) -> None:
        self._call("drain", project_id)

    def dispose(self, project_id: str) -> None:
        self._call("dispose", project_id)


class PaperLifecycleOwner(Protocol):
    def pause(self, project_id: str) -> None: ...

    def drain(self, project_id: str) -> None: ...

    def dispose(self, project_id: str) -> None: ...


class PaperLifecycleStore(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


_owners: WeakKeyDictionary[PaperLifecycleStore, PaperLifecycleOwner] = WeakKeyDictionary()
_owners_lock = threading.Lock()


def register_paper_live(store: PaperLifecycleStore, live: PaperLive) -> None:
    """Register the one process owner; never instantiate a second room set."""
    with _owners_lock:
        existing = _owners.get(store)
        if existing is not None:
            if isinstance(existing, LivePaperLifecycle) and existing.live is live:
                return
            raise RuntimeError("The store already has a PaperLive lifecycle owner.")
        _owners[store] = LivePaperLifecycle(live)


def unregister_paper_live(store: PaperLifecycleStore, live: PaperLive) -> None:
    with _owners_lock:
        owner = _owners.get(store)
        if isinstance(owner, LivePaperLifecycle) and owner.live is live:
            del _owners[store]


def get_paper_live(store: PaperLifecycleStore, project_id: str) -> PaperLifecycleOwner | None:
    with _owners_lock:
        live = _owners.get(store)
    if live is not None:
        return live
    # Legacy-only stores have no live workers. New durable rows require explicit
    # registration, otherwise claiming quiescence would silently omit publishers.
    from rcp.server_ops.backup_paper import PAPER_PROJECT_TABLES

    with store.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in PAPER_PROJECT_TABLES:
            if (
                table in tables
                and connection.execute(
                    f'SELECT 1 FROM "{table}" WHERE project_id=? LIMIT 1', (project_id,)
                ).fetchone()
            ):
                raise RuntimeError("PaperLive lifecycle owner is required for saved paper state.")
    return None


def stop_paper_project(store: PaperLifecycleStore, project_id: str) -> None:
    live = get_paper_live(store, project_id)
    if live is not None:
        live.pause(project_id)
        live.drain(project_id)
        live.dispose(project_id)


def detach_paper_runtime(store: PaperLifecycleStore) -> None:
    """Discard process-local job claims and destinations before offline restore.

    The contract persists content and publication intent, not worker claims or
    transport destinations. Those must be rebuilt from validated restored
    targets after startup admission opens; no saved revision or hold is reset.
    """
    with _owners_lock:
        live = _owners.get(store)
    if live is None:
        return
    with store.connection() as connection:
        project_ids = [row[0] for row in connection.execute("SELECT project_id FROM projects")]
    for project_id in project_ids:
        live.pause(project_id)
    for project_id in project_ids:
        live.drain(project_id)
        live.dispose(project_id)
