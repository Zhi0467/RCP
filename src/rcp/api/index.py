from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from rcp.agents import AgentLauncher
from rcp.api.dependencies import (
    get_catalog,
    get_experiment_operation_lock,
    get_identity_access,
    get_launcher,
    get_project_display_cache,
    get_project_service,
    get_setup,
    get_store,
    require_project_membership,
)
from rcp.api.episode_branches import graph_branch_summary
from rcp.api.episodes import (
    EpisodeResponse,
    serialize_episode,
    space_auto_research_episode_projection,
)
from rcp.api.experiment_controls import ExperimentControlResponse, _experiment_control_response
from rcp.api.identity import IdentityAccess
from rcp.api.team_shell_protocol import acknowledge_team_shell_protocol
from rcp.core.models import Experiment, GraphState
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.keyed_locks import KeyedLocks
from rcp.projects import TEAM_PROJECT_DELETE_UNAVAILABLE_REASON, ProjectCatalog, ProjectDisplayCache
from rcp.providers import PROVIDER_IDS
from rcp.service import ProjectService
from rcp.setup import ProjectSetupManager, ProjectSetupRequest, SshRepositoryBrowseRequest
from rcp.skill_registry import SkillKind, official_registry
from rcp.sources import (
    REMOTE_SOURCE_CACHE_LIMITS,
    SESSION_SLICE_CACHE_LIMITS,
    RebuildableCache,
    discover_project_cache_roots,
    legacy_shared_cache_roots,
)
from rcp.storage import (
    AppStore,
    AutoResearchSpaceRunProjectionSnapshot,
    ExperimentControlProjectionSnapshot,
    ExperimentEpisodeProjectionSnapshot,
    ExperimentLoopRuntime,
    ProjectActiveTaskConflict,
)
from rcp.transport import StateUnavailable

