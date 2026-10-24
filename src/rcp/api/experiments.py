from __future__ import annotations

import uuid
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import UUID4

from rcp.api.dependencies import (
    get_background_tasks,
    get_catalog,
    get_experiment_operation_lock,
    get_graph_service,
    get_identity_access,
    get_project_writer,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.episode_branches import ensure_episode_graph_target
from rcp.api.episodes import _episode_for_http, child_experiment_on_owned_branch
from rcp.api.graph_changes import require_graph_edit_admission
from rcp.api.identity import IdentityAccess
from rcp.background import BackgroundAgentTasks
from rcp.control import ExperimentControlState
from rcp.core.models import AuthorizedHuman, Experiment
from rcp.core.transition_models import GraphTargetRef
from rcp.history.branches import canonical_branch_id
from rcp.keyed_locks import KeyedLocks
from rcp.loop_status import LoopOverlap, other_branch_loops
from rcp.projects import ProjectCatalog
from rcp.runs.auto_research_admission import require_auto_research_child_profile_pick
from rcp.runs.experiment_admission import (
    ExperimentNotReady,
    WorkExperimentStart,
    experiment_control,
    experiment_operational_state,
    start_experiment_continuation,
    start_fresh_experiment,
)
from rcp.runs.membership_fence import fence_episodes_for_departed_member
from rcp.runs.provider_login import provider_login_host
from rcp.service import RunRequest
from rcp.storage import (
    AgentTaskAdmissionConflict,
    AgentTaskRecord,
    AppStore,
    EpisodeNotRunning,
    EpisodeRecord,
)
from rcp.storage.branch_names import BranchNameInvalid, BranchNameTaken, normalize_branch_name
from rcp.storage.client_requests import ClientRequestConflict
from rcp.transport import StateUnavailable

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityAccessDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
BackgroundTasksDependency = Annotated[BackgroundAgentTasks, Depends(get_background_tasks)]
ExperimentOperationLockDependency = Annotated[
    KeyedLocks,
    Depends(get_experiment_operation_lock),
]


class ExperimentStartResponse(AgentTaskRecord):
    """The admitted task plus informational overlap on other graph targets."""

    live_elsewhere: LoopOverlap


def _start_response(store: AppStore, task: AgentTaskRecord, node_id: str) -> dict[str, object]:
    return ExperimentStartResponse(
        **task.model_dump(),
        live_elsewhere=other_branch_loops(
            store,
            task.project_id,
            graph_target=task.graph_target,
            node_id=node_id,
        ),
    ).model_dump(mode="json")


@router.post(
    "/api/projects/{project_id}/experiments/{node_id:path}/run",
    status_code=202,
    response_model=ExperimentStartResponse,
    dependencies=[Depends(require_project_write_admission)],
)
def run_experiment(
    project_id: str,
    node_id: str,
    body: dict[str, object],
    request: Request,
    branch_id: str | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityAccessDependency,
    background_tasks: BackgroundTasksDependency,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
) -> dict[str, object]:
    if body.get("references"):
        raise HTTPException(status_code=422, detail="Experiment runs do not accept references.")
    authorized_by = identity_access.require_patch_capable_identity(request)
    if idempotency_key is not None:
        project_id = catalog.resolve_project_id(project_id)
    try:
        with store.client_request_admission(
            project_id,
            authorized_by.user_id,
            str(idempotency_key) if idempotency_key is not None else None,
            "experiments/run",
            result_kind="operation",
        ) as existing:
            if existing is not None:
                record = store.agent_task(existing.operation_id or "")
                assert record is not None
                return _start_response(store, record, str(record.request.get("node_id", node_id)))
            project_id = catalog.resolve_project_id(project_id)
            service = get_graph_service(catalog, project_id, branch_id, write=True)
            target = service.history.graph_target
            require_graph_edit_admission(store, project_id, target)
            try:
                state = service.history.state()
                node = state.nodes.get(node_id)
                if not isinstance(node, Experiment):
                    raise HTTPException(status_code=404, detail="Experiment not found")
                client_request = dict(body)
                client_request.pop("resolved_compute_context", None)
                client_request.pop("isolation_owner_episode_id", None)
                client_request.pop("started_from_operation_id", None)
                # Reauthorizing the next episode is an operational human act, not research
                # truth, so an explicit ceiling arrives with the Run instead of requiring a
                # staged node edit and a Sync. The node value remains the default.
                requested_ceiling = client_request.pop("invocation_ceiling", None)
                if requested_ceiling is not None and (
                    not isinstance(requested_ceiling, int)
                    or isinstance(requested_ceiling, bool)
                    or requested_ceiling < 1
                    # Past 2^53 every JSON client reads back a different number than the
                    # one authorized here, so Runs would act on a budget nobody chose.
                    or requested_ceiling > 2**53 - 1
                ):
                    raise ValueError("The authorized invocation limit must be a positive integer.")
                episode_ceiling = (
                    node.invocation_ceiling if requested_ceiling is None else requested_ceiling
                )
                supplied = RunRequest.model_validate(client_request)
                if supplied.branch_name is not None and (
                    not supplied.graph_isolation or target.kind != "main"
                ):
                    raise BranchNameInvalid("A branch name requires a new isolated graph branch.")
                supplied.branch_name = normalize_branch_name(supplied.branch_name)
                if not supplied.chat_id:
                    raise ValueError("Run requires a chat_id")
                uuid.UUID(supplied.chat_id)
                record = start_fresh_experiment(
                    background_tasks,
                    service,
                    project_id,
                    supplied,
                    node=node,
                    state=state,
                    invocation_ceiling=episode_ceiling,
                    authorized_by=authorized_by,
                    ensure_graph_target=partial(ensure_episode_graph_target, catalog=catalog),
                    episode_id=str(uuid.uuid4()),
                )
            except ExperimentNotReady as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except AgentTaskAdmissionConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except BranchNameInvalid as exc:
                raise HTTPException(
                    status_code=422, detail={"code": "branch_name_invalid", "message": str(exc)}
                ) from exc
            except BranchNameTaken as exc:
                raise HTTPException(
                    status_code=409, detail={"code": "branch_name_taken", "message": str(exc)}
                ) from exc
            except ClientRequestConflict:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _start_response(store, record, node_id)
    except ClientRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def start_work_experiment(
    catalog: ProjectCatalog,
    tasks: BackgroundAgentTasks,
    operation_lock: KeyedLocks,
    start: WorkExperimentStart,
) -> AgentTaskRecord:
    """Start the Experiment a Work turn's command names, as its human's Run would.

    The turn's authorizer and graph target stand in for the Run button's member
    and page, under the same project admission fence. The agent may shorten the
    episode but never exceed the node's limit.
    """

    store = tasks.store
    source = store.agent_task(start.source_operation_id)
    if source is None or source.authorized_by is None:
        raise ValueError("The Work turn that sent this command has no human authorizer.")
    project_id = source.project_id
    target = source.graph_target
    try:
        service = get_graph_service(catalog, project_id, target.branch_id, write=True)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    # An uncached pick probes its provider CLI, so it is checked outside the lock.
    reasoning = require_auto_research_child_profile_pick(
        service, provider=start.provider, model=start.model, reasoning=start.reasoning
    )
    authorizer = source.authorized_by
    with operation_lock(project_id):
        store.require_project_accepts_new_work(project_id)
        if not store.is_project_member(project_id, authorizer.user_id):
            raise ValueError("The human who sent this Work turn is no longer a project member.")
        try:
            require_graph_edit_admission(store, project_id, target)
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc
        state = service.history.state()
        node = state.nodes.get(start.node_id)
        if not isinstance(node, Experiment):
            raise ValueError(f"Node {start.node_id!r} is not an Experiment on this graph.")
        if start.invocation_limit is not None and start.invocation_limit > node.invocation_ceiling:
            raise ValueError(
                "The invocation limit exceeds this Experiment's limit of "
                f"{node.invocation_ceiling}."
            )
        source_request = RunRequest.model_validate(source.request)
        supplied = RunRequest(
            chat_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:work-experiment:{start.episode_id}")),
            message=start.goal,
            provider=start.provider,
            model=start.model,
            reasoning=reasoning,
            run_truth_scope=source_request.run_truth_scope,
            started_from_operation_id=start.source_operation_id,
        )
        record = start_fresh_experiment(
            tasks,
            service,
            project_id,
            supplied,
            node=node,
            state=state,
            invocation_ceiling=start.invocation_limit or node.invocation_ceiling,
            authorized_by=authorizer,
            ensure_graph_target=partial(ensure_episode_graph_target, catalog=catalog),
            episode_id=start.episode_id,
        )
    # Leaving deletes membership and then fences the member's episodes; checking
    # again after admission fences one that a concurrent leave's sweep missed.
    if not store.is_project_member(project_id, authorizer.user_id):
        fence_episodes_for_departed_member(store, tasks, project_id, authorizer.user_id)
    return record


@router.post("/api/projects/{project_id}/experiments/{node_id:path}/watchers/stop")
def stop_experiment_watchers(
    project_id: str,
    node_id: str,
    *,
    catalog: CatalogDependency,
) -> list[dict[str, object]]:
    """Reject the retired bulk watcher control in favor of graceful Stop loop."""

    require_registered_project(catalog, project_id)
    raise HTTPException(
        status_code=409,
        detail="This control was retired. Use Stop loop for the current Experiment episode.",
    )


def stop_bound_experiment_episode(
    project_id: str,
    episode: EpisodeRecord,
    *,
    store: AppStore,
    catalog: ProjectCatalog,
    initiated_by: str | None = None,
) -> ExperimentControlState:
    """Stop one exact current loop against the graph target it actually controls."""

    node_id = episode.control_node_id
    if episode.mode != "experiment_loop" or node_id is None:
        raise HTTPException(status_code=409, detail="This is not an Experiment-loop episode.")
    if episode.graph_target.kind == "branch":
        route = store.auto_research_child_experiment(episode.episode_id)
        if route is not None and not child_experiment_on_owned_branch(store, episode):
            raise HTTPException(
                status_code=409,
                detail="The branch Experiment lost its Auto-research parent binding.",
            )
    runtime = store.experiment_loop_runtime(
        project_id,
        node_id,
        graph_target=episode.graph_target,
    )
    if runtime.episode_id != episode.episode_id:
        raise HTTPException(
            status_code=409,
            detail="Only the current exact Experiment episode can be stopped.",
        )
    try:
        store.request_experiment_loop_stop(
            project_id,
            node_id,
            episode_id=episode.episode_id,
            graph_target=episode.graph_target,
            initiated_by=initiated_by,
        )
    except EpisodeNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        main_service = get_project_writer(catalog, project_id)
        target_service = (
            main_service
            if episode.graph_target.kind == "main"
            else main_service.for_graph_target(
                episode.graph_target,
                expected_episode_id=store.branch_owner(
                    project_id, episode.graph_target.branch_id or ""
                ),
                initialize=False,
            )
        )
        materialization = target_service.history.current_materialization()
        state = materialization.state
        head = target_service.history.head_ref(materialization)
        if head.target != episode.graph_target:
            raise ValueError("The Experiment episode no longer resolves to its exact graph target.")
        if not isinstance(state.nodes.get(node_id), Experiment):
            raise ValueError("Experiment not found")
        _, control = experiment_control(
            store,
            project_id,
            state,
            node_id,
            graph_target=episode.graph_target,
        )
        return control
    except (HTTPException, KeyError, OSError, StateUnavailable, ValueError):
        # Stop is already durable. A display failure cannot undo its acceptance;
        # return its operational receipt with graph readiness explicitly unknown.
        runtime = store.experiment_loop_runtime(
            project_id, node_id, graph_target=episode.graph_target
        )
        return ExperimentControlState(
            ready=False,
            reasons=["Stop accepted; graph readiness is unavailable."],
            episode_id=runtime.episode_id,
            invocations_used=runtime.invocations_used,
            invocation_ceiling=episode.invocation_ceiling,
            invocations_remaining=max(0, episode.invocation_ceiling - runtime.invocations_used),
            paused=runtime.paused,
            active=runtime.active,
            operational=experiment_operational_state(runtime),
        )


def continue_experiment_episode(
    project_id: str,
    source: EpisodeRecord,
    *,
    invocation_ceiling: int,
    request_id: str,
    authorized_by: AuthorizedHuman,
    catalog: ProjectCatalog,
    store: AppStore,
    background_tasks: BackgroundAgentTasks,
) -> EpisodeRecord:
    """Add turns to an ended Experiment episode by resuming its bound session.

    The continuation is a new episode with its own ceiling, invocation 1 of
    which resumes the source's native session inside the source's stage. It
    reuses the Run path: the source's last turn request, re-pinned to the
    current graph revision and Decision bundle, and any pending watcher group
    is delivered with it. The caller holds the Experiment operation lock.
    """

    node_id = source.control_node_id
    if source.mode != "experiment_loop" or node_id is None:
        raise HTTPException(status_code=409, detail="This is not an Experiment-loop episode.")
    existing = store.episode_continuation(source.episode_id)
    if existing is not None:
        if existing.continuation_request_id != request_id:
            raise HTTPException(status_code=409, detail="This episode has already been continued.")
        return existing
    experiment = store.experiment_episode(source.episode_id)
    if (
        experiment is None
        or not experiment.session_bound
        or not experiment.native_session_id
        or not experiment.stage_root
        or not experiment.chat_id
        or experiment.last_turn_operation_id is None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This episode never committed a native session to continue. "
                "Start a new episode instead."
            ),
        )
    last_turn = store.agent_task(experiment.last_turn_operation_id)
    if last_turn is None:
        raise HTTPException(status_code=409, detail="The episode lost its last turn record.")
    main_service = get_project_writer(catalog, project_id)
    target_service = (
        main_service
        if source.graph_target.kind == "main"
        else main_service.for_graph_target(
            source.graph_target,
            expected_episode_id=store.branch_owner(project_id, source.graph_target.branch_id or ""),
        )
    )
    state = target_service.history.state()
    node = state.nodes.get(node_id)
    if not isinstance(node, Experiment):
        raise HTTPException(status_code=404, detail="Experiment not found")
    _, control = experiment_control(
        store,
        project_id,
        state,
        node_id,
        graph_target=source.graph_target,
    )
    if not control.ready:
        raise HTTPException(status_code=409, detail=" ".join(control.reasons))
    pending_group = store.completed_experiment_watcher_group(
        project_id,
        node_id,
        graph_target=source.graph_target,
    )
    episode_id = str(uuid.uuid4())
    request = RunRequest.model_validate(last_turn.request).model_copy(
        update={
            "message": (
                f"Continue the bounded Experiment-loop for {node_id} in this same session "
                "with the additional turns the human authorized."
            ),
            "browser_requested": source.browser_requested,
            "session_id": experiment.native_session_id,
            "chat_id": experiment.chat_id,
            "trigger": "experiment_run",
            # The member pressing Add N turns starts this episode, not the source's chat.
            "started_from_operation_id": None,
            "control_revision": state.revision,
            "control_episode_id": episode_id,
            "control_invocation": 1,
            "control_invocation_ceiling": invocation_ceiling,
            "control_decision_bundle": control.governing_decisions,
            "control_completion_criteria": list(node.completion_criteria),
            "watcher_ids": [item.watcher_id for item in pending_group or []],
            "invoked_workflow_ids": [],
            "invoked_skill_ids": [],
        }
    )
    request = target_service.resolve_compute_request(request)
    # The saved stage is frozen on one machine; the worker checks the alias
    # against it. A repointed alias must refuse here, before the source is chained.
    try:
        current_host = provider_login_host(target_service.manifest, request.run_on)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if current_host != (experiment.stage_host or ""):
        raise HTTPException(
            status_code=409,
            detail=(
                "The saved stage is not on the machine its execution alias names now, so the "
                "session cannot be resumed. Start a new episode instead."
            ),
        )
    try:
        start_experiment_continuation(
            background_tasks,
            project_id,
            request,
            source=source,
            authorized_by=authorized_by,
            stage_host=experiment.stage_host,
            stage_root=experiment.stage_root,
            continuation_request_id=request_id,
        )
    except AgentTaskAdmissionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    continuation = store.episode(episode_id)
    if continuation is None:
        raise HTTPException(status_code=409, detail="The continuation was not recorded.")
    return continuation


