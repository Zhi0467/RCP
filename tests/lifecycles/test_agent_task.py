from __future__ import annotations

import faulthandler
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from tests.lifecycles.agent_task import AgentTask
from tests.lifecycles.engine import Action, explore, witness

STATES = 110
HANG_SECONDS = 60
RETRIED_FIRST = (
    Action("claim", ("A0",)),
    Action("settle", ("A0", "transport_lost")),
    Action("retry", ("A0",), environment=False),
    Action("finish", ("A0",)),
)


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


def test_agent_task_lifecycle_holds_within_bound(tmp_path: Path, hang_guard) -> None:
    """Every agent task state within four actions keeps its rules and liveness.

    The goal looks two environment steps ahead, so a bound of 4 checks it from
    the first two levels, where a claimed turn is paused. The longest trace a
    rule needs is the four-action Retry before a lost turn's settlement. This
    runs in about the time the handoff explorer takes on the same machine.
    """

    result = explore(AgentTask(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
    assert result.states == STATES


def test_a_pause_no_worker_observes_is_never_left_pausing(tmp_path: Path, hang_guard) -> None:
    """Without the unowned settle in Pause, a Pause of the unlaunched root stays pausing.

    The goal witness explores at bound 2 with a one-step horizon: the stuck
    state is one Pause from the start, and a smaller bound keeps the budget.
    """

    machine = AgentTask()
    machine.bound, machine.horizon = 2, 1
    with mock.patch("rcp.background.BackgroundAgentTasks._worker_owns", lambda *_: True):
        result = explore(machine, _root(tmp_path), tmp_path / "saves")
    assert any("pausing_settles" in message for _trace, message in result.stuck())


def test_a_retried_turn_arms_no_reattempt(tmp_path: Path) -> None:
    """Without the continuation check, settling a lost turn a human retried arms a timer."""

    with mock.patch("rcp.storage.AppStore.agent_task_has_continuation", lambda *_: False):
        reported = witness(AgentTask(), _root(tmp_path), tmp_path / "saves", RETRIED_FIRST)
    assert "timers_arm_only_for_untaken_turns" in reported
