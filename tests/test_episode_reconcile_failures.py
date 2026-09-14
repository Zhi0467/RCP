from __future__ import annotations

import json
import logging
import sqlite3
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from rcp.runs.auto_research import AutoResearchEndingSignal, auto_research_wrapup_spec
from rcp.runs.episodes import reconcile
from rcp.runs.episodes.wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup
from rcp.storage import EpisodeNotRunning, EpisodeReportConflict

from .test_auto_research_wrapup import _episode


def _ending(tmp_path, monkeypatch):
    store, episode, root = _episode(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="session", stage_root=str(tmp_path / "stage")
    )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    store.fence_episode_ending(episode.episode_id, "exhausted")
    signal = AutoResearchEndingSignal(
        episode_id=episode.episode_id, ending="exhausted", partial=True
    )
    launch = Mock()
    monkeypatch.setattr(reconcile, "start_episode_report", launch)
    owner = reconcile.EpisodeReconciler(store, Mock(), logger=logging.getLogger(__name__))
    return store, signal, owner, launch


def _validation_error():
    try:
        EpisodeWrapupSpec.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError("invalid spec accepted")


@pytest.mark.parametrize(
    "defect",
    [
        ValueError("bad ledger"),
        KeyError("missing binding"),
        _validation_error(),
        EpisodeReportConflict("bad fence"),
        EpisodeNotRunning("not running"),
    ],
    ids=["value", "key", "validation", "conflict", "not-running"],
)
@pytest.mark.parametrize("phase", ["spec", "admission"])
def test_permanent_admission_defects_settle_once(tmp_path, monkeypatch, caplog, defect, phase):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    spec = auto_research_wrapup_spec(store, signal)
    failed = Mock(side_effect=defect)
    monkeypatch.setattr(
        reconcile,
        "auto_research_wrapup_spec" if phase == "spec" else "begin_episode_report_wrapup",
        failed,
    )
    with caplog.at_level(logging.WARNING):
        assert not owner.reconcile_auto_research_wrapup(signal, source="test", operation_id="root")
        settled = store.episode(signal.episode_id)
        wrapup = store.episode_wrapup(signal.episode_id)
        owner.reconcile_auto_research_episode(signal.episode_id, source="poll")
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert settled.status == "needs_action"
    assert settled.wrapup_state == "failed"
    assert settled.wrapup_error == str(defect)
    assert store.episode(signal.episode_id) == settled
    assert store.episode_wrapup(signal.episode_id) == wrapup
    receipt = json.loads(wrapup.receipt_json)
    if phase == "spec":
        assert receipt["admission_error"] == type(defect).__name__
        assert wrapup.concluding_operation_id is None
    else:
        assert all(receipt[key] == value for key, value in spec.receipt.items())
        assert wrapup.concluding_operation_id == spec.continuation_operation_id
    assert len(caplog.records) == 1
    failed.assert_called_once()
    launch.assert_not_called()


def test_transient_store_failure_retries_real_admission(tmp_path, monkeypatch, caplog):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    before = store.episode(signal.episode_id)
    real = store.episode_wrapup
    with monkeypatch.context() as patch:
        patch.setattr(store, "episode_wrapup", Mock(side_effect=sqlite3.OperationalError("locked")))
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                assert not owner.reconcile_auto_research_wrapup(
                    signal, source="poll", operation_id="root"
                )
    assert store.episode(signal.episode_id) == before
    assert before.status == "wrapping_up"
    assert real(signal.episode_id) is None
    assert len(caplog.records) == 1
    assert any(
        r.category == "episode_report_reconciliation_failed"
        for r in store.agent_task_receipts("root")
    )
    assert owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode_wrapup(signal.episode_id).state == "pending"
    launch.assert_called_once()


def test_existing_admission_never_rebuilds_the_receipt(tmp_path, monkeypatch):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    admitted = begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
    builder = Mock(side_effect=AssertionError("must reuse immutable admission"))
    monkeypatch.setattr(reconcile, "auto_research_wrapup_spec", builder)
    assert owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode_wrapup(signal.episode_id) == admitted.wrapup
    builder.assert_not_called()
    launch.assert_called_once()


def test_settled_episode_is_left_alone(tmp_path, monkeypatch):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    store.end_episode_without_report(signal.episode_id, ending="exhausted")
    before = store.episode(signal.episode_id)
    builder = Mock(side_effect=AssertionError("settled"))
    monkeypatch.setattr(reconcile, "auto_research_wrapup_spec", builder)
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode(signal.episode_id) == before
    builder.assert_not_called()
    launch.assert_not_called()


def test_warning_is_once_per_episode_and_exception_type(tmp_path, monkeypatch, caplog):
    store, signal, owner, _ = _ending(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING):
        for error in [
            OSError("offline"),
            OSError("still offline"),
            TimeoutError("timeout"),
            TimeoutError("again"),
        ]:
            monkeypatch.setattr(store, "episode_wrapup", Mock(side_effect=error))
            assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
        owner._record_report_failure("another-episode", "poll", None, OSError("offline"))
    assert len(caplog.records) == 3


def test_stop_fence_wins_over_failed_admission(tmp_path, monkeypatch):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)

    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status = 'running', ending = NULL WHERE episode_id = ?",
            (signal.episode_id,),
        )

    def fence_then_fail(*args):
        store.request_episode_stop(signal.episode_id)
        raise ValueError("bad admission after Stop")

    monkeypatch.setattr(reconcile, "auto_research_wrapup_spec", fence_then_fail)
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode_wrapup(signal.episode_id) is None
    assert store.episode(signal.episode_id).stop_requested_at is not None
    owner.reconcile_auto_research_episode(signal.episode_id, source="poll")
    assert store.episode(signal.episode_id).ending == "stopped"
    launch.assert_not_called()


def test_launch_exception_is_not_a_permanent_admission_defect(tmp_path, monkeypatch):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    launch.side_effect = ValueError("launch unavailable")
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode(signal.episode_id).status == "wrapping_up"
    assert store.episode_wrapup(signal.episode_id).state == "pending"
    launch.side_effect = None
    assert owner.reconcile_auto_research_wrapup(signal, source="poll")


class _UnclassifiedDefect(Exception):
    """A programming error nobody classified as permanent or transient."""


def test_unclassified_defect_is_retried_three_times_then_settled(tmp_path, monkeypatch):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    monkeypatch.setattr(
        reconcile, "auto_research_wrapup_spec", Mock(side_effect=_UnclassifiedDefect("bug"))
    )
    for _ in range(2):
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
        assert store.episode(signal.episode_id).status == "wrapping_up"
        assert store.episode_wrapup(signal.episode_id) is None
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    settled = store.episode(signal.episode_id)
    assert settled.status == "needs_action"
    assert settled.wrapup_state == "failed"
    assert settled.wrapup_error == "bug"
    launch.assert_not_called()


def test_transient_errors_are_never_settled_by_repetition(tmp_path, monkeypatch):
    store, signal, owner, _ = _ending(tmp_path, monkeypatch)
    monkeypatch.setattr(
        store, "episode_wrapup", Mock(side_effect=sqlite3.OperationalError("locked"))
    )
    for _ in range(5):
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode(signal.episode_id).status == "wrapping_up"