router = APIRouter()
membership_router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
DisplayCacheDependency = Annotated[ProjectDisplayCache, Depends(get_project_display_cache)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
LauncherDependency = Annotated[AgentLauncher, Depends(get_launcher)]
SetupDependency = Annotated[ProjectSetupManager, Depends(get_setup)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
ExperimentOperationLockDependency = Annotated[
    KeyedLocks,
    Depends(get_experiment_operation_lock),
]


def _require_personal_project_entry(store: StoreDependency) -> None:
    if store.space_kind == "team":
        raise HTTPException(
            status_code=409,
            detail=(
                "Existing-checkout setup belongs to a personal space. "
                "Create a team-project provisioning request instead."
            ),
        )


PersonalProjectEntryDependency = Annotated[None, Depends(_require_personal_project_entry)]


class ProjectRegisterRequest(BaseModel):
    locator: str


class ExperimentLoopIndexEntryResponse(BaseModel):
    project_id: str
    project_name: str
    project_reachable: bool | None
    graph_target: GraphTargetRef
    graph_head: GraphHeadRef | None
    parent_episode_id: str | None
    parent_watching: bool
    node: Experiment
    control: ExperimentControlResponse
    episode: EpisodeResponse


SPACE_RUNS_COMPLETED_TTL = timedelta(days=7)
SpaceRunMode = Literal["experiment_loop", "auto_research"]
SpaceRunSection = Literal["needs_action", "completed"]
SpaceRunTone = Literal[
    "running",
    "waiting",
    "degraded",
    "stopping",
    "stopped",
    "actionable",
    "completed",
]


class SpaceRunIndexEntryResponse(BaseModel):
    """One backend-decided run summary for the space project index."""

    episode_id: str
    project_id: str
    project_name: str
    project_reachable: bool | None
    mode: SpaceRunMode
    title: str
    graph_target: GraphTargetRef
    parent_episode_id: str | None
    experiment_id: str | None
    started_at: str
    last_activity_at: str
    health_label: str
    health_tone: SpaceRunTone
    run_section: SpaceRunSection


@router.get("/api/projects")
def projects(
    request: Request,
    response: Response,
    *,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> list[dict[str, object]]:
    acknowledge_team_shell_protocol(request, response)
    visible = store.member_project_ids(identity_access.acting_user(request).user_id)
    return [card for card in catalog.cards() if card["id"] in visible]


@router.get("/api/episodes", response_model=list[ExperimentLoopIndexEntryResponse])
def experiment_episodes(
    request: Request,
    mode: Literal["experiment_loop"] = Query(...),
    *,
    catalog: CatalogDependency,
    project_display_cache: DisplayCacheDependency,
    identity_access: IdentityDependency,
    store: StoreDependency,
    experiment_operation_lock: ExperimentOperationLockDependency,
) -> list[ExperimentLoopIndexEntryResponse]:
    # An unfiltered answer would publish research and not just project names.
    # Start from durable loop parents rather than graph nodes: a branch may
    # create an Experiment that does not exist on main at all.
    visible = store.member_project_ids(identity_access.acting_user(request).user_id)
    return _experiment_episode_entries(
        catalog=catalog,
        project_display_cache=project_display_cache,
        store=store,
        experiment_operation_lock=experiment_operation_lock,
        visible=visible,
    )


def _experiment_episode_entries(
    *,
    catalog: ProjectCatalog,
    project_display_cache: ProjectDisplayCache,
    store: AppStore,
    experiment_operation_lock: KeyedLocks,
    visible: set[str],
) -> list[ExperimentLoopIndexEntryResponse]:
    """Build the exact Experiment index once for both index projections."""

    entries: list[ExperimentLoopIndexEntryResponse] = []
    branch_summary = partial(graph_branch_summary, store=store, catalog=catalog)
    for record in store.projects():
        if record.project_id not in visible:
            continue
        read_models = store.experiment_control_projection_snapshots(record.project_id)
        if not read_models:
            continue
        active_graph_watchers = store.active_graph_watchers(record.project_id)
        settle_ids = [
            experiment_id
            for experiment_id, read_model in read_models.items()
            if read_model.runtime.stop_requested
            and not read_model.runtime.stop_settled
            and not read_model.runtime.task_active
        ]
        if settle_ids:
            # This GET is the only index path that performs lifecycle repair.
            # Re-read under the same canonical project lock as Run/Stop so a
            # concurrent admission cannot race the quiescence decision.
            with experiment_operation_lock(record.project_id):
                read_models = store.experiment_control_projection_snapshots(record.project_id)
                for experiment_id, read_model in read_models.items():
                    runtime = read_model.runtime
                    episode_snapshot = read_model.episode
                    if (
                        runtime.stop_requested
                        and not runtime.stop_settled
                        and not runtime.task_active
                        and episode_snapshot is not None
                    ):
                        episode = episode_snapshot.episode
                        store.settle_experiment_loop_stop(
                            record.project_id,
                            experiment_id,
                            episode_id=episode.episode_id,
                            graph_target=episode.graph_target,
                        )
                read_models = store.experiment_control_projection_snapshots(record.project_id)

        current: list[
            tuple[
                ExperimentEpisodeProjectionSnapshot,
                ExperimentLoopRuntime,
                ExperimentControlProjectionSnapshot,
            ]
        ] = []
        for control_node_id, read_model in read_models.items():
            episode_snapshot = read_model.episode
            if episode_snapshot is None:
                continue
            runtime = read_model.runtime
            episode = episode_snapshot.episode
            if (
                episode.project_id != record.project_id
                or episode.mode != "experiment_loop"
                or episode.control_node_id != control_node_id
            ):
                raise ValueError("Experiment runtime does not identify its exact durable episode.")
            current.append((episode_snapshot, runtime, read_model))
        current.sort(
            key=lambda item: (item[0].episode.created_at, item[0].episode.episode_id),
            reverse=True,
        )

        cache_status, cached = catalog.cached_snapshot_status(record.project_id)
        if cache_status == "invalid" or (cache_status == "missing" and record.revision is not None):
            raise HTTPException(
                status_code=503,
                detail=f"Cached project snapshot is unavailable for {record.project_id}.",
            )
        grouped: dict[
            str,
            list[
                tuple[
                    ExperimentEpisodeProjectionSnapshot,
                    ExperimentLoopRuntime,
                    ExperimentControlProjectionSnapshot,
                ]
            ],
        ] = {}
        for episode_snapshot, runtime, read_model in current:
            grouped.setdefault(episode_snapshot.episode.graph_target.key, []).append(
                (episode_snapshot, runtime, read_model)
            )

        main_read_models = {
            episode_snapshot.episode.control_node_id: read_model
            for episode_snapshot, _runtime, read_model in grouped.get("main", [])
            if episode_snapshot.episode.control_node_id is not None
        }
        completed_cached = (
            project_display_cache.complete_cached_transition_control(
                record.project_id,
                cached,
                main_read_models,
            )
            if cached is not None
            else None
        )
        reachable = _cached_project_reachable(completed_cached)
        if record.reachable is False:
            reachable = False

        main_service: ProjectService | None = None
        for group in grouped.values():
            target = group[0][0].episode.graph_target
            graph_head = None
            if target.kind == "main":
                # ProjectDisplayCache is deliberately a main-only display snapshot.
                # Preserve its graph/runtime publication fence for ordinary loops;
                # branch state is always read through its exact history service.
                state = _cached_graph_state(completed_cached)
                if state is None:
                    continue
            else:
                if main_service is None:
                    main_service = get_project_service(catalog, record.project_id)
                try:
                    target_service = (
                        main_service
                        if target.kind == "main"
                        else main_service.for_graph_target(
                            target,
                            expected_episode_id=target.branch_id,
                        )
                    )
                    materialization = target_service.history.current_materialization()
                    state = materialization.state
                    graph_head = target_service.history.head_ref(materialization)
                except (KeyError, OSError, StateUnavailable, ValueError) as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                if graph_head.target != target:
                    raise ValueError(
                        "Experiment graph projection returned a different target head."
                    )

            for episode_snapshot, runtime, read_model in group:
                episode = episode_snapshot.episode
                node_id = episode.control_node_id
                assert node_id is not None
                node = state.nodes.get(node_id)
                if not isinstance(node, Experiment):
                    continue
                route = store.auto_research_child_experiment(episode.episode_id)
                parent_episode_id = route.auto_research_episode_id if route is not None else None
                parent_watching = parent_episode_id is not None and any(
                    watcher.origin_task_kind == "auto_research"
                    and watcher.episode_id == parent_episode_id
                    and watcher.graph_target == target
                    and watcher.condition.node_id == node.id
                    for watcher in active_graph_watchers
                )
                if target.kind == "branch" and parent_episode_id != target.branch_id:
                    raise ValueError(
                        "Branch-target Experiment lost its Auto-research parent identity."
                    )
                if target.kind == "main":
                    controls = (
                        completed_cached.get("experiment_control") if completed_cached else None
                    )
                    if not isinstance(controls, dict) or node.id not in controls:
                        raise ValueError("Completed project snapshot lost Experiment control.")
                    control = ExperimentControlResponse.model_validate(controls[node.id])
                    serialized_episode = control.episode
                    if (
                        serialized_episode is None
                        or serialized_episode.episode_id != episode.episode_id
                    ):
                        raise ValueError(
                            "Completed project control lost its exact Experiment episode."
                        )
                else:
                    serialized_episode = serialize_episode(
                        store,
                        record.project_id,
                        episode,
                        branch_summary=branch_summary,
                        projection_snapshot=episode_snapshot,
                    )
                    control = _experiment_control_response(
                        state,
                        node.id,
                        runtime,
                        serialized_episode,
                        latest_report_episode_id=read_model.latest_report_episode_id,
                    )
                entries.append(
                    ExperimentLoopIndexEntryResponse(
                        project_id=record.project_id,
                        project_name=record.name,
                        project_reachable=reachable,
                        graph_target=target,
                        graph_head=graph_head,
                        parent_episode_id=parent_episode_id,
                        parent_watching=parent_watching,
                        node=node,
                        control=control,
                        episode=serialized_episode,
                    )
                )
    return entries


@router.get("/api/space/runs", response_model=list[SpaceRunIndexEntryResponse])
def space_runs(
    request: Request,
    *,
    catalog: CatalogDependency,
    project_display_cache: DisplayCacheDependency,
    identity_access: IdentityDependency,
    store: StoreDependency,
    experiment_operation_lock: ExperimentOperationLockDependency,
) -> list[SpaceRunIndexEntryResponse]:
    """Publish current and recent episode parents across the visible space."""

    visible = store.member_project_ids(identity_access.acting_user(request).user_id)
    records = {
        record.project_id: record for record in store.projects() if record.project_id in visible
    }
    as_of = datetime.fromisoformat(store.now()).astimezone(UTC)
    completed_since = (as_of - SPACE_RUNS_COMPLETED_TTL).isoformat()
    entries = [
        _space_experiment_run(entry)
        for entry in _experiment_episode_entries(
            catalog=catalog,
            project_display_cache=project_display_cache,
            store=store,
            experiment_operation_lock=experiment_operation_lock,
            visible=visible,
        )
    ]
    for snapshot in store.auto_research_space_run_projection_snapshots(
        set(records),
        completed_since=completed_since,
    ):
        episode = snapshot.episode
        record = records[episode.project_id]
        entry = _space_auto_research_run(
            snapshot,
            project_name=record.name,
            project_reachable=record.reachable,
        )
        entries.append(entry)
    return sorted(
        (entry for entry in entries if _space_run_is_visible(entry, as_of=as_of)),
        key=lambda entry: (entry.started_at, entry.episode_id),
        reverse=True,
    )


def _space_experiment_run(
    entry: ExperimentLoopIndexEntryResponse,
) -> SpaceRunIndexEntryResponse:
    control = entry.control
    health_labels = {
        "starting": "Starting",
        "agent_active": "Agent active",
        "waiting_on_watchers": "Waiting on watchers",
        "degraded": "Watcher degraded",
        "stopping": "Stopping gracefully",
        "wrapping_up": "Wrapping up",
        "failed": "Failed",
        "human_stopped": "Human-stopped",
        "paused_at_limit": "Paused at invocation limit",
        "needs_action": "Needs action",
        "completed": "Completed",
    }
    health_tones: dict[str, SpaceRunTone] = {
        "starting": "running",
        "agent_active": "running",
        "waiting_on_watchers": "waiting",
        "degraded": "degraded",
        "stopping": "stopping",
        "wrapping_up": "running",
        "failed": "degraded",
        "human_stopped": "stopped",
        "paused_at_limit": "actionable",
        "needs_action": "actionable",
        "completed": "completed",
    }
    return SpaceRunIndexEntryResponse(
        episode_id=entry.episode.episode_id,
        project_id=entry.project_id,
        project_name=entry.project_name,
        project_reachable=entry.project_reachable,
        mode="experiment_loop",
        title=entry.node.title,
        graph_target=entry.graph_target,
        parent_episode_id=entry.parent_episode_id,
        experiment_id=entry.node.id,
        started_at=entry.episode.created_at,
        last_activity_at=(
            entry.episode.ended_at or entry.episode.updated_at
            if control.run_section == "completed"
            else entry.control.operational.current_last_activity_at or entry.episode.updated_at
        ),
        health_label=health_labels[control.health],
        health_tone=health_tones[control.health],
        run_section="completed" if control.run_section == "completed" else "needs_action",
    )


def _space_auto_research_run(
    snapshot: AutoResearchSpaceRunProjectionSnapshot,
    *,
    project_name: str,
    project_reachable: bool | None,
) -> SpaceRunIndexEntryResponse:
    episode = snapshot.episode
    health, run_section, last_activity_at = space_auto_research_episode_projection(snapshot)
    health_labels = {
        "starting": "Starting",
        "active": "Active",
        "recovering": "Recovering",
        "needs_action": "Needs action",
        "stopping": "Stopping gracefully",
        "wrapping_up": "Wrapping up",
        "completed": "Completed",
        "stopped": "Stopped",
        "failed": "Failed",
    }
    health_tones: dict[str, SpaceRunTone] = {
        "starting": "running",
        "active": "running",
        "recovering": "waiting",
        "needs_action": "actionable",
        "stopping": "stopping",
        "wrapping_up": "running",
        "completed": "completed",
        "stopped": "stopped",
        "failed": "degraded",
    }
    return SpaceRunIndexEntryResponse(
        episode_id=episode.episode_id,
        project_id=episode.project_id,
        project_name=project_name,
        project_reachable=project_reachable,
        mode="auto_research",
        title="Auto-research",
        graph_target=episode.graph_target,
        parent_episode_id=None,
        experiment_id=None,
        started_at=episode.created_at,
        last_activity_at=last_activity_at,
        health_label=health_labels[health],
        health_tone=health_tones[health],
        run_section=run_section,
    )


def _space_run_is_visible(entry: SpaceRunIndexEntryResponse, *, as_of: datetime) -> bool:
    if entry.run_section == "needs_action":
        return True
    completed_at = datetime.fromisoformat(entry.last_activity_at).astimezone(UTC)
    return completed_at >= as_of - SPACE_RUNS_COMPLETED_TTL


@router.get("/api/space/users")
def space_users(
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> list[dict[str, object]]:
    """Who is enrolled in this space, so Invite can offer them by name.

    Names are not unique, so the control resolves to the durable id.
    """

    identity_access.acting_user(request)
    return [
        {"user_id": user.user_id, "display_name": user.display_name}
        for user in store.space_users()
        if user.removal_started_at is None and user.removed_at is None
    ]


# S122. Deliberately *outside* the membership router: you are not a member
# of the project you are being invited to, and Inbox lives inside the
# project shell, which is unreachable before membership.
@router.get("/api/project-invitations")
def project_invitations_for_me(
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> list[dict[str, object]]:
    user = identity_access.acting_user(request)
    names = {item.user_id: item.display_name for item in store.space_users()}
    entries = []
    for invitation in store.pending_project_invitations(user.user_id):
        record = store.project(invitation.project_id)
        if record is None:
            continue
        entries.append(
            {
                "invitation_id": invitation.invitation_id,
                "project_id": invitation.project_id,
                "project_name": record.name,
                "space_name": store.space_name,
                "invited_by": invitation.invited_by,
                "invited_by_name": names.get(invitation.invited_by),
                "created_at": invitation.created_at,
            }
        )
    return entries


@router.post("/api/project-invitations/{invitation_id}/{response}")
def answer_project_invitation(
    invitation_id: str,
    response: Literal["accept", "decline"],
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> dict[str, object]:
    user = identity_access.acting_user(request)
    try:
        answered = store.answer_project_invitation(
            invitation_id,
            invited_user_id=user.user_id,
            response="accepted" if response == "accept" else "declined",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Invitation not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return answered.model_dump(mode="json")


@router.get("/api/providers")
def providers(
    refresh: bool = False,
    *,
    launcher: LauncherDependency,
) -> list[dict[str, object]]:
    """The registry probed on this machine, for surfaces with no project yet.

    Project setup picks agent defaults before any manifest exists, so it has
    no per-machine readiness to read. Remote hosts are reported by preflight.
    """

    return [
        launcher.readiness(provider, refresh=refresh).model_dump(mode="json")
        for provider in PROVIDER_IDS
    ]


@router.post("/api/projects")
def register_project(
    body: ProjectRegisterRequest,
    request: Request,
    *,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    _personal_entry: PersonalProjectEntryDependency,
) -> dict[str, object]:
    # Deliberately not require_patch_capable_identity: creating a project
    # does not demand a display name, and S01/S112/S116 rely on that.
    try:
        record = catalog.register(
            body.locator,
            seat_member=identity_access.acting_user(request).user_id,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return catalog.card(record.project_id)


@membership_router.delete("/api/projects/{project_id}")
def delete_project(
    project_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> dict[str, object]:
    if store.space_kind == "team":
        raise HTTPException(status_code=409, detail=TEAM_PROJECT_DELETE_UNAVAILABLE_REASON)
    try:
        return catalog.delete(project_id).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except ProjectActiveTaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, RuntimeError, StateUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/project-setup/preflight")
def preflight_project(
    body: ProjectSetupRequest,
    *,
    setup: SetupDependency,
    _personal_entry: PersonalProjectEntryDependency,
) -> dict[str, object]:
    try:
        return setup.preflight(body).model_dump(mode="json")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/project-setup/ssh-paths")
def browse_project_setup_ssh_paths(
    body: SshRepositoryBrowseRequest,
    *,
    setup: SetupDependency,
    _personal_entry: PersonalProjectEntryDependency,
) -> dict[str, object]:
    try:
        return setup.browse_ssh_repository_paths(body).model_dump(mode="json")
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/project-setup/create")
def create_project(
    body: ProjectSetupRequest,
    request: Request,
    *,
    identity_access: IdentityDependency,
    setup: SetupDependency,
    _personal_entry: PersonalProjectEntryDependency,
) -> dict[str, object]:
    try:
        return setup.create(
            body,
            seat_member=identity_access.acting_user(request).user_id,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@membership_router.delete("/api/projects/{project_id}/caches/all")
def clear_all_rebuildable_caches(
    project_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> dict[str, object]:
    if store.space_kind != "personal":
        raise HTTPException(
            status_code=409,
            detail="Clearing every project's cache is available only in a personal space.",
        )
    if store.has_any_active_agent_task():
        raise HTTPException(
            status_code=409,
            detail="All project caches cannot be cleared while any agent task is active.",
        )
    current_service = get_project_service(catalog, project_id)

    project_roots = discover_project_cache_roots(catalog.data_dir)
    for source_root, slice_root in project_roots:
        RebuildableCache(
            source_root,
            REMOTE_SOURCE_CACHE_LIMITS,
            layout="files",
        ).clear()
        RebuildableCache(
            slice_root,
            SESSION_SLICE_CACHE_LIMITS,
            layout="directories",
        ).clear()
    for record in store.projects():
        service = catalog.loaded_service(record.project_id)
        if service is not None:
            service.invalidate_source_index()

    legacy_source_root, legacy_slice_root = legacy_shared_cache_roots(catalog.data_dir)
    RebuildableCache(
        legacy_source_root,
        REMOTE_SOURCE_CACHE_LIMITS,
        layout="files",
    ).clear()
    RebuildableCache(
        legacy_slice_root,
        SESSION_SLICE_CACHE_LIMITS,
        layout="directories",
    ).clear()
    return current_service.indexer.cache_metrics().model_dump(mode="json")


@router.get("/api/skills/{kind}/{package_id}")
def read_skill_package(kind: str, package_id: str) -> dict[str, object]:
    """The official package's own text, for the read-only Settings inspector."""

    if kind not in {"skill", "workflow"}:
        raise HTTPException(status_code=404, detail="Package not found")
    registry = official_registry()
    try:
        package = registry.package(cast(SkillKind, kind), package_id)
        body = registry.package_body(cast(SkillKind, kind), package_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**package.catalog_entry(), "body": body}


def _cached_graph_state(snapshot: dict[str, object] | None) -> GraphState | None:
    if snapshot is None:
        return None
    try:
        return GraphState.model_validate(snapshot["graph"])
    except (KeyError, TypeError, ValueError):
        return None


def _cached_project_reachable(snapshot: dict[str, object] | None) -> bool | None:
    if snapshot is None:
        return None
    canonical = snapshot.get("canonical_state")
    if not isinstance(canonical, dict):
        return None
    reachable = canonical.get("reachable")
    return reachable if isinstance(reachable, bool) else None


__all__ = [
    "ExperimentLoopIndexEntryResponse",
    "answer_project_invitation",
    "clear_all_rebuildable_caches",
    "create_project",
    "delete_project",
    "experiment_episodes",
    "membership_router",
    "preflight_project",
    "project_invitations_for_me",
    "projects",
    "providers",
    "read_skill_package",
    "register_project",
    "router",
    "space_users",
]
