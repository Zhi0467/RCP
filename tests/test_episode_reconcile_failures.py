from __future__ import annotations

import errno
import json
import logging
import sqlite3
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from rcp.runs.auto_research import AutoResearchEndingSignal, auto_research_wrapup_spec
from rcp.runs.episodes import reconcile
from rcp.runs.episodes.reconcile import _UNCLASSIFIED_ADMISSION_RETRIES as _UNCLASSIFIED_RETRIES
from rcp.runs.episodes.wrapup import (
    EpisodeReportAdmissionInvalid,
    EpisodeWrapupSpec,
    begin_episode_report_wrapup,
)
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
        EpisodeReportAdmissionInvalid("bad ledger"),
        _validation_error(),
        EpisodeReportConflict("bad fence"),
        EpisodeNotRunning("not running"),
    ],
    ids=["invalid", "validation", "conflict", "not-running"],
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


@pytest.mark.parametrize("defect", [_UnclassifiedDefect("bug"), ValueError("bug"), KeyError("bug")])
def test_unclassified_defect_retries_are_counted_per_process(tmp_path, monkeypatch, defect):
    """Three tries per process, as the decision promises; a restart starts over."""

    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    monkeypatch.setattr(reconcile, "auto_research_wrapup_spec", Mock(side_effect=defect))
    for _ in range(2):
        # A fresh reconciler is a restarted process: the durable receipts it
        # rereads from the prior process do not count against this one.
        restarted = reconcile.EpisodeReconciler(store, Mock(), logger=logging.getLogger(__name__))
        for _ in range(_UNCLASSIFIED_RETRIES - 1):
            assert not restarted.reconcile_auto_research_wrapup(signal, source="poll")
            assert store.episode(signal.episode_id).status == "wrapping_up"
            assert store.episode_wrapup(signal.episode_id) is None
    for _ in range(_UNCLASSIFIED_RETRIES - 1):
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
        assert store.episode(signal.episode_id).status == "wrapping_up"
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    settled = store.episode(signal.episode_id)
    assert settled.status == "needs_action"
    assert settled.wrapup_state == "failed"
    assert settled.wrapup_error == str(defect)
    launch.assert_not_called()


@pytest.mark.parametrize(
    "transient",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is busy"),
        ConnectionResetError(errno.ECONNRESET, "peer reset"),
        OSError(errno.EHOSTUNREACH, "no route to host"),
        TimeoutError("ssh timed out"),
    ],
)
def test_transient_errors_are_never_settled_by_repetition(tmp_path, monkeypatch, transient):
    store, signal, owner, _ = _ending(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "episode_wrapup", Mock(side_effect=transient))
    for _ in range(5):
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode(signal.episode_id).status == "wrapping_up"


@pytest.mark.parametrize(
    "permanent",
    [
        sqlite3.OperationalError("attempt to write a readonly database"),
        sqlite3.OperationalError("no such table: episode_wrapups"),
        PermissionError(errno.EACCES, "stage is not writable"),
        FileNotFoundError(errno.ENOENT, "stage root is gone"),
    ],
)
def test_permanent_errors_sharing_a_transient_base_class_settle_after_repeats(
    tmp_path, monkeypatch, permanent
):
    """`OSError` and `OperationalError` also cover read-only, missing, and refused.

    Those never come back on their own, so they take the bounded path instead of
    keeping the episode in `wrapping_up` forever.
    """

    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "episode_wrapup", Mock(side_effect=permanent))
    for _ in range(2):
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll", operation_id="root")
        assert store.episode(signal.episode_id).status == "wrapping_up"
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll", operation_id="root")
    settled = store.episode(signal.episode_id)
    assert settled.status == "needs_action"
    assert settled.wrapup_state == "failed"
    assert settled.wrapup_error == str(permanent)
    launch.assert_not_called()


def test_repeated_launch_failures_settle_the_allocation_as_unlaunchable(tmp_path, monkeypatch):
    store, signal, owner, launch = _ending(tmp_path, monkeypatch)
    launch.side_effect = ValueError("the persisted report request never validates")
    for _ in range(2):
        assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
        assert store.episode(signal.episode_id).status == "wrapping_up"
        assert store.episode_wrapup(signal.episode_id).state == "pending"
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    settled = store.episode(signal.episode_id)
    assert settled.status == "needs_action"
    assert settled.wrapup_state == "failed"
    assert "never validates" in (settled.wrapup_error or "")
    assert store.episode_wrapup(signal.episode_id).state == "failed"
    launch.side_effect = None
    assert not owner.reconcile_auto_research_wrapup(signal, source="poll")
    assert store.episode(signal.episode_id) == settled


def _experiment_ending(tmp_path, monkeypatch):
    from rcp.storage import AppStore

    from .test_experiment_episode_storage import _admit_root, _bind

    store = AppStore(tmp_path / "experiment.sqlite3")
    episode_id, root = _admit_root(store)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="native-session", stage_root=str(tmp_path / "stage")
    )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    _bind(
        store,
        episode_id,
        root.operation_id,
        invocation=1,
        ending_signal={
            "episode_id": episode_id,
            "ending": "completed",
            "partial": False,
            "receipt": {"semantic_signals": ["experiment_completed"]},
        },
    )
    launch = Mock()
    monkeypatch.setattr(reconcile, "start_episode_report", launch)
    owner = reconcile.EpisodeReconciler(store, Mock(), logger=logging.getLogger(__name__))
    return store, episode_id, owner, launch


