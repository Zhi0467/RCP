from __future__ import annotations

import faulthandler
import os
import threading
from collections.abc import Iterator

import pytest

HANG_SECONDS = 120


@pytest.fixture(autouse=True)
def hang_guard() -> Iterator[None]:
    """End the process if a lifecycle test outlives its ceiling, so a stuck action fails its shard.

    A timer thread dumps every thread's stack while it holds the interpreter
    lock, then exits. ``faulthandler.dump_traceback_later`` dumps from its own
    native thread without that lock and can spin inside a frame walk on a
    loaded host, which hangs the shard instead of failing it.
    """

    def expire() -> None:
        faulthandler.dump_traceback(all_threads=True)
        os._exit(1)

    timer = threading.Timer(HANG_SECONDS, expire)
    timer.daemon = True
    timer.start()
    yield
    timer.cancel()
