from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from rcp.storage import AppStore, EpisodeNotRunning
from rcp.storage.models import WatcherCheckRecord, WatcherRecord
from tests.test_auto_research_children_storage import (
    _auto_parent,
    _child_work_mail_wake,
    _owned_fork,
    _project,
    _work_pair,
)


def armed_watcher(
    store: AppStore,
    watcher_id: str,
    *,
    agent_id: str,
    origin_turn_id: str,
    project_id: str = "project",
    completed: bool = True,
    log_path: str = "/tmp/job.log",
) -> WatcherRecord:
    """Arm one external watcher for an agent, already finished unless told otherwise."""

    now = store.now()
    return store.arm_watcher(
        WatcherRecord(
            watcher_id=watcher_id,
            agent_id=agent_id,
            project_id=project_id,
            purpose="Training job",
            origin_turn_id=origin_turn_id,
            created_at=now,
            checks=[
                WatcherCheckRecord(
                    check_id=watcher_id,
                    watcher_id=watcher_id,
                    machine_id="backend",
                    definition="external",
                    check_command="true",
                    log_path=log_path,
                    cwd="/tmp",
                    created_at=now,
                    completed_at=now if completed else None,
                )
            ],
        )
    )


def _waiting_child(tmp_path, *, ceiling=5, fork=False, completed=True):
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    parent, root = _auto_parent(store, ceiling=ceiling)
    route, task = _work_pair(store, parent, root, worker_id="worker-watch")
    if fork:
        task = task.model_copy(update={"graph_target": _owned_fork(store, parent)})
    store.create_auto_research_child_work(route, task)
    store.checkpoint_agent_task(
        task.operation_id,
        native_session_id="saved-child-session",
        stage_machine_id="backend",
        stage_root="/tmp/child-stage",
    )
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    current = store.agent_task(task.operation_id)
    assert current is not None
    watcher = armed_watcher(
        store,
        "child-observer",
        agent_id=route.worker_id,
        origin_turn_id=task.operation_id,
        completed=completed,
    )
    wake = _child_work_mail_wake(store, current)
    wake.request.update(message="Watcher completed; /tmp/job.log", watcher_ids=[watcher.watcher_id])
    return store, parent, route, watcher, wake


@pytest.mark.parametrize("fork", [False, True])
def test_child_watcher_wake_claims_once_on_saved_route_and_does_not_spend_b(tmp_path, fork):
    store, parent, route, watcher, wake = _waiting_child(tmp_path, fork=fork)
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
    assert (
        store.auto_research_child_work(route.worker_id).current_operation_id == result.operation_id
    )
    assert store.agent_task_continuation_cause(result.operation_id) == "watcher_wake"
    assert store.watcher(watcher.watcher_id).wake_turn_id == result.operation_id
    assert store.episode_budget_meter(parent.episode_id).invocations_used == before
    assert store.auto_research_waiting_child_work_ids(parent.episode_id) == set()


@pytest.mark.parametrize("stop_parent", [False, True])
def test_child_stop_fence_cancels_watchers_and_refuses_wake(tmp_path, stop_parent):
    store, parent, route, watcher, wake = _waiting_child(tmp_path)
    root_watcher = armed_watcher(
        store,
        "root-watcher",
        agent_id=parent.episode_id,
        origin_turn_id=route.admitted_by_operation_id,
        completed=False,
    )
    if stop_parent:
        store.request_auto_research_stop_and_settle_watchers(parent.episode_id)
    else:
        store.request_auto_research_child_work_stop(route.worker_id)
    stopped = store.watcher(watcher.watcher_id)
    assert stopped.state == "cancelled" and stopped.cancelled_by == "rcp"
    assert (store.watcher(root_watcher.watcher_id).state == "cancelled") is stop_parent
    with pytest.raises(EpisodeNotRunning):
        store.create_auto_research_child_work_watcher_wake_task(
            wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
        )
    assert store.agent_task(wake.operation_id) is None


@pytest.mark.parametrize("completed", [False, True])
def test_waiting_work_blocks_guarded_finish_until_child_stop(tmp_path, completed):
    store, parent, route, _watcher, _wake = _waiting_child(tmp_path, completed=completed)
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


@pytest.mark.parametrize("change", ["running", "session", "scope", "claimed"])
def test_child_watcher_wake_refuses_busy_route_or_changed_binding_without_claim(tmp_path, change):
    store, parent, route, watcher, wake = _waiting_child(tmp_path)
    if change in {"running", "claimed"}:
        if change == "running":
            with store.connection() as connection:
                connection.execute(
                    "UPDATE graph_runs SET status = 'running' WHERE operation_id = ?",
                    (route.current_operation_id,),
                )
        else:
            store.cancel_watchers([watcher.watcher_id], by="agent")
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
    assert store.watcher(watcher.watcher_id).wake_turn_id is None
    assert store.auto_research_child_work(route.worker_id).current_operation_id != (
        wake.operation_id
    )
    assert store.episode_budget_meter(parent.episode_id).invocations_used == 1


@pytest.mark.parametrize("completed", [False, True])
def test_exhausted_ending_keeps_child_observers_and_cancels_actor_watchers(tmp_path, completed):
    store, parent, route, watcher, wake = _waiting_child(tmp_path, ceiling=2, completed=completed)
    root_watcher = armed_watcher(
        store,
        "root-watcher",
        agent_id=parent.episode_id,
        origin_turn_id=route.admitted_by_operation_id,
    )

    ended = store.fence_auto_research_ending_and_settle_watchers(parent.episode_id, "exhausted")
    assert ended.ending == "exhausted"
    retained = store.watcher(watcher.watcher_id)
    assert retained.open and retained.ready is completed
    assert store.watcher(root_watcher.watcher_id).state == "cancelled"
    with pytest.raises(EpisodeNotRunning):
        store.create_auto_research_child_work_watcher_wake_task(
            wake, worker_id=route.worker_id, watcher_ids=[watcher.watcher_id]
        )
    assert store.agent_task(wake.operation_id) is None
    assert store.watcher(watcher.watcher_id).open
    assert store.auto_research_waiting_child_work_ids(parent.episode_id) == {route.worker_id}
    store.request_auto_research_child_work_stop(route.worker_id)
    assert store.watcher(watcher.watcher_id).state == "cancelled"


def test_orchestrator_watcher_without_task_parent_cannot_become_human_owned(tmp_path):
    store, parent, route, watcher, wake = _waiting_child(tmp_path)
    with store.connection() as connection:
        connection.execute(
            "UPDATE episodes SET status = 'completed' WHERE episode_id = ?", (parent.episode_id,)
        )
    human_wake = wake.model_copy(
        update={
            "episode_id": None,
            "parent_operation_id": None,
            "request": {**wake.request, "trigger": "watcher"},
        }
    )
    with pytest.raises(ValueError) as error:
        store.create_agent_task(human_wake, continuation_cause="watcher_wake")
    assert str(error.value) == "auto_research_child_episode_owned"
    assert store.agent_task(human_wake.operation_id) is None
    assert store.watcher(watcher.watcher_id).open
