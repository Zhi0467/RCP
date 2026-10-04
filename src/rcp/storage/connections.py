"""Bounded, thread-owned SQLite handles; only idle handles may close cross-thread."""

from __future__ import annotations

import sqlite3
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rcp.limits import STORAGE_IDLE_CONNECTION_LIMIT

_LOCK = threading.RLock()


@dataclass(eq=False)
class _Connection:
    handle: sqlite3.Connection
    identity: tuple[int, int]
    active: bool = True
    retired: bool = False


_CONNECTIONS: dict[tuple[object, object], _Connection] = {}
_IDLE: OrderedDict[tuple[object, object], None] = OrderedDict()


def _discard(key: tuple[object, object]) -> None:
    entry = _CONNECTIONS.pop(key)
    _IDLE.pop(key, None)
    entry.handle.close()


def _retire(owner: object) -> None:
    with _LOCK:
        for key, entry in list(_CONNECTIONS.items()):
            if owner in key:
                if entry.active:
                    entry.retired = True
                else:
                    _discard(key)


class _ThreadOwner:
    def __init__(self) -> None:
        self.token = object()
        weakref.finalize(self, _retire, self.token)


class ConnectionCache:
    def __init__(self) -> None:
        self.token = object()
        self.local = threading.local()
        weakref.finalize(self, _retire, self.token)

    def close(self) -> None:
        """Close idle handles now and active handles when their blocks finish."""
        _retire(self.token)

    def acquire(
        self, path: Path, connect: Callable[[], sqlite3.Connection]
    ) -> tuple[tuple[object, object], sqlite3.Connection] | None:
        if not hasattr(self.local, "owner"):
            self.local.owner = _ThreadOwner()
        key = (self.token, self.local.owner.token)
        with _LOCK:
            entry = _CONNECTIONS.get(key)
            if entry is not None and entry.active:
                return None  # Nested blocks must commit independently.
            try:
                info = path.stat()
                identity = (info.st_dev, info.st_ino)
            except FileNotFoundError:
                identity = None
            if entry is not None and entry.identity != identity:
                _discard(key)
                entry = None
            if entry is None:
                handle = connect()
                try:
                    info = path.stat()
                except BaseException:
                    handle.close()
                    raise
                entry = _Connection(handle, (info.st_dev, info.st_ino))
                _CONNECTIONS[key] = entry
            entry.active = True
            _IDLE.pop(key, None)
            return key, entry.handle

    def release(self, key: tuple[object, object], *, discard: bool) -> None:
        with _LOCK:
            entry = _CONNECTIONS[key]
            entry.active = False
            if discard or entry.retired:
                _discard(key)
                return
            _IDLE[key] = None
            while len(_IDLE) > STORAGE_IDLE_CONNECTION_LIMIT:
                _discard(next(iter(_IDLE)))