def test_experiment_permanent_admission_defect_settles_once(tmp_path, monkeypatch):
    store, episode_id, owner, launch = _experiment_ending(tmp_path, monkeypatch)
    monkeypatch.setattr(
        reconcile,
        "begin_episode_report_wrapup",
        Mock(side_effect=EpisodeReportAdmissionInvalid("bad ledger")),
    )
    owner.reconcile_experiment_episode(episode_id, source="poll")
    settled = store.episode(episode_id)
    # A completed ending settles to its own terminal status; the failed report is
    # published through the wrap-up state, never by reopening the episode.
    assert settled.status == "completed"
    assert settled.ending == "completed"
    assert settled.wrapup_state == "failed"
    assert settled.wrapup_error == "bad ledger"
    assert store.episode_wrapup(episode_id).state == "failed"
    launch.assert_not_called()
    owner.reconcile_experiment_episode(episode_id, source="poll")
    assert store.episode(episode_id) == settled


def test_experiment_transient_admission_failure_keeps_wrapping_up(tmp_path, monkeypatch):
    store, episode_id, owner, launch = _experiment_ending(tmp_path, monkeypatch)
    monkeypatch.setattr(
        store,
        "settle_experiment_episode_wrapup",
        Mock(side_effect=sqlite3.OperationalError("locked")),
    )
    for _ in range(5):
        owner.reconcile_experiment_episode(episode_id, source="poll")
    assert store.episode(episode_id).status == "wrapping_up"
    assert store.episode_wrapup(episode_id) is None
    launch.assert_not_called()


def test_experiment_repeated_launch_failures_settle_the_allocation(tmp_path, monkeypatch):
    store, episode_id, owner, launch = _experiment_ending(tmp_path, monkeypatch)
    launch.side_effect = ValueError("the persisted report request never validates")
    for _ in range(2):
        owner.reconcile_experiment_episode(episode_id, source="poll")
        assert store.episode(episode_id).status == "wrapping_up"
        assert store.episode_wrapup(episode_id).state == "pending"
    owner.reconcile_experiment_episode(episode_id, source="poll")
    settled = store.episode(episode_id)
    assert settled.status == "completed"
    assert settled.wrapup_state == "failed"
    assert "never validates" in (settled.wrapup_error or "")
    assert store.episode_wrapup(episode_id).state == "failed"


@pytest.mark.parametrize("mode", ["auto_research", "experiment_loop"])
def test_admission_and_launch_failures_have_separate_repeat_counters(
    tmp_path, monkeypatch, caplog, mode
):
    if mode == "auto_research":
        store, signal, owner, launch = _ending(tmp_path, monkeypatch)
        episode_id = signal.episode_id

        def poll():
            owner.reconcile_auto_research_wrapup(signal, source="poll")

    else:
        store, episode_id, owner, launch = _experiment_ending(tmp_path, monkeypatch)

        def poll():
            owner.reconcile_experiment_episode(episode_id, source="poll")

    with caplog.at_level(logging.WARNING):
        with monkeypatch.context() as patch:
            patch.setattr(
                reconcile,
                "begin_episode_report_wrapup",
                Mock(side_effect=_UnclassifiedDefect("admission defect")),
            )
            for _ in range(2):
                poll()
                assert store.episode_wrapup(episode_id) is None
        launch.side_effect = _UnclassifiedDefect("launch defect")
        for _ in range(2):
            poll()
            assert store.episode(episode_id).status == "wrapping_up"
            assert store.episode_wrapup(episode_id).state == "pending"
        poll()
    assert store.episode(episode_id).wrapup_state == "failed"
    assert store.episode(episode_id).wrapup_error == "launch defect"
    assert launch.call_count == 3
    assert len(caplog.records) == 1


def test_experiment_existing_admission_never_rebuilds_the_receipt(tmp_path, monkeypatch):
    store, episode_id, owner, launch = _experiment_ending(tmp_path, monkeypatch)
    owner.reconcile_experiment_episode(episode_id, source="poll")
    admitted = store.episode_wrapup(episode_id)
    assert admitted.state == "pending"
    launch.reset_mock()
    builder = Mock(side_effect=AssertionError("must reuse immutable admission"))
    admission = Mock(side_effect=AssertionError("must reuse durable allocation"))
    monkeypatch.setattr(reconcile, "experiment_loop_wrapup_spec", builder)
    monkeypatch.setattr(reconcile, "begin_episode_report_wrapup", admission)
    owner.reconcile_experiment_episode(episode_id, source="poll")
    assert store.episode_wrapup(episode_id) == admitted
    builder.assert_not_called()
    admission.assert_not_called()
    launch.assert_called_once()
