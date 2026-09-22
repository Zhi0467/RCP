"""How long this machine has been awake since it last slept.

A laptop that sleeps with its lid closed still wakes for a few seconds at a
time. An automatic launch started in one of those short wakes loses its link
partway through, so automatic launches wait until the machine has stayed awake
long enough to finish one. Anything a human starts is never held: a human
acting is a machine awake.

Two clocks name a sleep: one that counts time asleep and one that does not.
Their difference grows only while the machine sleeps, and neither follows the
wall clock, so a time-sync step or a manual clock change is not a sleep. The
same clock name means opposite things on macOS and Linux, so each platform
names its pair here rather than trusting `time.monotonic()` to keep one
meaning. A platform with no known pair, or a pair that stops behaving as
named, has no sleep detection, and launches there are never held.

A sleep is seen at the first reading after it, so the awake streak starts at
that reading, never earlier than the true wake.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections.abc import Callable

from rcp.limits import AUTOMATIC_LAUNCH_AWAKE_SECONDS, SLEEP_DETECTION_TOLERANCE_SECONDS

logger = logging.getLogger(__name__)

# Platform -> (clock that counts sleep, clock that skips it). On macOS
# CLOCK_MONOTONIC keeps counting through sleep; on Linux it stops.
_SLEEP_CLOCK_PAIRS: dict[str, tuple[str, str]] = {
    "darwin": ("CLOCK_MONOTONIC", "CLOCK_UPTIME_RAW"),
    "linux": ("CLOCK_BOOTTIME", "CLOCK_MONOTONIC"),
}


class AwakeStreak:
    """Readings of one machine's two clocks, and the sleep they reveal."""

    def __init__(
        self,
        platform: str = sys.platform,
        read_clock: Callable[[int], float] = time.clock_gettime,
        clock_ids: dict[str, int] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._read = read_clock
        self._clocks = _resolve_clocks(platform, read_clock, clock_ids)
        self._slept = 0.0
        # None: no sleep seen since this process started, so it reads as long awake.
        self._streak_started: float | None = None
        if self._clocks is not None:
            self._slept = self._sleep_so_far(self._clocks)

    def awake_seconds(self) -> float | None:
        """Seconds awake since the last sleep, or None when this machine cannot tell."""

        with self._lock:
            if self._clocks is None:
                return None
            counts, skips = self._clocks
            asleep, awake = self._read(counts), self._read(skips)
            grew = (asleep - awake) - self._slept
            if grew < -SLEEP_DETECTION_TOLERANCE_SECONDS:
                logger.warning(
                    "The sleep-counting clock fell behind the awake clock by %.1f s, so "
                    "sleep detection is off and automatic launches are no longer held.",
                    -grew,
                )
                self._clocks = None
                return None
            if grew > SLEEP_DETECTION_TOLERANCE_SECONDS:
                logger.info(
                    "This machine slept for %.0f s; automatic launches wait until it has "
                    "been awake %.0f s.",
                    grew,
                    AUTOMATIC_LAUNCH_AWAKE_SECONDS,
                )
                self._streak_started = awake
            self._slept = asleep - awake
            if self._streak_started is None:
                return math.inf
            return awake - self._streak_started

    def seconds_until_automatic_launch(self) -> float:
        """How much longer an automatic launch should wait; 0 when it may start now."""

        awake = self.awake_seconds()
        if awake is None:
            return 0.0
        return max(0.0, AUTOMATIC_LAUNCH_AWAKE_SECONDS - awake)

    def _sleep_so_far(self, clocks: tuple[int, int]) -> float:
        counts, skips = clocks
        return self._read(counts) - self._read(skips)


def _resolve_clocks(
    platform: str,
    read_clock: Callable[[int], float],
    clock_ids: dict[str, int] | None,
) -> tuple[int, int] | None:
    names = next(
        (pair for prefix, pair in _SLEEP_CLOCK_PAIRS.items() if platform.startswith(prefix)),
        None,
    )
    if names is None:
        logger.info(
            "No sleep clocks are known for %s; automatic launches are never held.", platform
        )
        return None
    lookup = clock_ids if clock_ids is not None else vars(time)
    ids = tuple(lookup.get(name) for name in names)
    if not all(isinstance(value, int) for value in ids):
        logger.info("This Python has no %s; automatic launches are never held.", " or ".join(names))
        return None
    counts, skips = ids
    try:
        slept = read_clock(counts) - read_clock(skips)
    except OSError as exc:
        logger.info("Sleep clocks are unreadable (%s); automatic launches are never held.", exc)
        return None
    if slept < -SLEEP_DETECTION_TOLERANCE_SECONDS:
        logger.warning(
            "%s reads behind %s, so they do not count sleep as named; automatic launches "
            "are never held.",
            names[0],
            names[1],
        )
        return None
    return counts, skips


_MACHINE = AwakeStreak()


def seconds_until_automatic_launch() -> float:
    """How much longer this machine's automatic launches should wait."""

    return _MACHINE.seconds_until_automatic_launch()
