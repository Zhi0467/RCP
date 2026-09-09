from __future__ import annotations

import hashlib
import logging
import uuid

import pytest

from rcp.api.episodes import serialize_episode
from rcp.background import BackgroundAgentTasks
from rcp.runs.auto_research import (
    auto_research_exhaustion_signal,
    auto_research_wrapup_spec,
    request_auto_research_stop,
    settle_auto_research_stop,
)
from rcp.runs.episodes.reconcile import EpisodeReconciler
from rcp.runs.episodes.report import restart_interrupted_episode_reports
from rcp.runs.episodes.wrapup import begin_episode_report_wrapup
from rcp.storage import WatcherContinuation

from .test_auto_research_children_storage import _experiment_route, _experiment_task
from .test_auto_research_wrapup import _episode
from .test_watchers import _record


def _parent_with_child(tmp_path):
    store, parent, root = _episode(tmp_path)
    child = _experiment_task(
        store,
        str(uuid.uuid4()),
        parent.authorized_by,
        node_id="exp/child",
        session_id="child-session",
        stage_root=str(tmp_path),
    )
    store.create_experiment_episode_with_invocation(
        child, auto_research_route=_experiment_route(store, parent, root, child)
    )
    store.record_agent_task_contract(
        child.operation_id,
        "experiment_episode_context_candidate",
        "{}",
        hashlib.sha256(b"{}").hexdigest(),
    )
    store.complete_agent_task(root.operation_id, applied_revision=None, result={})
    return store, parent, root, child


def _watcher(child, watcher_id, *, status="completed"):
    return _record(watcher_id, origin=child.operation_id, status=status).model_copy(
        update={
            "episode_id": child.episode_id,
            "graph_target": child.graph_target,
            "node_id": child.request["node_id"],
            "chat_id": child.request["chat_id"],
            "continuation": WatcherContinuation.model_validate(
                {k: v for k, v in child.request.items() if k in WatcherContinuation.model_fields}
            ),
        }
    )


def test_parent_stop_fences_existing_and_late_child_watchers_without_canceling_turn(tmp_path):
    store, parent, _root, child = _parent_with_child(tmp_path)
    store.create_watchers(
        [_watcher(child, "completed"), _watcher(child, "active", status="active")]
    )

    stopping = request_auto_research_stop(store, parent.episode_id)
    assert stopping.status == "stopping"
    assert store.episode(child.episode_id).stop_requested_at == stopping.stop_requested_at
    assert settle_auto_research_stop(store, parent.episode_id) is None
    assert store.agent_task(child.operation_id).status == "queued"

    store.create_watchers([_watcher(child, "late", status="active")])
    for watcher_id in ("completed", "active", "late"):
        watcher = store.watcher(watcher_id)
        assert watcher.status == "stopped" and watcher.notified
        assert watcher.notification_operation_id is None
    assert request_auto_research_stop(store, parent.episode_id) == stopping

    store.complete_agent_task(child.operation_id, applied_revision=7, result={"retained": True})
    store.settle_ready_experiment_loop_stops()
    assert store.episode(child.episode_id).ending == "stopped"
    assert settle_auto_research_stop(store, parent.episode_id).ending == "stopped"
    assert store.agent_task(child.operation_id).applied_revision == 7


@pytest.mark.parametrize(
    "task_state", ["queued", "running", "pausing", "paused", "failed", "interrupted"]
)
@pytest.mark.parametrize("fence", ["stop", "exhausted"])
def test_parent_waits_for_child_turn_and_its_exact_recovery(tmp_path, task_state, fence):
    store, parent, _root, child = _parent_with_child(tmp_path)
    if task_state == "running":
        store.mark_agent_task_running(child.operation_id)
    elif task_state == "pausing":
        store.request_agent_task_pause(child.operation_id)
    elif task_state == "paused":
        store.pause_agent_task(child.operation_id, detail="Saved checkpoint.")
    elif task_state == "failed":
        store.fail_agent_task(child.operation_id, "Recoverable interruption.")
    elif task_state == "interrupted":
        store.interrupt_active_agent_tasks()
    if fence == "stop":
        request_auto_research_stop(store, parent.episode_id)
    else:
        auto_research_exhaustion_signal(store, parent.episode_id)
    assert not store.auto_research_is_quiescent(parent.episode_id)
    if fence == "stop":
        assert settle_auto_research_stop(store, parent.episode_id) is None
    if task_state in {"paused", "failed", "interrupted"}:
        projection = serialize_episode(store, parent.project_id, store.episode(child.episode_id))
        assert projection.task_control in {"resume", "retry"}
        recovery = _experiment_task(
            store,
            child.episode_id,
            parent.authorized_by,
            node_id="exp/child",
            parent_operation_id=child.operation_id,
            attempt=2,
            session_id="child-session",
            stage_root=str(tmp_path),
        )
        store.create_experiment_recovery_task(recovery)
        assert not store.auto_research_is_quiescent(parent.episode_id)
        child = recovery
    store.complete_agent_task(child.operation_id, applied_revision=None, result={})
    assert store.auto_research_is_quiescent(parent.episode_id)
    assert store.auto_research_experiment_allowance(parent.episode_id).used == 1


