from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from rcp.storage import AppStore, EpisodeInvocationCeilingReached, EpisodeNotRunning
from rcp.storage.models import GraphWatcherRecord, WatcherContinuation, WatcherRecord
from tests.test_auto_research_children_storage import (
    _auto_parent,
    _child_work_mail_wake,
    _project,
    _work_pair,
)


def _waiting_child(tmp_path, *, ceiling=5):
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    parent, root = _auto_parent(store, ceiling=ceiling)
    route, task = _work_pair(store, parent, root, worker_id="worker-watch")
    store.create_auto_research_child_work(route, task)
    store.checkpoint_agent_task(
        task.operation_id, native_session_id="saved-child-session", stage_root="/tmp/child-stage"
    )
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    current = store.agent_task(task.operation_id)
    assert current is not None
    watcher = WatcherRecord(
        watcher_id="child-observer",
        project_id=parent.project_id,
        origin_operation_id=task.operation_id,
        origin_task_kind="node_chat",
        episode_id=parent.episode_id,
        worker_id=route.worker_id,
        graph_target=parent.graph_target,
        chat_id=task.request["chat_id"],
        node_id=task.request["node_id"],
        continuation=WatcherContinuation.model_validate(
            {
                key: value
                for key, value in task.request.items()
                if key in WatcherContinuation.model_fields
            }
        ),
        check_command="true",
        log_path="/tmp/job.log",
        cwd="/tmp",
        status="completed",
        created_at=store.now(),
        completed_at=store.now(),
    )
    store.create_watchers([watcher])
    wake = _child_work_mail_wake(store, current)
    wake.request.update(message="Watcher completed; /tmp/job.log", watcher_ids=[watcher.watcher_id])
    return store, parent, route, watcher, wake


def test_child_watcher_wake_claims_once_on_saved_route_and_spends_one_b(tmp_path):
    store, parent, route, watcher, wake = _waiting_child(tmp_path)
    before = store.episode_budget_meter(parent.episode_id).invocations_used
    candidates = [wake, wake.model_copy(update={"operation_id": "another-wake"})]

    def admit(candidate):
        return store.create_auto_research_child_work_watcher_wake_task(
            candidate, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = [item for item in pool.map(admit, candidates) if item is not None]
    assert len(admitted) == 1
    result = admitted[0]
    assert result.native_session_id == "saved-child-session"
    assert result.stage_root == "/tmp/child-stage"
    assert result.graph_target == parent.graph_target
    assert (
        store.auto_research_child_work(route.worker_id).current_operation_id == result.operation_id
    )
    assert store.agent_task_continuation_cause(result.operation_id) == "watcher_wake"
    assert store.watcher(watcher.watcher_id).notification_operation_id == result.operation_id
    assert store.episode_budget_meter(parent.episode_id).invocations_used == before + 1
    assert store.auto_research_waiting_child_work_ids(parent.episode_id) == set()


def test_child_watcher_exhausted_budget_retains_visible_pending_completion(tmp_path):
    store, parent, route, watcher, wake = _waiting_child(tmp_path, ceiling=2)
    with pytest.raises(EpisodeInvocationCeilingReached):
        store.create_auto_research_child_work_watcher_wake_task(
            wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
        )
    assert store.agent_task(wake.operation_id) is None
    assert store.watcher(watcher.watcher_id).notified is False
    assert store.auto_research_waiting_child_work_ids(parent.episode_id) == {route.worker_id}
    assert store.episode_budget_meter(parent.episode_id).invocations_used == 2


@pytest.mark.parametrize("stop_parent", [False, True])
def test_child_stop_fence_retires_watchers_and_refuses_late_arming_and_wake(tmp_path, stop_parent):
    store, parent, route, watcher, wake = _waiting_child(tmp_path)
    if stop_parent:
        store.request_auto_research_stop_and_settle_watchers(parent.episode_id)
    else:
        store.request_auto_research_child_work_stop(route.worker_id)
    stopped = store.watcher(watcher.watcher_id)
    assert stopped.status == "stopped" and stopped.notified
    late = store.create_watchers([watcher.model_copy(update={"watcher_id": "late-watcher"})])[0]
    assert late.status == "stopped" and late.notified
    with pytest.raises(EpisodeNotRunning):
        store.create_auto_research_child_work_watcher_wake_task(
            wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
        )
    assert store.agent_task(wake.operation_id) is None


@pytest.mark.parametrize("status", ["active", "degraded", "completed"])
def test_waiting_work_blocks_guarded_finish_until_child_stop(tmp_path, status):
    store, parent, route, watcher, _wake = _waiting_child(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET status = ? WHERE watcher_id = ?", (status, watcher.watcher_id)
        )
    blockers = store.auto_research_finish_blockers(parent.episode_id)
    waiting = [item for item in blockers if item.kind == "waiting_work"]
    assert [(item.blocker_id, item.state, item.action) for item in waiting] == [
        (route.worker_id, "waiting", f"stop --key <key> {route.worker_id}")
    ]
    receipt = store.guard_auto_research_finish(
        parent.episode_id, effect_id="guard", actor_operation_id=route.admitted_by_operation_id
    )
    assert receipt.disposition == "blocked"
    assert any(item["kind"] == "waiting_work" for item in receipt.result["blockers"])
    store.request_auto_research_child_work_stop(route.worker_id)
    assert not any(
        item.kind == "waiting_work"
        for item in store.auto_research_finish_blockers(parent.episode_id)
    )


@pytest.mark.parametrize("change", ["running", "session", "scope"])
def test_child_watcher_wake_refuses_busy_route_or_changed_binding_without_claim(tmp_path, change):
    store, parent, route, watcher, wake = _waiting_child(tmp_path)
    if change == "running":
        with store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET status = 'running' WHERE operation_id = ?",
                (route.current_operation_id,),
            )
        assert (
            store.create_auto_research_child_work_watcher_wake_task(
                wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
            )
            is None
        )
    else:
        if change == "session":
            wake.native_session_id = "different-session"
        else:
            wake.request["run_truth_scope"] = ["different-repo"]
        with pytest.raises(ValueError):
            store.create_auto_research_child_work_watcher_wake_task(
                wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
            )
    assert store.agent_task(wake.operation_id) is None
    assert store.watcher(watcher.watcher_id).notified is False
    assert store.episode_budget_meter(parent.episode_id).invocations_used == 2


