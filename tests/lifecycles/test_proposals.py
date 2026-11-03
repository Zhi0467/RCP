from __future__ import annotations

from pathlib import Path
from unittest import mock

from tests.lifecycles.engine import RESTART, Action, explore, witness
from tests.lifecycles.proposals import Proposals

STATES = 30


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_proposal_lifecycle_holds_within_bound(tmp_path: Path) -> None:
    """Every Proposal and approval card state within two actions keeps its rules.

    Every way a card ends is one environment step (a sweep, or the restart after
    a crashed tap), so a look-ahead of one checks the goal from every state at
    depth one, and the witness traces take two actions. Each step boots a fresh
    app over the tree, so a bound of 3 takes about 30 s, over the 20 s budget.
    """

    result = explore(Proposals(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
    assert result.states == STATES


def test_a_restart_settles_a_committed_tap_as_approved(tmp_path: Path) -> None:
    """Without the graph receipt of 5f8c37898, a restart fails a card whose tap already approved."""

    with mock.patch("rcp.runs.approval_cards.committed_receipt", lambda *_: None):
        reported = witness(
            Proposals(),
            _root(tmp_path),
            tmp_path / "saves",
            (Action("tap", ("C0", "stop-after-commit"), environment=False), Action(RESTART)),
        )
    assert "restart_never_reruns" in reported


def test_an_outage_tap_releases_its_card(tmp_path: Path) -> None:
    """Without the release of 5f8c37898, an outage tap leaves its card claimed for good."""

    with mock.patch("rcp.storage.AppStore.release_approval_card", lambda *_: None):
        reported = witness(
            Proposals(),
            _root(tmp_path),
            tmp_path / "saves",
            (Action("tap", ("C0", "outage"), environment=False),),
        )
    assert "outage_releases_the_card" in reported