# Register this after ``.../watchers/stop``: ``{node_id:path}`` is greedy, so
# the stop-loop route would otherwise swallow that path.
@router.post("/api/projects/{project_id}/experiments/{node_id:path}/stop")
def stop_experiment_loop(
    project_id: str,
    node_id: str,
    request: Request,
    episode_id: str | None = Query(default=None),
    branch_id: str | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityAccessDependency,
    experiment_operation_lock: ExperimentOperationLockDependency,
) -> dict[str, object]:
    """Finish the current turn, then disable automatic continuation.

    The stop is durable before this returns, so no unclaimed watcher can win a
    wake afterwards. It never cancels the live task, kills external work,
    deletes a watcher, or changes what the Experiment means, and calling it
    again changes nothing.
    """

    actor = identity_access.require_patch_capable_identity(request)
    project_id = catalog.resolve_project_id(project_id)
    if branch_id is not None:
        try:
            canonical_branch_id(branch_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Episode graph not found") from exc
    target = GraphTargetRef(kind="branch", branch_id=branch_id) if branch_id else GraphTargetRef()
    with experiment_operation_lock(project_id):
        if episode_id is None:
            runtime = store.experiment_loop_runtime(
                project_id,
                node_id,
                graph_target=target,
            )
            episode = store.episode(runtime.episode_id) if runtime.episode_id is not None else None
            if episode is None or episode.project_id != project_id:
                raise HTTPException(status_code=404, detail="Experiment episode not found")
            if episode.graph_target != target:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This Experiment runs on an episode graph branch; select its exact "
                        "episode before stopping the loop."
                    ),
                )
        else:
            episode = _episode_for_http(store, catalog, project_id, episode_id)
            if episode.control_node_id != node_id:
                raise HTTPException(status_code=404, detail="Experiment episode not found")
            if branch_id is not None and episode.graph_target != target:
                raise HTTPException(status_code=404, detail="Experiment episode not found")
        control = stop_bound_experiment_episode(
            project_id,
            episode,
            store=store,
            catalog=catalog,
            initiated_by=f"human:{actor.user_id}",
        )
    return control.model_dump(mode="json")


__all__ = [
    "router",
    "run_experiment",
    "stop_bound_experiment_episode",
    "stop_experiment_loop",
    "stop_experiment_watchers",
]
