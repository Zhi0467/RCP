from __future__ import annotations

from pathlib import Path

from tests.lifecycles.engine import explore
from tests.lifecycles.schedules import Schedules

STATES = 186


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_schedule_lifecycle_holds_within_bound(tmp_path: Path) -> None:
    """Every schedule state within three actions keeps the private-schedule rules.

    The machine has no Goal: the spec's progress rule, that each tick claims a
    waiting note, keeps it for an active turn, or records why, is the Step rule
    ``waiting_note_says_why``. Three actions also cross keyed Pause/Resume,
    a newer opposite member control, and receipt replay; delayed Add and
    restore replay preserve their recorded results and the current schedule.
    The machine has no defect seam. Restore leaving a paused schedule paused is the
    spec's rule, not a defect: a paused schedule holds no note and fires only
    after the member's Resume.
    """

    result = explore(Schedules(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
    assert result.states == STATES
