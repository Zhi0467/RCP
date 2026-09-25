from __future__ import annotations

import logging
import uuid

import pytest

from rcp.api.experiment_controls import _experiment_control_response
from rcp.background import BackgroundAgentTasks
from rcp.core.models import Experiment, GraphState
from rcp.runs.watcher_admission import start_watcher_notification
from rcp.service import RunRequest
from rcp.storage import AppStore, WatcherContinuation
from rcp.watchers import WatcherPoller

from .test_auto_research_children_storage import (
    _auto_parent,
    _experiment_route,
    _experiment_task,
    _project,
)
from .test_storage import _TracingAppStore
from .test_watchers import _record


def _waiting_child(tmp_path, monkeypatch, *, child_ceiling=10):
    store = AppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    parent, root = _auto_parent(store, ceiling=1)
    task = _experiment_task(
        store,
        str(uuid.uuid4()),
        parent.authorized_by,
        node_id="exp/waiting",
        ceiling=child_ceiling,
        session_id="child-session",
        stage_root=str(tmp_path),
    )
    store.create_experiment_episode_with_invocation(
        task, auto_research_route=_experiment_route(store, parent, root, task)
    )
    store.commit_experiment_episode_turn(
        episode_id=task.episode_id,
        project_id=task.project_id,
        control_node_id=task.request["control_node_id"],
        provider="codex",
        execution_machine="local",
        execution_host="",
        native_session_id=task.native_session_id,
        stage_host=None,
        stage_root=task.stage_root,
        chat_id=task.request["chat_id"],
        operation_id=task.operation_id,
        invocation=1,
        graph_result="no_patch",
        watcher_ids=[],
        context_baseline={},
    )
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    continuation = WatcherContinuation.model_validate(
        {
            key: value
            for key, value in task.request.items()
            if key in WatcherContinuation.model_fields
        }
    )
    watcher = _record("child-watcher", origin=task.operation_id, status="completed").model_copy(
        update={
            "episode_id": task.episode_id,
            "graph_target": parent.graph_target,
            "continuation": continuation,
            "node_id": task.request["node_id"],
            "chat_id": task.request["chat_id"],
        }
    )
    store.create_watchers([watcher])

    async def forbidden_stream(*_args, **_kwargs):
        raise AssertionError("The admission tests must not launch a provider.")
        yield

    tasks = BackgroundAgentTasks(store, forbidden_stream)
    launches = []

    def record_launch(operation_id):
        launches.append(operation_id)
        return store.agent_task(operation_id)

    monkeypatch.setattr(tasks, "launch_admitted", record_launch)

    def deliver(group):
        ids = [item.watcher_id for item in group]
        request = RunRequest.model_validate(
            {
                **task.request,
                "trigger": "watcher",
                "watcher_ids": ids,
                "control_invocation": 2,
            }
        )
        return start_watcher_notification(
            tasks,
            task.project_id,
            "node_chat",
            request,
            ids,
            authorized_by=parent.authorized_by,
            episode_stage_root=task.stage_root,
        )

    poller = WatcherPoller(store, on_completed=deliver)
    return store, parent, root, task, watcher, launches, poller


def test_parent_wake_diagnostics_use_a_constant_number_of_projection_reads(tmp_path):
    store = _TracingAppStore(tmp_path / "rcp.sqlite3")
    _project(store)
    parent, root = _auto_parent(store, ceiling=1)
    allowance = store.auto_research_experiment_allowance(parent.episode_id)
    select_counts = []
    for index in range(allowance.remaining):
        task = _experiment_task(
            store, str(uuid.uuid4()), parent.authorized_by, node_id=f"exp/child-{index}"
        )
        store.create_experiment_episode_with_invocation(
            task, auto_research_route=_experiment_route(store, parent, root, task)
        )
        store.select_count = 0
        runtimes = store.project_experiment_loop_runtimes(parent.project_id)
        select_counts.append(store.select_count)
        assert len(runtimes) == index + 1
        exhausted = index + 1 == allowance.remaining
        assert all(
            bool(runtime.watcher_delivery_diagnostic) == exhausted for runtime in runtimes.values()
        )
    assert len(set(select_counts)) == 1


