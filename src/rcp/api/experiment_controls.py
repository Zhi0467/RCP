from __future__ import annotations

from rcp.control import (
    ExperimentControlState,
    ExperimentOperationalState,
    ExperimentSessionBinding,
    derive_experiment_control_state,
)
from rcp.core.models import ExperimentDecisionPin, GraphState
from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore, ExperimentLoopRuntime


def _experiment_control(
    store: AppStore,
    project_id: str,
    state: GraphState,
    experiment_id: str,
    *,
    graph_target: GraphTargetRef,
) -> tuple[ExperimentLoopRuntime, ExperimentControlState]:
    """Derive one Experiment's operational and semantic control state together.

    Deriving is also where a graceful stop is reconciled, so the same joint
    handoff settles identically after a restart without anyone replaying it.
    """

    runtime = store.experiment_loop_runtime(
        project_id,
        experiment_id,
        graph_target=graph_target,
    )
    if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active:
        store.settle_experiment_loop_stop(
            project_id,
            experiment_id,
            episode_id=runtime.episode_id,
            graph_target=graph_target,
        )
        runtime = store.experiment_loop_runtime(
            project_id,
            experiment_id,
            graph_target=graph_target,
        )
    return runtime, _experiment_control_from_runtime(state, experiment_id, runtime)


def _experiment_control_for_target(
    store: AppStore,
    project_id: str,
    state: GraphState,
    experiment_id: str,
    *,
    graph_target: GraphTargetRef,
) -> tuple[ExperimentLoopRuntime, ExperimentControlState]:
    """Derive and reconcile one exact target-bound operational runtime."""

    runtime = store.experiment_loop_runtime_for_target(
        project_id,
        experiment_id,
        graph_target,
    )
    if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active:
        store.settle_experiment_loop_stop(
            project_id,
            experiment_id,
            episode_id=runtime.episode_id,
            graph_target=graph_target,
        )
        runtime = store.experiment_loop_runtime_for_target(
            project_id,
            experiment_id,
            graph_target,
        )
    return runtime, _experiment_control_from_runtime(state, experiment_id, runtime)


def _experiment_control_from_runtime(
    state: GraphState,
    experiment_id: str,
    runtime: ExperimentLoopRuntime,
) -> ExperimentControlState:
    """Combine graph authority with one already-projected operational runtime."""

    pins = [ExperimentDecisionPin.model_validate(item) for item in runtime.decision_bundle]
    return derive_experiment_control_state(
        state,
        experiment_id,
        {experiment_id} if runtime.active else set(),
        episode_id=runtime.episode_id,
        invocations_used=runtime.invocations_used,
        invocation_ceiling=runtime.invocation_ceiling,
        paused=runtime.paused,
        detached_work_active=runtime.detached_work_active,
        episode_decision_bundle=pins if runtime.episode_id is not None else None,
        operational=_experiment_operational_state(runtime),
    )


def _experiment_operational_state(runtime: ExperimentLoopRuntime) -> ExperimentOperationalState:
    """Project the loop runtime onto the operational block Runs reads.

    The native session id itself stays in the backend; whether one is bound is
    the only part of it the human needs.
    """

    return ExperimentOperationalState(
        task_active=runtime.task_active,
        detached_work_active=runtime.detached_work_active,
        watcher_degraded=runtime.watcher_degraded,
        watcher_completion_pending=runtime.watcher_completion_pending,
        episode_exited=runtime.episode_exited,
        episode_live=runtime.episode_live,
        stop_requested=runtime.stop_requested,
        stop_settled=runtime.stop_settled,
        chat_id=runtime.chat_id,
        current_operation_id=runtime.current_operation_id,
        current_status=runtime.current_status,
        current_phase=runtime.current_phase,
        current_status_message=runtime.current_status_message,
        current_last_activity_at=runtime.current_last_activity_at,
        current_invocation=runtime.current_invocation,
        session=ExperimentSessionBinding(
            provider=runtime.provider,
            model=runtime.model,
            reasoning=runtime.reasoning,
            run_on=runtime.run_on,
            execution_host=runtime.execution_host,
            run_truth_scope=runtime.run_truth_scope,
            native_session_bound=runtime.session_bound,
            diagnostic=runtime.session_diagnostic,
        ),
    )


__all__ = [
    "_experiment_control",
    "_experiment_control_for_target",
    "_experiment_control_from_runtime",
    "_experiment_operational_state",
]
