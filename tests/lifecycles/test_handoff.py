from __future__ import annotations

import faulthandler
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.lifecycles.engine import explore
from tests.lifecycles.handoff import Handoff

STATES = 106
HANG_SECONDS = 60


@pytest.fixture
def hang_guard() -> Iterator[None]:
    """End the run if the exploration hangs, so a stuck action fails its shard."""

    faulthandler.dump_traceback_later(HANG_SECONDS, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def test_handoff_lifecycle_holds_within_bound(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, hang_guard
) -> None:
    """Every handoff state within four actions keeps agreement, safety and liveness.

    The bound is set by #368's defects with their fixes reverted. Without
    ``retry_undecided``, the retry race is reported at depth 2: the delivered
    turn loses its link, a dispatch settles the handoff before the worker
    decides its retry, and the worker then arms one. Liveness looks three
    environment steps ahead, so a bound of 4 checks it from the first two
    levels, which include the race's first half. A bound of 5 has 290 states
    and exceeds the budget.
    """

    root = tmp_path / "tree"
    root.mkdir()
    result = explore(Handoff(), root, tmp_path_factory.mktemp("saves"))
    assert result.failures == []
    assert result.stuck() == []
    assert result.states == STATES
