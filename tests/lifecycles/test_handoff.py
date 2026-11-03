from __future__ import annotations

import faulthandler
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from tests.lifecycles.engine import RESTART, Action, explore, witness
from tests.lifecycles.handoff import Handoff

STATES = 144
HANG_SECONDS = 60
LOST = (Action("settle", ("T0", "transport_lost")), Action("finish", ("T0",)))


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


def test_handoff_lifecycle_holds_within_bound(tmp_path: Path, hang_guard) -> None:
    """Every handoff state within four actions keeps agreement, safety and liveness.

    Liveness looks three environment steps ahead, so a bound of 4 checks it from
    the first two levels. The stood-down reattempt of 3ef7773ec is a Safety
    trace of four actions: the delivered turn loses its link, its worker arms a
    retry, its attachment expires, and the timer stands down. This bound runs
    in about 21 s locally, a little over the 20 s budget; the only cut that fits
    is dropping the member Retry, which the goal reads. A bound of 5 does not
    fit.
    """

    result = explore(Handoff(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.states == STATES


def test_a_settled_handoff_waits_for_a_decided_retry(tmp_path: Path) -> None:
    """Without the #368 race fix, the handoff settles before the worker arms its retry."""

    with mock.patch("rcp.background.BackgroundAgentTasks.retry_undecided", lambda *_: False):
        reported = witness(Handoff(), _root(tmp_path), tmp_path / "saves", LOST)
    assert "pending_retry_blocks_settlement" in reported


def test_a_restart_rearms_an_owed_retry(tmp_path: Path) -> None:
    """Without the 3cb576199 re-arm, a restart leaves the lost turn final and the handoff open."""

    with mock.patch(
        "rcp.background.BackgroundAgentTasks._rearm_owed_transport_retries", lambda _: None
    ):
        reported = witness(Handoff(), _root(tmp_path), tmp_path / "saves", (*LOST, Action(RESTART)))
    assert "final_turn_settles_handoff" in reported
