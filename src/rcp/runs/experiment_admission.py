from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rcp.control import (
    ExperimentControlState,
    ExperimentOperationalState,
    ExperimentSessionBinding,
    derive_experiment_control_state,
)
from rcp.core.models import AuthorizedHuman, Experiment, ExperimentDecisionPin, GraphState
from rcp.core.transition_models import GraphTargetRef
from rcp.service import ProjectService, RunRequest
from rcp.storage import AgentTaskRecord, AppStore, EpisodeRecord, ExperimentLoopRuntime
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES, AWAITING_HUMAN_AGENT_TASK_STATUSES

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


def experiment_control(
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
    A caller that can reach reconciliation must already hold the canonical
    project operation lock; this helper deliberately does not reacquire it.
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
    return runtime, experiment_control_from_runtime(state, experiment_id, runtime)


def experiment_control_from_runtime(
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
        operational=experiment_operational_state(runtime),
    )


def experiment_operational_state(runtime: ExperimentLoopRuntime) -> ExperimentOperationalState:
    """Project the loop runtime onto the operational block Runs reads.

    The native session id itself stays in the backend; whether one is bound is
    the only part of it the human needs.
    """

    return ExperimentOperationalState(
        task_active=runtime.task_active,
        detached_work_active=runtime.detached_work_active,
        watcher_degraded=runtime.watcher_degraded,
        watcher_completion_pending=runtime.watcher_completion_pending,
        watcher_delivery_diagnostic=runtime.watcher_delivery_diagnostic,
        episode_exited=runtime.episode_exited,
        episode_live=runtime.episode_live,
        stop_requested=runtime.stop_requested,
        stop_settled=runtime.stop_settled,
        chat_id=runtime.chat_id,
        current_operation_id=runtime.current_operation_id,
        current_status=runtime.current_status,
        current_queued=runtime.current_status == "queued",
        current_active=runtime.current_status in ACTIVE_AGENT_TASK_STATUSES,
        current_awaiting_human=runtime.current_status in AWAITING_HUMAN_AGENT_TASK_STATUSES,
        current_phase=runtime.current_phase,
        current_status_message=runtime.current_status_message,
        current_failure_kind=runtime.current_failure_kind,
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


def experiment_start_message(message: str | None, node_id: str) -> str:
    """Preserve an explicit goal and use the canonical fallback only for blank input."""

    if message is not None and message.strip():
        return message
    return f"Begin a bounded Experiment-loop episode for {node_id}."


def resolve_experiment_node_work_request(
    service: ProjectService,
    request: RunRequest,
) -> RunRequest:
    """Resolve one Experiment turn from the node-Work profile and the human's pick.

    A human start may name provider, model, and reasoning; the machine stays the
    profile's. An orchestrator child carries the orchestrator's pick, or none, so it
    keeps the Settings profile.
    """

    profile = service.resolve_agent_profile(
        "node_chat",
        provider=request.provider,
        model=request.model,
        reasoning=request.reasoning,
    )
    resolved = request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
            "run_truth_scope": list(
                request.run_truth_scope or service.manifest.agent.default_run_truth_scope
            ),
        }
    )
    skill_resolved = service.resolve_skill_request(resolved)
    if not isinstance(skill_resolved, RunRequest):
        raise TypeError("Experiment skill resolution changed the task request type.")
    return skill_resolved


def fresh_experiment_run_request(
    service: ProjectService,
    supplied: RunRequest,
    *,
    node: Experiment,
    state_revision: int,
    control: ExperimentControlState,
    episode_id: str,
    invocation_ceiling: int | None = None,
    trigger: Literal["experiment_run", "orchestrator"] = "experiment_run",
) -> RunRequest:
    """Build the immutable invocation-one contract for a fresh Experiment episode."""

    ceiling = node.invocation_ceiling if invocation_ceiling is None else invocation_ceiling
    if ceiling < 1:
        raise ValueError("Experiment invocation limit must be positive.")
    request = supplied.model_copy(
        update={
            "chat_scope": "node",
            "node_id": node.id,
            "message": experiment_start_message(supplied.message, node.id),
            "session_id": None,
            "mode": "work",
            "trigger": trigger,
            "patch_kind": "experiment_loop",
            "control_node_id": node.id,
            "control_revision": state_revision,
            "control_episode_id": episode_id,
            "control_invocation": 1,
            "control_invocation_ceiling": ceiling,
            "control_decision_bundle": control.governing_decisions,
            "control_completion_criteria": list(node.completion_criteria),
            "watcher_ids": [],
        }
    )
    return service.resolve_compute_request(resolve_experiment_node_work_request(service, request))