@pytest.mark.parametrize(
    "fence", ["exhausted", "failed", "human_pause", "completed", "shared_E", "child_ceiling"]
)
def test_child_experiment_watcher_refusal_retains_completion_without_callback_error(
    tmp_path, monkeypatch, caplog, fence
):
    store, parent, root, child, watcher, launches, poller = _waiting_child(
        tmp_path, monkeypatch, child_ceiling=1 if fence == "child_ceiling" else 10
    )
    if fence == "shared_E":
        remaining = store.auto_research_experiment_allowance(parent.episode_id).remaining
        for index in range(remaining):
            task = _experiment_task(
                store,
                str(uuid.uuid4()),
                parent.authorized_by,
                node_id=f"exp/other-{index}",
            )
            store.create_experiment_episode_with_invocation(
                task, auto_research_route=_experiment_route(store, parent, root, task)
            )
    elif fence != "child_ceiling":
        store.fence_auto_research_ending_and_settle_watchers(parent.episode_id, fence)
    allowance = store.auto_research_experiment_allowance(parent.episode_id)
    child_budget = store.episode_budget_meter(child.episode_id)
    task_ids = [task.operation_id for task in store.agent_tasks(parent.project_id)]

    for _ in range(2):
        assert [[item.watcher_id for item in group] for group in poller.poll_once()] == [
            [watcher.watcher_id]
        ]

    retained = store.watcher(watcher.watcher_id)
    assert retained.status == "completed" and not retained.notified
    assert retained.notification_operation_id is None
    assert store.auto_research_experiment_allowance(parent.episode_id) == allowance
    assert store.episode_budget_meter(child.episode_id) == child_budget
    assert [task.operation_id for task in store.agent_tasks(parent.project_id)] == task_ids
    assert launches == []
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    runtime = store.experiment_loop_runtime_for_target(
        child.project_id, child.request["control_node_id"], child.graph_target
    )
    node = Experiment(
        type="experiment",
        id=child.request["control_node_id"],
        title="Waiting child",
        objective="Inspect the completed observation.",
        completion_criteria=["The result is inspected."],
    )
    control = _experiment_control_response(
        GraphState(nodes={node.id: node}), node.id, runtime, None
    )
    if fence == "child_ceiling":
        assert control.health == "paused_at_limit"
        assert runtime.watcher_delivery_diagnostic is None
    else:
        assert control.health == "needs_action"
        assert control.recommendation == "stop_and_restart"
        assert runtime.watcher_delivery_diagnostic in control.reasons


def test_running_parent_child_experiment_watcher_claims_and_launches_once(tmp_path, monkeypatch):
    store, parent, _root, child, watcher, launches, poller = _waiting_child(tmp_path, monkeypatch)
    before = store.auto_research_experiment_allowance(parent.episode_id).used
    runtime = store.experiment_loop_runtime_for_target(
        child.project_id, child.request["control_node_id"], child.graph_target
    )
    assert runtime.watcher_delivery_diagnostic is None

    poller.poll_once()
    assert poller.poll_once() == []

    assert len(launches) == 1
    assert store.watcher(watcher.watcher_id).notification_operation_id == launches[0]
    assert store.watcher(watcher.watcher_id).notified
    assert store.agent_task(launches[0]).native_session_id == child.native_session_id
    assert store.auto_research_experiment_allowance(parent.episode_id).used == before + 1
    assert store.episode_budget_meter(child.episode_id).invocations_used == 2


def test_unexpected_child_experiment_watcher_error_remains_visible(tmp_path, monkeypatch, caplog):
    store, _parent, _root, _child, watcher, launches, poller = _waiting_child(tmp_path, monkeypatch)

    def invalid_binding(*_args, **_kwargs):
        raise ValueError("The watcher wake belongs to another episode.")

    monkeypatch.setattr(store, "create_experiment_watcher_invocation", invalid_binding)
    poller.poll_once()

    assert any(record.levelname == "ERROR" and record.exc_info for record in caplog.records)
    assert "The watcher wake belongs to another episode." in caplog.text
    assert not store.watcher(watcher.watcher_id).notified
    assert launches == []
