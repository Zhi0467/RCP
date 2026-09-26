"""Lifetime coordination between source servers and the source updater."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def source_checkout_root() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    marker = root / ".git"
    return root if marker.is_dir() or marker.is_file() else None


@contextmanager
def source_checkout_lock() -> Iterator[None]:
    """Keep all source backends out of an exclusive source update."""
    root = source_checkout_root()
    if root is None:
        yield
        return
    with (root / ".rcp-serve.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