@dataclass(frozen=True)
class WorkExperimentStart:
    """One Work turn's Experiment kickoff, resolved by its command handler."""

    #: The Work task that issued the command; it supplies project, target and authorizer.
    source_operation_id: str
    #: Derived from the turn and the command key, so a replay names the same episode.
    episode_id: str
    node_id: str
    goal: str
    invocation_limit: int | None
    provider: str | None
    model: str | None
    reasoning: str | None


WorkExperimentStarter = Callable[[WorkExperimentStart], AgentTaskRecord]


class ExperimentNotReady(ValueError):
    """The Experiment's graph or operational state refuses a fresh episode."""


def ready_experiment_control(
    store: AppStore,
    project_id: str,
    state: GraphState,
    node_id: str,
    *,
    graph_target: GraphTargetRef,
) -> tuple[ExperimentLoopRuntime, ExperimentControlState]:
    """The one start rule: this target's loop runtime and a control that is ready."""

    runtime, control = experiment_control(
        store, project_id, state, node_id, graph_target=graph_target
    )
    if not control.ready:
        raise ExperimentNotReady(" ".join(control.reasons))
    return runtime, control


def start_fresh_experiment(
    tasks: BackgroundAgentTasks,
    service: ProjectService,
    project_id: str,
    supplied: RunRequest,
    *,
    node: Experiment,
    state: GraphState,
    invocation_ceiling: int,
    authorized_by: AuthorizedHuman,
    ensure_graph_target: Callable[[EpisodeRecord], None],
    episode_id: str,
) -> AgentTaskRecord:
    """Admit invocation 1 of a fresh Experiment episode on the service's graph target.

    A human Run and a Work turn's ``episode --kick-off-experiment`` both start
    here, so they share one readiness rule and one delivery of completions a
    previous episode left unclaimed. ``supplied`` must carry a fresh chat id.
    """

    from rcp.runs.episodes.isolation import validate_episode_admission
    from rcp.runs.experiment_loop import experiment_watcher_delivery_request
    from rcp.runs.watcher_admission import start_watcher_notification

    store = tasks.store
    target = service.history.graph_target
    if not supplied.chat_id:
        raise ValueError("Run requires a chat_id")
    # A fresh Run is its own conversation; joining an existing chat would
    # file the episode inside that chat's history.
    if store.chat_graph_target(project_id, supplied.chat_id) is not None:
        raise ValueError("Run requires a new chat_id; this conversation already has turns.")
    creates_branch = supplied.graph_isolation and target.kind == "main"
    runtime, control = ready_experiment_control(
        store,
        project_id,
        state,
        node.id,
        graph_target=(
            # An episode's own branch is named by the episode, as before
            # forks; chat and Runs readers key on that identity.
            GraphTargetRef(kind="branch", branch_id=episode_id) if creates_branch else target
        ),
    )
    pending_group = (
        None
        if creates_branch or (runtime.stop_requested and runtime.stop_settled)
        else store.completed_experiment_watcher_group(project_id, node.id, graph_target=target)
    )
    if pending_group is not None:
        request = experiment_watcher_delivery_request(
            pending_group,
            trigger="experiment_run",
            episode_id=episode_id,
            invocation=1,
            invocation_ceiling=invocation_ceiling,
            control_revision=state.revision,
            decision_bundle=control.governing_decisions,
            completion_criteria=list(node.completion_criteria),
        )
        request = request.model_copy(
            update={
                "run_truth_scope": supplied.run_truth_scope,
                "browser_requested": supplied.browser_requested,
                "code_worktree": supplied.code_worktree,
                "graph_isolation": supplied.graph_isolation,
                "chat_scope": "node",
                "node_id": node.id,
                "message": experiment_start_message(supplied.message, node.id),
                "chat_id": supplied.chat_id,
                "session_id": None,
                # A fresh session: the starter's pick, else Settings,
                # never the delivered watcher's old binding.
                "provider": supplied.provider,
                "model": supplied.model,
                "reasoning": supplied.reasoning,
                "started_from_operation_id": supplied.started_from_operation_id,
            }
        )
        request = resolve_experiment_node_work_request(service, request)
        validate_episode_admission(store, project_id, request, graph_target=target)
        record = start_watcher_notification(
            tasks,
            project_id,
            "node_chat",
            request,
            [item.watcher_id for item in pending_group],
            authorized_by=authorized_by,
        )
        if record is None:
            raise ValueError(
                "The pending watcher completion could not be claimed because its "
                "conversation is active."
            )
        return record
    request = fresh_experiment_run_request(
        service,
        supplied,
        node=node,
        state_revision=state.revision,
        control=control,
        episode_id=episode_id,
        invocation_ceiling=invocation_ceiling,
    )
    return tasks.start(
        project_id,
        "node_chat",
        request,
        authorized_by=authorized_by,
        graph_target=target,
        graph_base_head=service.history.head_ref() if creates_branch else None,
        ensure_graph_target=ensure_graph_target if creates_branch else None,
    )


