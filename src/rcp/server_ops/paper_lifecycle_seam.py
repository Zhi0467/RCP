"""S2 integration seam for the process-owned ``PaperLive`` lifecycle.

S1 registers its synchronous facade once per store. The facade marshals room
work to its owner loop; callers here run on lifecycle/control worker threads.
Dispose unloads a project's rooms and workers. A later admitted request may
reopen them; maintenance admission remains closed until startup is safe.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import AbstractContextManager
from typing import Protocol
from weakref import WeakKeyDictionary


class PaperLifecycleOwner(Protocol):
    def pause(self, project_id: str) -> None: ...

    def drain(self, project_id: str) -> None: ...

    def dispose(self, project_id: str) -> None: ...


class PaperLifecycleStore(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


_owners: WeakKeyDictionary[PaperLifecycleStore, PaperLifecycleOwner] = WeakKeyDictionary()
_owners_lock = threading.Lock()


def register_paper_live(store: PaperLifecycleStore, live: PaperLifecycleOwner) -> None:
    """S1 wiring: register the actual owner, never instantiate another room set."""
    with _owners_lock:
        existing = _owners.get(store)
        if existing is not None and existing is not live:
            raise RuntimeError("The store already has a PaperLive lifecycle owner.")
        _owners[store] = live


def get_paper_live(store: PaperLifecycleStore, project_id: str) -> PaperLifecycleOwner | None:
    with _owners_lock:
        live = _owners.get(store)
    if live is not None:
        return live
    # Legacy-only stores have no live workers. New durable rows require explicit
    # S1 wiring, otherwise claiming quiescence would silently omit publishers.
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
