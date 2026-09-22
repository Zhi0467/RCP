from __future__ import annotations

import sys

import pytest

from rcp.limits import AUTOMATIC_LAUNCH_AWAKE_SECONDS
from rcp.machine_sleep import AwakeStreak

MAC = ("darwin", {"CLOCK_MONOTONIC": 1, "CLOCK_UPTIME_RAW": 2})
# The same name, CLOCK_MONOTONIC, is the clock that skips sleep here.
LINUX = ("linux", {"CLOCK_BOOTTIME": 1, "CLOCK_MONOTONIC": 2})


class _Clocks:
    """Clock 1 counts sleep, clock 2 does not."""

    def __init__(self) -> None:
        self.counts = self.skips = 1_000.0

    def read(self, clock_id: int) -> float:
        return self.counts if clock_id == 1 else self.skips

    def awake(self, seconds: float) -> None:
        self.counts += seconds
        self.skips += seconds

    def asleep(self, seconds: float) -> None:
        self.counts += seconds


@pytest.mark.parametrize(("platform", "clock_ids"), [MAC, LINUX])
def test_a_sleep_holds_automatic_launches_until_the_machine_stays_awake(
    platform: str, clock_ids: dict[str, int]
) -> None:
    clocks = _Clocks()
    streak = AwakeStreak(platform, clocks.read, clock_ids)
    # No sleep since the process started: nothing is held at startup.
    assert streak.seconds_until_automatic_launch() == 0

    clocks.asleep(900)
    clocks.awake(1)
    assert streak.seconds_until_automatic_launch() == AUTOMATIC_LAUNCH_AWAKE_SECONDS
    clocks.awake(20)
    assert streak.seconds_until_automatic_launch() == AUTOMATIC_LAUNCH_AWAKE_SECONDS - 20
    # Another short wake starts the streak over.
    clocks.asleep(900)
    clocks.awake(2)
    assert streak.seconds_until_automatic_launch() == AUTOMATIC_LAUNCH_AWAKE_SECONDS
    clocks.awake(AUTOMATIC_LAUNCH_AWAKE_SECONDS)
    assert streak.seconds_until_automatic_launch() == 0


@pytest.mark.parametrize(
    ("platform", "clock_ids"),
    [("win32", MAC[1]), ("darwin", {"CLOCK_MONOTONIC": 1})],
)
def test_a_machine_without_a_known_pair_never_holds_a_launch(
    platform: str, clock_ids: dict[str, int]
) -> None:
    clocks = _Clocks()
    streak = AwakeStreak(platform, clocks.read, clock_ids)
    clocks.asleep(900)
    assert streak.awake_seconds() is None
    assert streak.seconds_until_automatic_launch() == 0


def test_a_pair_that_stops_behaving_as_named_turns_detection_off() -> None:
    """A future OS that swapped what a name means must fail open, not hold forever."""

    clocks = _Clocks()
    streak = AwakeStreak(MAC[0], clocks.read, MAC[1])
    clocks.skips += 60
    assert streak.awake_seconds() is None
    clocks.asleep(900)
    assert streak.seconds_until_automatic_launch() == 0


@pytest.mark.skipif(
    not sys.platform.startswith(("darwin", "linux")), reason="no sleep clock pair is named here"
)
def test_this_machine_resolves_its_named_pair() -> None:
    assert AwakeStreak().awake_seconds() is not None
