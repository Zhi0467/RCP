from __future__ import annotations

from pathlib import Path
from unittest import mock

from rcp.storage.experiments import ExperimentStoreMixin
from tests.lifecycles.engine import Action, explore, witness
from tests.lifecycles.episode import Episode

STATES = 36


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    return root


def test_episode_lifecycle_holds_within_bound(tmp_path: Path) -> None:
    """Every episode state within three actions keeps the Stop, wrap-up and watcher rules.

    The outage defect is a two-action trace (Stop, then a restart that cannot
    reach the stage host) and the Stop witness is two actions too. The goal
    looks two environment steps ahead from the wrap-up the exit reaches at
    depth 1, so a bound of 3 is the minimum; it runs in about 10 s locally.
    """

    result = explore(Episode(), _root(tmp_path), tmp_path / "saves")
    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []
    assert result.states == STATES


def test_stop_settles_once_its_failed_turn_is_no_longer_live(tmp_path: Path) -> None:
    """Without e87b06f58, a failed leaf reads as running and Stop never settles."""

    unresolved = ExperimentStoreMixin._experiment_stop_unresolved_tasks

    def failed_reads_running(connection, project_id, control_node_id, episode_id):
        rows = unresolved(connection, project_id, control_node_id, episode_id)
        return [
            {
                "operation_id": row["operation_id"],
                "status": "running" if row["status"] in {"failed", "paused"} else row["status"],
            }
            for row in rows
        ]

    with mock.patch.object(
        ExperimentStoreMixin,
        "_experiment_stop_unresolved_tasks",
        staticmethod(failed_reads_running),
    ):
        reported = witness(
            Episode(),
            _root(tmp_path),
            tmp_path / "saves",
            (Action("settle", ("R0", "failed")), Action("stop", environment=False)),
        )
    assert "stop_settles_once_no_leaf_is_live" in reported


def test_stop_waits_for_a_turn_an_outage_kept_from_recovery(tmp_path: Path) -> None:
    """Without the fix in de0b4bb03, Stop abandons the turn startup left for a later start."""

    rows = ExperimentStoreMixin._stopping_experiment_recovery_rows

    def without_pending(connection, episode_id=None):
        return [] if episode_id is not None else rows(connection)

    with mock.patch.object(
        ExperimentStoreMixin,
        "_stopping_experiment_recovery_rows",
        staticmethod(without_pending),
    ):
        reported = witness(
            Episode(),
            _root(tmp_path),
            tmp_path / "saves",
            (Action("stop", environment=False), Action("outage")),
        )
    assert "stop_keeps_a_pending_recovery" in reported