def test_old_database_with_child_routes_backfills_watcher_worker_and_episode(tmp_path):
    store, parent, route, watcher, _wake = _waiting_child(tmp_path)
    with store.connection() as connection:
        connection.execute("UPDATE watchers SET episode_id = NULL")
        connection.execute("ALTER TABLE watchers DROP COLUMN worker_id")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 11")
    upgraded = AppStore(tmp_path / "rcp.sqlite3")
    assert upgraded.auto_research_child_work(route.worker_id) == route
    assert upgraded.watcher(watcher.watcher_id).worker_id == route.worker_id
    assert upgraded.watcher(watcher.watcher_id).episode_id == parent.episode_id
    assert upgraded.auto_research_waiting_child_work_ids(parent.episode_id) == {route.worker_id}
    assert upgraded.episode_budget_meter(parent.episode_id).invocations_used == 2


def test_child_watcher_cannot_use_unmetered_generic_wake(tmp_path):
    store, parent, _route, watcher, wake = _waiting_child(tmp_path)
    with pytest.raises(ValueError, match="paid wake"):
        store.create_watcher_notification_task(wake, [watcher.watcher_id])
    assert store.agent_task(wake.operation_id) is None
    assert store.watcher(watcher.watcher_id).notified is False
    assert store.episode_budget_meter(parent.episode_id).invocations_used == 2


@pytest.mark.parametrize("status", ["active", "degraded", "completed"])
def test_exhausted_ending_retains_child_completion_and_late_arming(tmp_path, status):
    store, parent, route, watcher, wake = _waiting_child(tmp_path, ceiling=2)
    with store.connection() as connection:
        connection.execute(
            "UPDATE watchers SET status = ? WHERE watcher_id = ?", (status, watcher.watcher_id)
        )
    root_watcher = GraphWatcherRecord(
        watcher_id="root-watcher",
        project_id=parent.project_id,
        origin_operation_id=route.admitted_by_operation_id,
        origin_task_kind="auto_research",
        chat_id="root-chat",
        episode_id=parent.episode_id,
        graph_target=parent.graph_target,
        continuation=watcher.continuation,
        condition={"node_id": "exp/seat", "status_in": ["done"]},
        armed_revision=0,
        created_at=store.now(),
    )
    store.create_watchers([root_watcher])

    ended = store.fence_auto_research_ending_and_settle_watchers(parent.episode_id, "exhausted")
    assert ended.ending == "exhausted"
    retained = store.watcher(watcher.watcher_id)
    assert retained.status == status and retained.notified is False
    assert store.watcher(root_watcher.watcher_id).status == "stopped"
    late = store.create_watchers([watcher.model_copy(update={"watcher_id": "late-child"})])[0]
    assert late.status == "completed" and late.notified is False
    store.record_watcher_check(watcher.watcher_id, status="completed", exit_code=0, error=None)
    with pytest.raises(EpisodeNotRunning):
        store.create_auto_research_child_work_watcher_wake_task(
            wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
        )
    assert store.agent_task(wake.operation_id) is None
    assert store.watcher(watcher.watcher_id).status == "completed"
    assert store.watcher(watcher.watcher_id).notified is False
    assert store.auto_research_waiting_child_work_ids(parent.episode_id) == {route.worker_id}
    store.request_auto_research_child_work_stop(route.worker_id)
    assert store.watcher(watcher.watcher_id).status == "stopped"
    assert (
        store.create_watchers([watcher.model_copy(update={"watcher_id": "after-child-stop"})])[
            0
        ].status
        == "stopped"
    )