def test_parent_report_waits_for_child_and_then_enters_wrapup(tmp_path):
    store, parent, root, child = _parent_with_child(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="root-session", stage_root=str(tmp_path)
    )
    signal = auto_research_exhaustion_signal(store, parent.episode_id)

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("The regression must not invoke a provider.")
        yield

    tasks = BackgroundAgentTasks(store, forbidden_stream)
    reconciler = EpisodeReconciler(store, tasks, logger=logging.getLogger(__name__))
    assert not reconciler.reconcile_auto_research_wrapup(signal, source="test")
    assert store.episode_wrapup(parent.episode_id) is None
    # The same guard protects restart of an already-admitted report.
    assert reconciler._has_unsettled_visible_episode_task(parent.episode_id)

    store.complete_agent_task(child.operation_id, applied_revision=None, result={})
    admission = begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
    assert admission.launchable
    assert not reconciler._has_unsettled_visible_episode_task(parent.episode_id)
    assert not store.auto_research_report_has_later_child_work(parent.episode_id)


def test_report_snapshot_detects_child_ending_without_another_turn(tmp_path):
    store, parent, root, child = _parent_with_child(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="root-session", stage_root=str(tmp_path)
    )
    store.complete_agent_task(child.operation_id, applied_revision=None, result={})
    signal = auto_research_exhaustion_signal(store, parent.episode_id)
    begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
    assert not store.auto_research_report_has_later_child_work(parent.episode_id)

    store.request_episode_stop(child.episode_id)
    store.settle_ready_experiment_loop_stops()

    assert store.episode(child.episode_id).ending == "stopped"
    assert store.auto_research_report_has_later_child_work(parent.episode_id)


@pytest.mark.parametrize(
    ("entrypoint", "report_state", "child_completion"),
    [
        ("poll", "queued", "recovery"),
        ("restart", "queued", "recovery"),
        ("restart", "interrupted", "recovery"),
        ("restart", "paused", "recovery"),
        ("poll", "queued", "same_turn"),
    ],
)
@pytest.mark.parametrize("poll_before_recovery", [False, True])
def test_stale_report_snapshot_fails_visibly_after_child_recovery(
    tmp_path, monkeypatch, entrypoint, report_state, child_completion, poll_before_recovery
):
    store, parent, root, child = _parent_with_child(tmp_path)
    store.checkpoint_agent_task(
        root.operation_id, native_session_id="root-session", stage_root=str(tmp_path)
    )
    if child_completion == "recovery":
        store.fail_agent_task(child.operation_id, "Recoverable interruption.")
    signal = auto_research_exhaustion_signal(store, parent.episode_id)
    # Reproduce the durable report allocation made by older versions before the
    # child settled. Both startup and the ordinary poll must wait on recovery.
    admission = begin_episode_report_wrapup(store, auto_research_wrapup_spec(store, signal))
    assert admission.launchable
    if report_state != "queued":
        store.mark_agent_task_running(admission.task.operation_id)
        attempt = store.allocate_episode_report_attempt(parent.episode_id)
        store.mark_episode_report_attempt_running(attempt.attempt_id)
        if report_state == "paused":
            store.pause_agent_task(admission.task.operation_id, detail="Shutdown checkpoint.")
        else:
            store.interrupt_active_agent_tasks()

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("The regression must not invoke a provider.")
        yield

    tasks = BackgroundAgentTasks(store, forbidden_stream)
    launches = []
    monkeypatch.setattr(
        tasks, "launch_admitted", lambda operation_id: launches.append(operation_id)
    )
    reconciler = EpisodeReconciler(store, tasks, logger=logging.getLogger(__name__))

    def reconcile():
        if entrypoint == "restart":
            restart_interrupted_episode_reports(tasks)
        else:
            reconciler.reconcile_auto_research_episode(parent.episode_id, source="test")

    if poll_before_recovery:
        reconcile()
        assert not launches
    if child_completion == "recovery":
        child = _experiment_task(
            store,
            child.episode_id,
            parent.authorized_by,
            node_id="exp/child",
            parent_operation_id=child.operation_id,
            attempt=2,
            session_id="child-session",
            stage_root=str(tmp_path),
        )
        store.create_experiment_recovery_task(child)
        if poll_before_recovery:
            reconcile()
            assert not launches
    store.complete_agent_task(child.operation_id, applied_revision=None, result={})
    reconcile()
    assert not launches
    ended = store.episode(parent.episode_id)
    assert ended.status == "needs_action" and ended.ending == "exhausted"
    assert ended.wrapup_state == "failed"
    assert "saved summary was captured before child Experiment work finished" in ended.wrapup_error
    retained = store.episode_wrapup(parent.episode_id)
    assert retained.receipt_json == admission.wrapup.receipt_json
    assert retained.receipt_sha256 == admission.wrapup.receipt_sha256
    assert retained.allocation_operation_id == admission.task.operation_id
    assert store.agent_task(admission.task.operation_id).status == "failed"
    assert store.episode_report(parent.episode_id) is None
    assert ended.report_attempts_used == (0 if report_state == "queued" else 1)
    assert all(
        attempt.status == "failed" for attempt in store.episode_report_attempts(parent.episode_id)
    )
    assert store.auto_research_experiment_allowance(parent.episode_id).used == 1
    assert serialize_episode(
        store, parent.project_id, ended, include_graph_branch=False
    ).can_reauthorize
    reconcile()
    assert not launches
