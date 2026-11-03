from __future__ import annotations

import faulthandler
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.lifecycles.engine import explore
from tests.lifecycles.schedules import Schedules

STATES = 104
HANG_SECONDS = 60


@pytest.fixture
def hang_guard() -> Iterator[None]:
    """End the run if the exploration hangs, so a stuck action fails its shard."""

    faulthandler.dump_traceback_later(HANG_SECONDS, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_schedule_lifecycle_holds_within_bound(tmp_path: Path, hang_guard) -> None:
    """Every schedule state within three actions keeps the private-schedule rules.

    The machine has no Goal: the spec's progress rule, that each tick claims a
    waiting note, keeps it for an active turn, or records why, is the Step rule
    ``waiting_note_says_why``. Three actions reach every trace this PR fixed:
    ``tick 15`` (missed fires past --count), ``close`` and ``fence, tick 1,
    close`` (a disabled row keeping its next fire and note), and ``fence,
    tick 1`` (a claim refusal with no reason). Each fix has a plain regression
    test in ``tests/test_agent_schedules.py``; no seam reverts them, so the
    machine has no witness. Restore leaving a paused schedule paused is the
    spec's rule, not a defect: a paused schedule holds no note and fires only
    after the member's Resume.
    """

    result = explore(Schedules(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
    assert result.states == STATES
