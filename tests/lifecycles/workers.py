"""The one seam an explorer puts on worker dispatch: the thread never starts.

The real ``_spawn_record`` runs, so a dispatched turn carries the
``operation_dispatch_attempt`` and ``operation_dispatch_started`` receipts that
startup recovery reads. Only the thread is held: it reports alive and its
turn advances only through the machine's actions.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock


class HeldWorker(threading.Thread):
    """A dispatched worker whose body never runs."""

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        pass


@contextmanager
def held_workers() -> Iterator[None]:
    """``rcp.background`` builds every worker thread as a ``HeldWorker``."""

    import rcp.background

    held = SimpleNamespace(
        **{name: getattr(threading, name) for name in dir(threading) if not name.startswith("__")}
    )
    held.Thread = HeldWorker
    with mock.patch.object(rcp.background, "threading", held):
        yield
