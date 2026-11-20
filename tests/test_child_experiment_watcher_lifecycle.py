from __future__ import annotations

import logging
import uuid

import pytest

from rcp.api.experiment_controls import _experiment_control_response
from rcp.core.models import Experiment, GraphState
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.storage import (
    AppStore,
    AutoResearchExperimentAllowanceReached,
    EpisodeInvocationCeilingReached,
    EpisodeNotRunning,
)

from .helpers import _TracingAppStore
from .test_auto_research_child_work_watchers_storage import armed_watcher
from .test_auto_research_children_storage import (
    _auto_parent,
    _experiment_route,
    _experiment_task,
    _project,
)


def _waiting_child(tmp_path, *, child_ceiling=10):
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
        machine_id=BACKEND_MACHINE_ID,
        native_session_id=task.native_session_id,
        stage_machine_id=None,
        stage_root=task.stage_root,
        chat_id=task.request["chat_id"],
        operation_id=task.operation_id,
        invocation=1,
        graph_result="no_patch",
        watcher_ids=[],
        context_baseline={},
    )
    store.complete_agent_task(task.operation_id, applied_revision=None, result={})
    watcher = armed_watcher(
        store,
        "child-watcher",
        agent_id=task.request["chat_id"],
        origin_turn_id=task.operation_id,
        project_id=task.project_id,
    )
    claimed = []

    def deliver():
        """Offer the child agent's ready watchers to its Experiment wake claim once."""

        ids = [item.watcher_id for item in store.ready_watchers()]
        if not ids:
            return
        wake = task.model_copy(
            update={
                "operation_id": str(uuid.uuid4()),
                "stage_machine_id": None,
                "created_at": store.now(),
                "updated_at": store.now(),
                "request": {
                    **task.request,
                    "trigger": "watcher",
                    "watcher_ids": ids,
                    "control_invocation": 2,
                },
            }
        )
        try:
            stored = store.create_experiment_watcher_invocation(wake, ids)
        except (
            AutoResearchExperimentAllowanceReached,
            EpisodeInvocationCeilingReached,
            EpisodeNotRunning,
        ):
            return
        if stored is not None:
            claimed.append(stored.operation_id)

    return store, parent, root, task, watcher, claimed, deliver


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
        runtimes = store.experiment_loop_runtimes(
            parent.project_id,
            [f"exp/child-{child_index}" for child_index in range(index + 1)],
            graph_target=parent.graph_target,
        )
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
    tmp_path, caplog, fence
):
    store, parent, root, child, watcher, claimed, deliver = _waiting_child(
        tmp_path, child_ceiling=1 if fence == "child_ceiling" else 10
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
        deliver()

    retained = store.watcher(watcher.watcher_id)
    assert retained.ready
    assert store.auto_research_experiment_allowance(parent.episode_id) == allowance
    assert store.episode_budget_meter(child.episode_id) == child_budget
    assert [task.operation_id for task in store.agent_tasks(parent.project_id)] == task_ids
    assert claimed == []
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    runtime = store.experiment_loop_runtime(
        child.project_id, str(child.request["control_node_id"]), graph_target=child.graph_target
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
        assert "Auto-research parent" in runtime.watcher_delivery_diagnostic


def test_running_parent_child_experiment_watcher_claims_once(tmp_path):
    store, parent, _root, child, watcher, claimed, deliver = _waiting_child(tmp_path)
    before = store.auto_research_experiment_allowance(parent.episode_id).used
    runtime = store.experiment_loop_runtime(
        child.project_id, str(child.request["control_node_id"]), graph_target=child.graph_target
    )
    assert runtime.watcher_delivery_diagnostic is None

    deliver()
    deliver()

    assert len(claimed) == 1
    assert store.watcher(watcher.watcher_id).wake_turn_id == claimed[0]
    assert store.agent_task(claimed[0]).native_session_id == child.native_session_id
    assert store.auto_research_experiment_allowance(parent.episode_id).used == before + 1
    assert store.episode_budget_meter(child.episode_id).invocations_used == 2
