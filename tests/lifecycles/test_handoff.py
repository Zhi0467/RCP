from __future__ import annotations

from pathlib import Path
from unittest import mock

from tests.lifecycles.engine import RESTART, Action, explore, witness
from tests.lifecycles.handoff import Handoff

STATES = 159
LOST = (
    Action("run", ("T0",)),
    Action("settle", ("T0", "transport_lost")),
    Action("finish", ("T0",)),
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_handoff_lifecycle_holds_within_bound(tmp_path: Path) -> None:
    """Every handoff state within five actions keeps agreement, safety and liveness.

    Liveness looks three environment steps ahead, so a bound of 5 checks it from
    the first three levels. The stood-down reattempt of 3ef7773ec is a Safety
    trace of five actions: the delivered turn's worker claims it, the turn
    loses its link, its worker arms a retry, its attachment expires, and the
    timer stands down. A restart between the claim and the verdict, or between
    dispatch and the claim, interrupts the turn instead of relaunching it. This
    bound runs in about 20 s locally; a bound of 4 no longer reaches the
    3ef7773ec trace.
    """

    result = explore(Handoff(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
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
