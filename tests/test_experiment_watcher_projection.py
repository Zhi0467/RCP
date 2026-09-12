from __future__ import annotations

import pytest

from rcp.api.experiment_controls import _experiment_control_response
from rcp.core.models import Experiment, GraphState
from rcp.storage.models import ExperimentLoopRuntime


@pytest.fixture
def graph() -> GraphState:
    node = Experiment(
        id="experiment/projection",
        type="experiment",
        title="Watcher continuation",
        objective="Audit completed external work.",
        status="running",
    )
    return GraphState(nodes={node.id: node})


@pytest.fixture
def runtime() -> ExperimentLoopRuntime:
    return ExperimentLoopRuntime(
        episode_id="episode-current",
        invocations_used=1,
        invocation_ceiling=3,
        active=True,
        episode_live=True,
        session_bound=True,
        watcher_completion_pending=True,
    )


def test_completed_watchers_are_pending_delivery_instead_of_still_running(
    graph: GraphState, runtime: ExperimentLoopRuntime
) -> None:
    response = _experiment_control_response(graph, "experiment/projection", runtime, None)

    assert response.health == "completion_pending"
    assert response.recommendation == "wait"
    assert response.run_section == "running"
    assert response.live and response.can_stop
    assert response.operational.watcher_delivery_diagnostic is None


def test_blocked_watcher_delivery_explains_why_human_action_is_needed(
    graph: GraphState, runtime: ExperimentLoopRuntime
) -> None:
    diagnostic = "The owning Auto-research episode has no invocations remaining."
    runtime = runtime.model_copy(
        update={
            "watcher_delivery_diagnostic": diagnostic,
        }
    )

    response = _experiment_control_response(graph, "experiment/projection", runtime, None)

    assert response.health == "needs_action"
    assert response.recommendation == "stop_and_restart"
    assert response.run_section == "actionable"
    assert response.can_stop and not response.can_start
    assert response.operational.watcher_delivery_diagnostic == diagnostic
    assert diagnostic in response.reasons
    assert diagnostic not in response.graph_reasons


@pytest.mark.parametrize(
    ("updates", "health", "recommendation"),
    [
        ({"stop_requested": True}, "stopping", "wait"),
        ({"current_status": "queued", "task_active": True}, "starting", "wait"),
        ({"current_status": "running", "task_active": True}, "agent_active", "wait"),
        ({"invocations_used": 3}, "paused_at_limit", "stop_and_restart"),
        ({"session_diagnostic": "Session unavailable."}, "needs_action", "stop_and_restart"),
    ],
)
def test_pending_delivery_preserves_stop_task_budget_and_session_precedence(
    graph: GraphState,
    runtime: ExperimentLoopRuntime,
    updates: dict[str, object],
    health: str,
    recommendation: str,
) -> None:
    response = _experiment_control_response(
        graph, "experiment/projection", runtime.model_copy(update=updates), None
    )

    assert response.health == health
    assert response.recommendation == recommendation


@pytest.mark.parametrize("completion_pending", [False, True])
@pytest.mark.parametrize("parent_blocked", [False, True])
def test_active_observers_still_wait_for_completion(
    graph: GraphState,
    runtime: ExperimentLoopRuntime,
    completion_pending: bool,
    parent_blocked: bool,
) -> None:
    diagnostic = "The Auto-research parent cannot admit a continuation."
    response = _experiment_control_response(
        graph,
        "experiment/projection",
        runtime.model_copy(
            update={
                "watcher_completion_pending": completion_pending,
                "detached_work_active": True,
                "watcher_delivery_diagnostic": diagnostic if parent_blocked else None,
            }
        ),
        None,
    )

    assert response.health == "waiting_on_watchers"
    assert response.recommendation == "wait"
    assert diagnostic not in response.reasons
