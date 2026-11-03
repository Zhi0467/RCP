from __future__ import annotations

import faulthandler
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.lifecycles.consolidation import (
    Consolidation,
    due_target_advances,
    queued_task_launches,
)
from tests.lifecycles.engine import Exploration, explore

STATES = 44
HANG_SECONDS = 60


@pytest.fixture
def hang_guard() -> Iterator[None]:
    """End the run if the exploration hangs, so a stuck action fails its shard."""

    faulthandler.dump_traceback_later(HANG_SECONDS, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def _explore(tmp_path: Path, seed: str | None = None) -> Exploration:
    root = tmp_path / "tree"
    root.mkdir()
    return explore(Consolidation(seed), root, tmp_path / "saves")


def _start(result: Exploration):
    return next(view for view, depth in result.depth.items() if depth == 0)


def test_consolidation_lifecycle_holds_within_bound(tmp_path: Path, hang_guard) -> None:
    """Every consolidation state within three actions keeps safety and liveness.

    Liveness looks two environment steps ahead, so a bound of 3 checks it from
    the start, a claimed night, a launch left queued and a member turn holding
    the chat.
    """

    result = _explore(tmp_path)
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
    assert result.states == STATES


def test_a_run_0_4_35_queued_with_its_target_launches(tmp_path: Path, hang_guard) -> None:
    """A run queued by 0.4.35 with ``graph_target`` in its request launches (b12897489)."""

    result = _explore(tmp_path, "request_target")
    start = _start(result)
    assert start.request_target and not queued_task_launches(start)
    assert result.failures == []
    assert result.stuck() == []


def test_a_legacy_archived_chat_keeps_consolidating(tmp_path: Path, hang_guard) -> None:
    """A consolidation chat an older release archived still takes its night (09f7519f6)."""

    result = _explore(tmp_path, "archived_chat")
    start = _start(result)
    assert start.archived and not due_target_advances(start)
    assert result.failures == []
    assert result.stuck() == []
