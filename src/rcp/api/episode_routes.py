from __future__ import annotations

import hashlib
import logging
from functools import partial
from typing import Annotated, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from rcp.api.dependencies import (
    get_background_tasks,
    get_catalog,
    get_experiment_operation_lock,
    get_identity_access,
    get_project_service,
    get_store,
    project_write_admission,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.episode_timeline import build_episode_timeline, episode_timeline_text
from rcp.api.episode_timeline_models import EpisodeTimelineResponse, EpisodeTimelineText
from rcp.api.episodes import (
    ContinueEpisodeBody,
    EpisodeMessageBody,
    EpisodeResponse,
    StartEpisodeBody,
    _episode_for_http,
    serialize_episode,
    serialize_episodes,
)
from rcp.api.experiments import continue_experiment_episode, stop_bound_experiment_episode
from rcp.api.identity import IdentityAccess
from rcp.artifact_comments import comment_panel, selection_frame_addon
from rcp.artifact_views import artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor, html_preview_document
from rcp.background import BackgroundAgentTasks
from rcp.keyed_locks import KeyedLocks
from rcp.limits import (
    REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS,
    REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
)
from rcp.projects import ProjectCatalog
from rcp.runs.auto_research import AutoResearchStartRequest, settle_auto_research_stop
from rcp.runs.auto_research_admission import (
    continue_auto_research,
    start_auto_research,
    stop_auto_research,
)
from rcp.runs.auto_research_delivery import (
    deliver_pending_auto_research_lifecycle,
    deliver_pending_auto_research_mail,
    record_auto_research_message,
)
from rcp.runs.branch_merge_admission import settle_agentless_merge, start_branch_merge
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.episodes.isolation import auto_research_code_worktree_eligible
from rcp.runs.episodes.merge import (
    CleanupEpisodeBody,
    MergeEpisodeBody,
    MergePreview,
    cleanup_episode,
    merge_episode,
    merge_owner,
    merge_preview,
    reconcile_episode_merge,
)
from rcp.service import ProjectService, RunRequest
from rcp.storage import AppStore, AutoResearchMessageRecord, EpisodeNotRunning
from rcp.storage.conversation_worktrees import UnfinishedEpisodeJobs
from rcp.transport import StateUnavailable

from .episode_branches import (
    ensure_episode_graph_target,
    graph_branch_summaries,
    graph_branch_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
BackgroundTasksDependency = Annotated[BackgroundAgentTasks, Depends(get_background_tasks)]
ExperimentOperationLockDependency = Annotated[
    KeyedLocks,
    Depends(get_experiment_operation_lock),
]


class ArchiveEpisodeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    archived: bool


def _branch_summaries(
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> partial:
    return partial(
        graph_branch_summaries,
        store=store,
        catalog=catalog,
        refresh_max_age_seconds=refresh_max_age_seconds,
    )


def _branch_summary(
    store: AppStore,
    catalog: ProjectCatalog,
    refresh_max_age_seconds: float = REMOTE_STATE_RECONCILE_WINDOW_SECONDS,
) -> partial:
    return partial(
        graph_branch_summary,
        store=store,
        catalog=catalog,
        refresh_max_age_seconds=refresh_max_age_seconds,
    )


@router.get(
    "/api/projects/{project_id}/episodes",
    response_model=list[EpisodeResponse],
)
def episodes(
    project_id: str,
    mode: Literal["auto_research", "experiment_loop"] | None = None,
    episode_id: str | None = None,
    include_archived_branches: bool = False,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> list[EpisodeResponse]:
    require_registered_project(catalog, project_id)
    if episode_id is not None:
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        if mode is not None and episode.mode != mode:
            raise HTTPException(status_code=404, detail="Episode not found")
        return [
            serialize_episode(
                store,
                project_id,
                episode,
                branch_summary=_branch_summary(
                    store, catalog, REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS
                ),
            )
        ]
    return serialize_episodes(
        store,
        project_id,
        mode=mode,
        include_archived_branches=include_archived_branches,
        branch_summaries=_branch_summaries(
            store, catalog, REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS
        ),
    )


@router.get(
    "/api/projects/{project_id}/episodes/{episode_id}/timeline",
    response_model=EpisodeTimelineResponse,
)
def episode_timeline(
    project_id: str,
    episode_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> EpisodeTimelineResponse:
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    return build_episode_timeline(store, episode)


@router.get(
    "/api/projects/{project_id}/episodes/{episode_id}/timeline/text/{text_ref}",
    response_model=EpisodeTimelineText,
)
def episode_timeline_body(
    project_id: str,
    episode_id: str,
    text_ref: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> EpisodeTimelineText:
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    result = episode_timeline_text(store, episode, text_ref)
    if result is None:
        raise HTTPException(status_code=404, detail="Timeline text not found")
    return result


@router.post(
    "/api/projects/{project_id}/episodes",
    response_model=EpisodeResponse,
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def start_episode(
    project_id: str,
    body: StartEpisodeBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
) -> EpisodeResponse:
    authorized_by = identity_access.require_patch_capable_identity(request)
    service = get_project_service(catalog, project_id)
    try:
        start_request = _resolved_auto_research_start_request(service, body)
        if body.code_worktree is None:
            start_request = start_request.model_copy(
                update={
                    "code_worktree": auto_research_code_worktree_eligible(
                        store, project_id, start_request
                    )
                }
            )
        service.history.require_writable()
        graph_base_head = service.history.head_ref()
        episode, _ = start_auto_research(
            background_tasks,
            project_id,
            start_request,
            authorized_by=authorized_by,
            graph_base_head=graph_base_head,
            ensure_graph_target=partial(
                ensure_episode_graph_target,
                catalog=catalog,
            ),
        )
        return serialize_episode(
            store,
            project_id,
            episode,
            branch_summary=_branch_summary(store, catalog),
        )
    except ValueError as exc:
        live = any(
            episode.mode == "auto_research"
            and episode.status in {"queued", "running", "stopping", "wrapping_up"}
            for episode in store.episodes(project_id)
        )
        status = 409 if live else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/archive",
    response_model=EpisodeResponse,
    dependencies=[Depends(require_project_write_admission)],
)
def archive_episode(
    project_id: str,
    episode_id: str,
    body: ArchiveEpisodeBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> EpisodeResponse:
    actor = identity_access.require_patch_capable_identity(request)
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    try:
        archive_state = store.set_episode_archived(
            episode.project_id, episode_id, actor.user_id, archived=body.archived
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_episode(
        store,
        episode.project_id,
        episode,
        archive_state=archive_state,
        branch_summary=_branch_summary(store, catalog),
    )


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/stop",
    response_model=EpisodeResponse,
)
def stop_episode(
    project_id: str,
    episode_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    experiment_operation_lock: ExperimentOperationLockDependency,
) -> EpisodeResponse:
    actor = identity_access.require_patch_capable_identity(request)
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    if episode.mode == "auto_research":
        try:
            stop_auto_research(
                background_tasks, episode.episode_id, initiated_by=f"human:{actor.user_id}"
            )
            settle_auto_research_stop(store, episode.episode_id)
        except EpisodeNotRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    elif episode.mode == "experiment_loop":
        with experiment_operation_lock(project_id):
            stop_bound_experiment_episode(
                project_id,
                episode,
                store=store,
                catalog=catalog,
                initiated_by=f"human:{actor.user_id}",
            )
    else:
        raise HTTPException(status_code=409, detail="This episode cannot be stopped.")
    current = store.episode(episode.episode_id)
    if current is None:
        raise RuntimeError("The stopped episode could not be reloaded.")
    return serialize_episode(
        store,
        project_id,
        current,
        branch_summary=_branch_summary(store, catalog),
    )


def _merge_refusal(exc: ValueError) -> dict:
    detail = {"code": getattr(exc, "code", str(exc).split(":", 1)[0]), "message": str(exc)}
    # A pause, not a refusal: the Web lists the jobs and the human can merge anyway.
    if isinstance(exc, UnfinishedEpisodeJobs):
        detail["jobs"] = [job.model_dump(mode="json") for job in exc.jobs]
    return detail


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/merge",
    response_model=EpisodeResponse,
    status_code=202,
)
def merge_episode_branch(
    project_id: str,
    episode_id: str,
    request: Request,
    body: MergeEpisodeBody | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
) -> EpisodeResponse:
    authorized_by = identity_access.require_patch_capable_identity(request)
    member = _episode_for_http(store, catalog, project_id, episode_id)
    episode = merge_owner(store, member)
    service = get_project_service(catalog, project_id)
    try:
        with project_write_admission(project_id, request):
            service.history.require_writable()
            reconciled = reconcile_episode_merge(service, store, episode)
            if reconciled is None:
                merge_episode(
                    service,
                    store,
                    episode,
                    body or MergeEpisodeBody(),
                    authorized_by=authorized_by,
                    dispatch_graph=lambda operation_id, launch=True: start_branch_merge(
                        background_tasks,
                        project_id,
                        _resolved_branch_merge_request(
                            service,
                            episode.episode_id,
                            run_on=(
                                isolation.worktree.machine
                                if (
                                    isolation := store.episode_isolation(
                                        project_id, episode.episode_id
                                    )
                                )
                                and isolation.worktree
                                else None
                            ),
                        ),
                        authorized_by=authorized_by,
                        operation_id=operation_id,
                        launch=launch,
                    ),
                )
        state = store.episode_isolation_state(project_id, episode.episode_id)
        if state and state.merge_attempt:
            settle_agentless_merge(background_tasks, service, state.merge_attempt.attempt_id)
        current = store.episode(member.episode_id)
        if current is None:
            raise RuntimeError("The branch merge episode could not be reloaded.")
        return serialize_episode(
            store,
            project_id,
            current,
            branch_summary=_branch_summary(store, catalog),
        )
    except ValueError as exc:
        state = store.episode_isolation_state(project_id, episode.episode_id)
        if state and state.merge_attempt:
            settle_agentless_merge(background_tasks, service, state.merge_attempt.attempt_id)
        raise HTTPException(status_code=409, detail=_merge_refusal(exc)) from exc


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/cleanup",
    response_model=EpisodeResponse,
    dependencies=[Depends(require_project_write_admission)],
)
def cleanup_episode_branch(
    project_id: str,
    episode_id: str,
    body: CleanupEpisodeBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> EpisodeResponse:
    actor = identity_access.require_patch_capable_identity(request)
    member = _episode_for_http(store, catalog, project_id, episode_id)
    owner = merge_owner(store, member)
    try:
        cleanup_episode(
            get_project_service(catalog, project_id), store, owner, body, authorized_by=actor
        )
        return serialize_episode(
            store, project_id, member, branch_summary=_branch_summary(store, catalog)
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_merge_refusal(exc)) from exc


@router.get(
    "/api/projects/{project_id}/episodes/{episode_id}/merge-preview", response_model=MergePreview
)
def episode_merge_preview(
    project_id: str,
    episode_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    target_branch: str | None = None,
) -> MergePreview:
    actor = identity_access.require_patch_capable_identity(request)
    owner = merge_owner(store, _episode_for_http(store, catalog, project_id, episode_id))
    try:
        return merge_preview(
            get_project_service(catalog, project_id),
            store,
            owner,
            authorized_by=actor,
            target_branch=target_branch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_merge_refusal(exc)) from exc


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/continue",
    response_model=EpisodeResponse,
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def continue_episode(
    project_id: str,
    episode_id: str,
    body: ContinueEpisodeBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
) -> EpisodeResponse | JSONResponse:
    """Add turns to an ended episode as a continuation on its branch and session.

    One control for both modes. Storage refuses a source that is live, already
    continued, merging, or occupied by a newer live episode; a repeated request
    id returns the continuation it already created with status 200.
    """

    authorized_by = identity_access.require_patch_capable_identity(request)
    source = _episode_for_http(store, catalog, project_id, episode_id)
    if source.status not in {"completed", "failed", "needs_action", "stopped"}:
        raise HTTPException(status_code=409, detail="Only an ended episode can be continued.")
    existing = store.episode_continuation(source.episode_id)
    if existing is not None:
        if existing.continuation_request_id != body.request_id:
            raise HTTPException(status_code=409, detail="This episode has already been continued.")
        # A committed continuation is replayed as is; no fresh write is admitted.
        replayed = serialize_episode(
            store, project_id, existing, branch_summary=_branch_summary(store, catalog)
        )
        return JSONResponse(status_code=200, content=replayed.model_dump(mode="json"))
    service = get_project_service(catalog, project_id)
    try:
        service.history.require_writable()
        if source.mode == "auto_research":
            continuation, _ = continue_auto_research(
                background_tasks,
                source,
                invocation_ceiling=body.invocation_ceiling,
                request_id=body.request_id,
                authorized_by=authorized_by,
            )
        else:
            # The write-admission dependency already holds the project's
            # Experiment operation lock for this request.
            continuation = continue_experiment_episode(
                project_id,
                source,
                invocation_ceiling=body.invocation_ceiling,
                request_id=body.request_id,
                authorized_by=authorized_by,
                catalog=catalog,
                store=store,
                background_tasks=background_tasks,
            )
    except (EpisodeNotRunning, StateUnavailable, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_episode(
        store,
        project_id,
        continuation,
        branch_summary=_branch_summary(store, catalog),
    )


@router.get(
    "/api/projects/{project_id}/episodes/{episode_id}/messages",
    response_model=list[AutoResearchMessageRecord],
)
def episode_messages(
    project_id: str,
    episode_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> list[AutoResearchMessageRecord]:
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    if episode.mode != "auto_research":
        raise HTTPException(status_code=409, detail="This episode has no Auto-research mail.")
    return store.auto_research_messages(episode.episode_id)


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/messages",
    response_model=AutoResearchMessageRecord,
    status_code=201,
    dependencies=[Depends(require_project_write_admission)],
)
def send_episode_message(
    project_id: str,
    episode_id: str,
    body: EpisodeMessageBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
) -> AutoResearchMessageRecord:
    authorized_by = identity_access.require_patch_capable_identity(request)
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    if episode.mode != "auto_research":
        raise HTTPException(status_code=409, detail="This episode has no Auto-research mail.")
    if episode.status != "running" or episode.ending is not None:
        raise HTTPException(status_code=409, detail="Episode is not accepting new mail")
    if episode.root_operation_id is None:
        raise HTTPException(status_code=409, detail="Episode orchestrator is unavailable")
    try:
        saved = record_auto_research_message(
            store,
            episode_id=episode.episode_id,
            sender_role="human",
            sender_task_id=None,
            authorized_by=authorized_by,
            recipient_task_id=episode.root_operation_id,
            body=body.body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        started = deliver_pending_auto_research_lifecycle(
            background_tasks,
            episode_id=episode.episode_id,
        )
        if started is None:
            deliver_pending_auto_research_mail(
                background_tasks,
                episode_id=episode.episode_id,
                recipient_task_id=episode.root_operation_id,
            )
    except Exception as exc:
        logger.warning(
            "Could not deliver durable Auto-research message %s immediately: %s",
            saved.message_id,
            exc,
        )
    current = store.auto_research_message(saved.message_id)
    if current is None:
        raise RuntimeError("The durable episode message could not be reloaded.")
    return current


@router.get("/api/projects/{project_id}/episodes/{episode_id}/report/content")
@router.head("/api/projects/{project_id}/episodes/{episode_id}/report/content")
def content_episode_report(
    project_id: str,
    episode_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    report = None if episode.ending == "stopped" else store.episode_report(episode.episode_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Episode report not found")
    try:
        document, csp = html_preview_document(
            store.read_artifact_bytes(report.artifact_id, report.artifact_version_id),
            frame_addon=selection_frame_addon()
            if _report_discussable_origin(store, episode_id)
            else None,
        )
    except (UnicodeError, ValueError, KeyError, OSError) as exc:
        raise HTTPException(status_code=410, detail="Episode report unavailable") from exc
    encoded = document.encode("utf-8")
    return Response(
        b"" if request.method == "HEAD" else encoded,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(len(encoded)),
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/api/projects/{project_id}/episodes/{episode_id}/report/save",
    dependencies=[Depends(require_project_write_admission)],
)
def save_episode_report(
    project_id: str,
    episode_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> dict[str, str]:
    """Save a repository copy without changing the captured episode report."""

    episode = _episode_for_http(store, catalog, project_id, episode_id)
    report = None if episode.ending == "stopped" else store.episode_report(episode.episode_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Episode report not found")
    service = get_project_service(catalog, project_id)
    project_name = catalog.card(project_id)["name"]
    if not isinstance(project_name, str):
        raise HTTPException(status_code=503, detail="Episode report save unavailable")
    try:
        filename = service.history.workspace.keep_artifact(
            source_name="episode-report.html",
            project_name=project_name,
            data=store.read_artifact_bytes(report.artifact_id, report.artifact_version_id),
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Episode report save unavailable") from exc
    return {"path": f"artifacts/{filename}"}


@router.get("/api/projects/{project_id}/episodes/{episode_id}/report/preview")
@router.head("/api/projects/{project_id}/episodes/{episode_id}/report/preview")
def preview_episode_report(
    project_id: str,
    episode_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    """Keep the old desktop route on the unified shell after source updates."""

    return _episode_report_viewer_response(
        project_id,
        episode_id,
        catalog=catalog,
        store=store,
        head=request.method == "HEAD",
    )


@router.get("/api/projects/{project_id}/episodes/{episode_id}/report/viewer")
def view_episode_report(
    project_id: str,
    episode_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    return _episode_report_viewer_response(
        project_id,
        episode_id,
        catalog=catalog,
        store=store,
    )


def _report_discussable_origin(store: AppStore, episode_id: str) -> bool:
    wrapup = store.episode_wrapup(episode_id)
    origin = (
        store.agent_task(wrapup.concluding_operation_id)
        if wrapup and wrapup.concluding_operation_id
        else None
    )
    return bool(
        origin
        and isinstance(origin.request.get("chat_id"), str)
        and not origin.history_only
        and origin.native_session_id
        and origin.stage_root
    )


def _episode_report_viewer_response(
    project_id: str,
    episode_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    head: bool = False,
) -> Response:
    episode = _episode_for_http(store, catalog, project_id, episode_id)
    report = None if episode.ending == "stopped" else store.episode_report(episode.episode_id)
    wrapup = store.episode_wrapup(episode.episode_id)
    if report is None or wrapup is None or wrapup.concluding_operation_id is None:
        raise HTTPException(status_code=404, detail="Episode report not found")
    origin = store.agent_task(wrapup.concluding_operation_id)
    chat_id = origin.request.get("chat_id") if origin is not None else None
    if not isinstance(chat_id, str):
        chat_id = None
    artifact_id = hashlib.sha256(report.report_id.encode("utf-8")).hexdigest()[:24]
    descriptor = AgentArtifactDescriptor(
        artifact_id=artifact_id,
        name="episode-report.html",
        media_type="text/html",
        size_bytes=len(store.read_artifact_bytes(report.artifact_id, report.artifact_version_id)),
    )
    content_url = (
        f"/api/projects/{quote(project_id, safe='')}/episodes/"
        f"{quote(episode_id, safe='')}/report/content"
    )
    panel = (
        comment_panel(
            {
                "projectId": project_id,
                "chatId": chat_id,
                "operationId": wrapup.concluding_operation_id,
                "artifactId": descriptor.artifact_id,
                "artifactName": descriptor.name,
                "mediaType": descriptor.media_type,
                "source": "episode_report",
                "episodeId": episode_id,
                "branchId": origin.graph_target.branch_id if origin else None,
            }
        )
        if _report_discussable_origin(store, episode_id)
        else None
    )
    document, csp = artifact_viewer_document(
        descriptor,
        content_url=content_url,
        state="report",
        panel=panel,
        save_url=(
            f"/api/projects/{quote(project_id, safe='')}/episodes/"
            f"{quote(episode_id, safe='')}/report/save"
        ),
    )
    return Response(
        b"" if head else document,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _resolved_auto_research_start_request(
    service: ProjectService,
    body: StartEpisodeBody,
) -> AutoResearchStartRequest:
    profile = service.resolve_agent_profile("orchestrator")
    request = AutoResearchStartRequest(
        invocation_ceiling=body.invocation_ceiling,
        code_worktree=body.code_worktree if body.code_worktree is not None else False,
        graph_isolation=body.graph_isolation,
        starting_instruction=body.starting_instruction,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        run_on=profile.run_on,
        run_truth_scope=list(service.manifest.agent.default_run_truth_scope),
    )
    resolved = service.resolve_skill_request(cast(RunRequest, request))
    if not isinstance(resolved, AutoResearchStartRequest):
        raise TypeError("Auto-research skill resolution changed the start request type.")
    return resolved


def _resolved_branch_merge_request(
    service: ProjectService,
    episode_id: str,
    *,
    run_on: str | None = None,
) -> BranchMergeRunRequest:
    profile = service.resolve_agent_profile("orchestrator")
    return BranchMergeRunRequest(
        episode_id=episode_id,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        # A code-isolated merge runs where its worktree is.
        run_on=run_on or profile.run_on,
        run_truth_scope=sorted(set(service.history.state().project_truth_scope)),
        chat_scope="project",
        mode="work",
        trigger="human",
        patch_kind="work",
    )


__all__ = [
    "archive_episode",
    "content_episode_report",
    "episode_messages",
    "episodes",
    "merge_episode_branch",
    "continue_episode",
    "preview_episode_report",
    "router",
    "send_episode_message",
    "start_episode",
    "stop_episode",
    "view_episode_report",
]