def start_experiment_continuation(
    tasks: BackgroundAgentTasks,
    project_id: str,
    request: RunRequest,
    *,
    source: EpisodeRecord,
    authorized_by: AuthorizedHuman,
    stage_host: str | None,
    stage_root: str,
    continuation_request_id: str,
) -> AgentTaskRecord:
    """Admit invocation 1 of a continuation that resumes its source's session.

    ``start`` refuses a saved stage and a watcher notification refuses a session
    on a human Run, because both are fresh episodes; a continuation is the one
    Experiment Run that carries the ended episode's exact session and stage.
    """

    if not authorized_by.display_name.strip():
        raise ValueError("An Experiment continuation requires a named human authorizer snapshot.")
    if (
        request.patch_kind != "experiment_loop"
        or request.trigger != "experiment_run"
        or request.control_invocation != 1
        or not request.session_id
        or not request.chat_id
        or request.control_node_id != source.control_node_id
    ):
        raise ValueError("An Experiment continuation is invocation 1 on the source's session.")
    estimate, samples = tasks.store.agent_task_estimate(
        project_id, "node_chat", request.model_dump(mode="json")
    )
    record = tasks._create_and_spawn(
        project_id,
        "node_chat",
        request,
        estimate_seconds=estimate,
        estimate_samples=samples,
        authorized_by=authorized_by,
        stage_host=stage_host,
        stage_root=stage_root,
        graph_target=source.graph_target,
        continues_episode_id=source.episode_id,
        continuation_request_id=continuation_request_id,
    )
    if record is None:
        raise RuntimeError("Experiment continuation admission returned no task")
    return record


def proven_reserved_experiment_branch_roots(
    tasks: BackgroundAgentTasks,
) -> list[tuple[EpisodeRecord, AgentTaskRecord]]:
    """Graph-isolated Experiments admitted but interrupted before their branch existed."""

    reserved: list[tuple[EpisodeRecord, AgentTaskRecord]] = []
    for project in tasks.store.projects():
        for episode in tasks.store.episodes(project.project_id, limit=None):
            if (
                episode.mode != "experiment_loop"
                or episode.root_operation_id is None
                or episode.graph_target.kind != "branch"
                or tasks.store.branch_owner(
                    episode.project_id, episode.graph_target.branch_id or ""
                )
                != episode.episode_id
                or episode.status not in {"queued", "running"}
            ):
                continue
            task = tasks.store.agent_task(episode.root_operation_id)
            if (
                task is None
                or task.episode_id != episode.episode_id
                or task.graph_target != episode.graph_target
                or task.status != "queued"
                or not tasks.store.agent_task_dispatch_was_proven_not_started(task.operation_id)
            ):
                continue
            reserved.append((episode, task))
    return reserved


def reconcile_reserved_experiment_branch_roots(
    tasks: BackgroundAgentTasks,
    ensure_graph_target: Callable[[EpisodeRecord], None],
) -> list[str]:
    """Finish branch creation and launch Experiment roots reserved before an interruption."""

    from rcp.runs.episodes.wrapup import EpisodeWrapupSpec, begin_episode_report_wrapup

    started: list[str] = []
    for episode, task in proven_reserved_experiment_branch_roots(tasks):
        try:
            ensure_graph_target(episode)
        except Exception as exc:
            tasks.store.fail_agent_task(task.operation_id, str(exc))
            begin_episode_report_wrapup(
                tasks.store,
                EpisodeWrapupSpec(
                    episode_id=episode.episode_id,
                    ending="failed",
                    partial=True,
                    continuation_operation_id=task.operation_id,
                    receipt={"reason": "graph_branch_unavailable_before_launch"},
                    diagnostic=str(exc),
                ),
            )
            continue
        tasks.launch_admitted(task.operation_id)
        started.append(task.operation_id)
    return started
