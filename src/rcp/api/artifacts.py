from __future__ import annotations

import logging
import uuid
from typing import Annotated, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from rcp.api.dependencies import (
    get_background_tasks,
    get_catalog,
    get_graph_service,
    get_identity_access,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.episode_timeline import worker_label
from rcp.api.episodes import episode_on_branch
from rcp.api.identity import IdentityAccess
from rcp.api.tasks import _agent_artifact_response
from rcp.artifact_comments import comment_panel, selection_frame_addon, supports_comments
from rcp.artifact_views import artifact_content, artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor, ArtifactMediaType, ArtifactView, artifact_view
from rcp.background import BackgroundAgentTasks
from rcp.limits import ARTIFACT_CONTEXT_MAX_SELECTIONS, STEERING_MESSAGE_MAX_CHARS
from rcp.projects import ProjectCatalog
from rcp.runs.artifact_edit_admission import (
    ArtifactFreshSessionRequired,
    admit_artifact_edit,
    artifact_edit_availability,
    artifact_reply_origin,
    start_artifact_edit,
)
from rcp.service import ArtifactContextRequest, ArtifactSelection, RunRequest
from rcp.storage import AgentTaskAdmissionConflict, AgentTaskRecord, AppStore, EpisodeMode
from rcp.transport import StateUnavailable

router = APIRouter(dependencies=[Depends(require_project_membership)])
logger = logging.getLogger(__name__)


class SavedArtifactResponse(BaseModel):
    id: str
    name: str
    kind: Literal["artifact", "report"]
    created_at: str
    path: str | None = None
    operation_id: str | None = None
    artifact_id: str | None = None
    episode_id: str | None = None
    episode_mode: EpisodeMode | None = None
    source_chat_href: str | None = None
    source_node_id: str | None = None
    viewer_url: str | None
    view: ArtifactView
    available: bool
    can_download: bool
    download_url: str | None
    can_open: bool
    unavailable_reason: str | None = None


def _task_artifact_episode_id(task: AgentTaskRecord) -> str | None:
    edit = task.request.get("artifact_edit")
    return edit.get("episode_id") if isinstance(edit, dict) else task.episode_id


def _source_node_id(
    store: AppStore, project_id: str, task: AgentTaskRecord | None, episode_id: str | None
) -> str | None:
    """The graph node an artifact came from: its Experiment's node, else its node chat."""
    episode = store.episode(episode_id) if episode_id else None
    if episode is not None and episode.project_id == project_id and episode.control_node_id:
        return episode.control_node_id
    if task is None or task.project_id != project_id:
        return None
    node_id = task.request.get("node_id")
    return node_id if isinstance(node_id, str) and node_id else None


def _episode_runs_query(
    store: AppStore,
    project_id: str,
    task: AgentTaskRecord,
    branch_id: str,
) -> dict[str, str] | None:
    """Name the Runs surface that owns one episode-bound branch conversation."""
    # Edits retain episode provenance in their immutable admission, but never
    # participate in episode operations. Validate the operational origin before
    # exposing its bounded transcript, including edits of forked edit outputs.
    visited: set[str] = set()
    while isinstance(task.request.get("artifact_edit"), dict):
        if task.operation_id in visited:
            return None
        visited.add(task.operation_id)
        edit = task.request["artifact_edit"]
        origin = store.agent_task(edit.get("origin_operation_id", ""))
        if (
            origin is None
            or origin.project_id != project_id
            or origin.graph_target != task.graph_target
            or edit.get("episode_id") != _task_artifact_episode_id(origin)
        ):
            return None
        task = origin
    episode = store.episode(task.episode_id) if task.episode_id else None
    if (
        episode is None
        or episode.project_id != project_id
        or episode.graph_target != task.graph_target
    ):
        return None
    if episode.mode == "auto_research":
        # Ordinary Work the Auto-research parent spawned on its own branch. Its
        # turns are listed under that episode in Runs, where each transcript is
        # inspected without a composer.
        work = store.auto_research_child_work_for_operation(task.operation_id)
        if (
            episode.graph_target.branch_id != branch_id
            or work is None
            or work.project_id != project_id
            or work.episode_id != episode.episode_id
        ):
            return None
        return {"view": "runs", "mode": "auto_research", "episode": episode.episode_id}
    experiment = store.auto_research_child_experiment(episode.episode_id)
    if (
        not episode.control_node_id
        or not episode_on_branch(store, episode.episode_id, branch_id)
        or (
            experiment is not None
            and (
                experiment.project_id != project_id
                or experiment.control_node_id != episode.control_node_id
                or not episode_on_branch(store, experiment.auto_research_episode_id, branch_id)
            )
        )
    ):
        return None
    return {
        "view": "runs",
        "experiment": episode.control_node_id,
        "episode": episode.episode_id,
        "target": "branch",
        "branch": branch_id,
        **({"parent": experiment.auto_research_episode_id} if experiment else {}),
    }


def _saved_chat_origins(
    catalog: ProjectCatalog,
    store: AppStore,
    project_id: str,
    tasks: list[AgentTaskRecord],
) -> dict[str, str]:
    """Verify source conversations once per exact graph, without reading report bytes."""
    by_target: dict[str | None, list[AgentTaskRecord]] = {}
    for task in tasks:
        if task.project_id != project_id or task.kind not in {
            "node_chat",
            "project_chat",
            "artifact_edit",
        }:
            continue
        chat_id = task.request.get("chat_id")
        if not isinstance(chat_id, str):
            continue
        try:
            if str(uuid.UUID(chat_id)) != chat_id:
                continue
        except ValueError:
            continue
        by_target.setdefault(task.graph_target.branch_id, []).append(task)

    origins: dict[str, str] = {}
    for branch_id, group in by_target.items():
        chat_ids = sorted({task.request["chat_id"] for task in group})
        try:
            service = get_graph_service(catalog, project_id, branch_id, initialize=False)
            transcripts = service.chat_transcripts(chat_ids)
        except (HTTPException, OSError, StateUnavailable) as exc:
            # Losing a source conversation never removes an independently saved preview.
            logger.warning("Saved artifact source conversations unavailable: %s", exc)
            continue
        for task in group:
            chat_id = task.request["chat_id"]
            if chat_id not in transcripts:
                continue
            query = {"view": "chats", "chat": chat_id}
            if branch_id is not None:
                query["branch_id"] = branch_id
                if _task_artifact_episode_id(task) is not None:
                    # This session belongs to a bounded episode. Runs owns its
                    # read-only transcript; ordinary Chats would expose a composer.
                    runs_query = _episode_runs_query(store, project_id, task, branch_id)
                    if runs_query is None:
                        continue
                    query = runs_query
            origins[task.operation_id] = (
                f"#/projects/{quote(project_id, safe='')}?{urlencode(query)}"
            )
    return origins


@router.get("/api/projects/{project_id}/artifacts", response_model=list[SavedArtifactResponse])
def saved_artifacts(
    project_id: str,
    *,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
    store: Annotated[AppStore, Depends(get_store)],
) -> list[SavedArtifactResponse]:
    """Project saved output inventory, independent of recent task/episode windows."""
    # require_current_instance canonicalizes legacy project URLs before routing.
    require_registered_project(catalog, project_id)
    base = f"/api/projects/{quote(project_id, safe='')}"
    entries: list[SavedArtifactResponse] = []
    artifact_tasks = store.project_tasks_with_kept_artifacts(project_id)
    reports = store.project_episode_report_summaries(project_id)
    report_origins: dict[str, AgentTaskRecord] = {}
    for report in reports:
        wrapup = store.episode_wrapup(report.episode_id)
        if wrapup is not None and wrapup.concluding_operation_id is not None:
            origin = store.agent_task(wrapup.concluding_operation_id)
            if origin is not None:
                report_origins[report.episode_id] = origin
    chat_origins = _saved_chat_origins(
        catalog, store, project_id, [*artifact_tasks, *report_origins.values()]
    )
    for task in artifact_tasks:
        raw_artifacts = task.result.get("artifacts") if task.result else None
        if not isinstance(raw_artifacts, list):
            continue
        episode_id = _task_artifact_episode_id(task)
        episode = store.episode(episode_id) if episode_id else None
        episode_mode = episode.mode if episode and episode.project_id == project_id else None
        for raw in raw_artifacts:
            try:
                artifact = AgentArtifactDescriptor.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if not artifact.is_kept():
                continue
            projected = _agent_artifact_response(store, task, artifact)
            artifact_url = (
                f"{base}/tasks/{quote(task.operation_id, safe='')}/artifacts/{artifact.artifact_id}"
            )
            entries.append(
                SavedArtifactResponse(
                    id=f"artifact:{task.operation_id}:{artifact.artifact_id}",
                    name=artifact.name,
                    kind="artifact",
                    created_at=artifact.kept_at or task.created_at,
                    path=f"artifacts/{artifact.kept_filename}" if artifact.kept_filename else None,
                    operation_id=task.operation_id,
                    artifact_id=artifact.artifact_id,
                    episode_mode=episode_mode,
                    source_chat_href=chat_origins.get(task.operation_id),
                    source_node_id=_source_node_id(store, project_id, task, episode_id),
                    viewer_url=f"{artifact_url}/viewer" if projected.can_open else None,
                    view=projected.view,
                    available=projected.available,
                    can_open=projected.can_open,
                    can_download=projected.can_download,
                    download_url=f"{artifact_url}/download" if projected.can_download else None,
                )
            )
    # _validate_new_wrapup requires a concluding operation; finish_episode_report_ready
    # retains that wrap-up and forbids stopped endings. Captured reports therefore
    # satisfy _episode_report_viewer_response's availability prerequisites.
    for report in reports:
        origin = report_origins.get(report.episode_id)
        stored_report = store.episode_report(report.episode_id)
        if stored_report is None:
            continue
        artifact_url = f"{base}/artifacts/{quote(stored_report.artifact_id, safe='')}"
        entries.append(
            SavedArtifactResponse(
                id=f"report:{report.report_id}",
                name=report.display_title or "Report",
                kind="report",
                view="html",
                available=True,
                can_open=True,
                can_download=True,
                download_url=f"{artifact_url}/download",
                created_at=report.created_at,
                artifact_id=stored_report.artifact_id,
                episode_id=report.episode_id,
                episode_mode=report.mode,
                source_chat_href=chat_origins.get(origin.operation_id) if origin else None,
                source_node_id=_source_node_id(store, project_id, origin, report.episode_id),
                viewer_url=f"{artifact_url}/viewer",
            )
        )
    represented = {entry.artifact_id for entry in entries if entry.artifact_id}
    represented.update(
        report.artifact_id
        for summary in reports
        if (report := store.episode_report(summary.episode_id)) is not None
    )
    for artifact in store.artifacts(project_id):
        if artifact.artifact_id in represented or artifact.expires_at is not None:
            continue
        view = artifact_view(artifact.media_type)
        artifact_url = f"{base}/artifacts/{quote(artifact.artifact_id, safe='')}"
        entries.append(
            SavedArtifactResponse(
                id=f"artifact:{artifact.artifact_id}",
                name=artifact.display_title or artifact.source_name,
                kind="artifact",
                created_at=artifact.created_at,
                artifact_id=artifact.artifact_id,
                operation_id=artifact.origin_operation_id,
                episode_id=artifact.episode_id,
                source_node_id=_source_node_id(
                    store,
                    project_id,
                    store.agent_task(artifact.origin_operation_id)
                    if artifact.origin_operation_id
                    else None,
                    artifact.episode_id,
                ),
                viewer_url=f"{artifact_url}/viewer" if view not in {"file", "pdf"} else None,
                view=view,
                available=True,
                can_download=True,
                download_url=f"{artifact_url}/download",
                can_open=view not in {"file", "pdf"},
            )
        )
    return sorted(entries, key=lambda entry: (entry.created_at, entry.id), reverse=True)


def _stored_artifact(store: AppStore, project_id: str, artifact_id: str):
    artifact = store.artifact(artifact_id)
    if artifact is None:
        status = store.artifact_import_status(artifact_id)
        if status is not None and status["project_id"] == project_id:
            raise HTTPException(
                status_code=410 if status["state"] == "missing" else 409,
                detail=status["reason"] or "Artifact import is pending.",
            )
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if (
        artifact.expires_at is not None
        and artifact.expires_at <= store.now()
        and artifact_id
        not in (store.protected_edit_artifact_ids() | store.legacy_artifact_import_ids())
    ):
        raise HTTPException(status_code=410, detail="Artifact expired")
    return artifact


class ArtifactViewerState(BaseModel):
    artifact_id: str
    name: str
    media_type: ArtifactMediaType
    view: ArtifactView
    supplier: Literal["turn", "episode_ending"]
    current_version: str
    version_number: int
    can_undo: bool
    live: Literal["live", "finished"] | None
    editing_operation_id: str | None
    edit_failure: str | None = None
    can_comment: bool
    comment_unavailable_reason: str | None
    fresh_session_required: bool
    thread_href: str | None
    viewer_url: str | None
    download_url: str
    can_keep: bool
    expires_at: str | None


class RunArtifactEntry(BaseModel):
    artifact_id: str
    name: str
    media_type: ArtifactMediaType
    view: ArtifactView
    supplier: Literal["turn", "episode_ending"]
    origin_operation_id: str | None
    worker_label: str | None
    created_at: str


def _artifact_thread_href(store: AppStore, artifact) -> str | None:
    try:
        origin, reply_episode_id = artifact_reply_origin(store, artifact)
    except AgentTaskAdmissionConflict:
        return None
    if reply_episode_id:
        query = {"view": "runs", "mode": "auto_research", "episode": reply_episode_id}
    else:
        query = {"view": "chats", "chat": origin.request["chat_id"]}
        branch_id = origin.graph_target.branch_id
        episode = store.episode(artifact.episode_id or origin.episode_id or "")
        if branch_id:
            query["branch_id"] = branch_id
            if episode:
                query = _episode_runs_query(store, artifact.project_id, origin, branch_id)
                if query is None:
                    return None
    return f"#/projects/{quote(artifact.project_id, safe='')}?{urlencode(query)}"


@router.get(
    "/api/projects/{project_id}/artifacts/{artifact_id}/state",
    response_model=ArtifactViewerState,
)
def stored_artifact_state(
    project_id: str,
    artifact_id: str,
    *,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
    store: Annotated[AppStore, Depends(get_store)],
) -> ArtifactViewerState:
    with store.artifact_lock(artifact_id):
        artifact = _stored_artifact(store, project_id, artifact_id)
        versions = store.artifact_versions(artifact_id)
        current = next(v for v in versions if v.version_id == artifact.current_version)
        can_undo = bool(store._artifact_ancestors(current, versions))
    can_comment, reason, fresh = False, None, False
    origin = store.agent_task(artifact.origin_operation_id or "")
    if origin is None:
        reason = "The artifact's origin is unavailable."
    else:
        try:
            service = get_graph_service(
                catalog, project_id, origin.graph_target.branch_id, initialize=False
            )
            availability = artifact_edit_availability(store, service, artifact)
            can_comment = availability.can_comment
            reason = availability.comment_unavailable_reason
            fresh = availability.fresh_session_required
        except (HTTPException, OSError, StateUnavailable, ValueError) as exc:
            reason = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
    editing = store.artifact_edit_operation_id(project_id, artifact_id)
    base = f"/api/projects/{quote(project_id, safe='')}/artifacts/{quote(artifact_id, safe='')}"
    live = None
    if artifact.live_data_allowed and current.live is not None and not current.live.invalid_reason:
        live = "finished" if current.live_snapshot else "live"
    return ArtifactViewerState(
        artifact_id=artifact_id,
        name=artifact.display_title or artifact.source_name,
        media_type=artifact.media_type,
        view=artifact_view(artifact.media_type),
        supplier=artifact.supplier,
        current_version=current.version_id,
        version_number=versions.index(current) + 1,
        can_undo=can_undo,
        live=live,
        editing_operation_id=editing,
        edit_failure=store.artifact_edit_failure(project_id, artifact_id),
        can_comment=can_comment,
        comment_unavailable_reason=reason,
        fresh_session_required=fresh,
        thread_href=_artifact_thread_href(store, artifact),
        viewer_url=f"{base}/viewer"
        if artifact_view(artifact.media_type) not in {"pdf", "file"}
        else None,
        download_url=f"{base}/download",
        can_keep=artifact.expires_at is not None,
        expires_at=artifact.expires_at,
    )


@router.get(
    "/api/projects/{project_id}/episodes/{episode_id}/artifacts",
    response_model=list[RunArtifactEntry],
)
def run_artifacts(
    project_id: str,
    episode_id: str,
    *,
    store: Annotated[AppStore, Depends(get_store)],
) -> list[RunArtifactEntry]:
    episode = store.episode(episode_id)
    if episode is None or episode.project_id != project_id:
        raise HTTPException(status_code=404, detail="Episode not found")
    episode_ids = {episode_id}
    if episode.mode == "auto_research":
        episode_ids.update(
            child.child_episode_id
            for child in store.auto_research_child_experiments(episode_id)
            if child.project_id == project_id
        )
    now = store.now()
    artifacts = [
        artifact
        for artifact in store.artifacts(project_id)
        if artifact.episode_id in episode_ids
        and (artifact.kept_at or artifact.expires_at is None or artifact.expires_at > now)
    ]
    artifacts.sort(
        key=lambda artifact: (
            not (artifact.supplier == "episode_ending" and artifact.episode_id == episode_id),
            artifact.created_at,
            artifact.artifact_id,
        )
    )
    entries = []
    for artifact in artifacts:
        work = store.auto_research_child_work_for_operation(artifact.origin_operation_id or "")
        entries.append(
            RunArtifactEntry(
                artifact_id=artifact.artifact_id,
                name=artifact.display_title or artifact.source_name,
                media_type=artifact.media_type,
                view=artifact_view(artifact.media_type),
                supplier=artifact.supplier,
                origin_operation_id=artifact.origin_operation_id,
                worker_label=worker_label(work.instruction) if work else None,
                created_at=artifact.created_at,
            )
        )
    return entries


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/content")
@router.head("/api/projects/{project_id}/artifacts/{artifact_id}/content")
def stored_artifact_content(
    project_id: str,
    artifact_id: str,
    request: Request,
    version_id: str | None = None,
    *,
    store: Annotated[AppStore, Depends(get_store)],
) -> Response:
    artifact = _stored_artifact(store, project_id, artifact_id)
    try:
        data = store.read_artifact_bytes(artifact_id, version_id)
        document, media_type, csp = artifact_content(
            artifact.source_name,
            artifact.media_type,
            data,
            frame_addon=selection_frame_addon() if artifact.media_type == "text/html" else None,
        )
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=410, detail="Artifact unavailable") from exc
    return Response(
        b"" if request.method == "HEAD" else document,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": csp,
        },
    )


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/viewer")
@router.head("/api/projects/{project_id}/artifacts/{artifact_id}/viewer")
def stored_artifact_viewer(
    project_id: str,
    artifact_id: str,
    request: Request,
    *,
    store: Annotated[AppStore, Depends(get_store)],
) -> Response:
    artifact = _stored_artifact(store, project_id, artifact_id)
    base = f"/api/projects/{quote(project_id, safe='')}/artifacts/{quote(artifact_id, safe='')}"
    descriptor = AgentArtifactDescriptor(
        artifact_id=artifact.artifact_id,
        name=artifact.source_name,
        media_type=artifact.media_type,
        kept_at=artifact.kept_at,
    )
    try:
        document, csp = artifact_viewer_document(
            descriptor,
            content_url=f"{base}/content?{urlencode({'version_id': artifact.current_version})}",
            live_url=f"{base}/versions/{quote(artifact.current_version, safe='')}/live",
            keep_url=f"{base}/keep" if artifact.expires_at else None,
            state="temporary" if artifact.expires_at else "kept",
            panel=comment_panel(
                {
                    "projectId": project_id,
                    "artifactId": artifact_id,
                    "mediaType": artifact.media_type,
                }
            )
            if supports_comments(artifact.media_type)
            else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Artifact has no viewer") from exc
    return Response(
        b"" if request.method == "HEAD" else document,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": csp,
        },
    )


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/versions/{version_id}/live")
def stored_artifact_live(
    project_id: str,
    artifact_id: str,
    version_id: str,
    *,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
    store: Annotated[AppStore, Depends(get_store)],
):
    from rcp.live_artifact_runtime import artifact_live_snapshot

    require_registered_project(catalog, project_id)
    _stored_artifact(store, project_id, artifact_id)
    try:
        snapshot = artifact_live_snapshot(store, catalog, artifact_id, version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Artifact version not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, StateUnavailable) as exc:
        raise HTTPException(status_code=503, detail="Live data unavailable") from exc
    return Response(
        snapshot.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/api/projects/{project_id}/artifacts/{artifact_id}/keep",
    dependencies=[Depends(require_project_write_admission)],
)
def keep_stored_artifact(
    project_id: str,
    artifact_id: str,
    request: Request,
    *,
    store: Annotated[AppStore, Depends(get_store)],
    identity_access: Annotated[IdentityAccess, Depends(get_identity_access)],
):
    human = identity_access.require_patch_capable_identity(request)
    _stored_artifact(store, project_id, artifact_id)
    return store.keep_artifact(artifact_id, resolved_by=human)


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/download")
@router.head("/api/projects/{project_id}/artifacts/{artifact_id}/download")
def download_stored_artifact(
    project_id: str,
    artifact_id: str,
    request: Request,
    *,
    store: Annotated[AppStore, Depends(get_store)],
) -> Response:
    artifact = _stored_artifact(store, project_id, artifact_id)
    try:
        data = store.read_artifact_bytes(artifact_id)
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=410, detail="Artifact unavailable") from exc
    return Response(
        b"" if request.method == "HEAD" else data,
        media_type=artifact.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.source_name, safe='')}",
            "Content-Length": str(len(data)),
        },
    )


class ArtifactComment(BaseModel):
    """One comment: its text, anchored to a selection or to the whole artifact."""

    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=2048)
    selection: ArtifactSelection | None = None


class ArtifactCommentBody(BaseModel):
    model_config = {"extra": "forbid"}

    comments: list[ArtifactComment] = Field(
        min_length=1, max_length=ARTIFACT_CONTEXT_MAX_SELECTIONS
    )
    edit_now: bool = False
    fresh_session: bool = False


@router.post(
    "/api/projects/{project_id}/artifacts/{artifact_id}/comments",
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def comment_artifact(
    project_id: str,
    artifact_id: str,
    body: ArtifactCommentBody,
    request: Request,
    *,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
    store: Annotated[AppStore, Depends(get_store)],
    identity_access: Annotated[IdentityAccess, Depends(get_identity_access)],
    background_tasks: Annotated[BackgroundAgentTasks, Depends(get_background_tasks)],
):
    artifact = store.artifact(artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if any(not comment.text.strip() for comment in body.comments):
        raise HTTPException(status_code=422, detail="Every artifact comment needs text.")
    general = "\n\n".join(c.text.strip() for c in body.comments if c.selection is None)
    selections = [
        c.selection.model_copy(update={"comment": c.text.strip()})
        for c in body.comments
        if c.selection is not None
    ]
    if len(general) + sum(len(s.comment) for s in selections) > STEERING_MESSAGE_MAX_CHARS:
        raise HTTPException(status_code=422, detail="The artifact comments are too long.")
    author = identity_access.require_patch_capable_identity(request)
    origin = store.agent_task(artifact.origin_operation_id or "")
    if origin is None:
        raise HTTPException(status_code=409, detail="The artifact's origin is unavailable.")
    service = get_graph_service(catalog, project_id, origin.graph_target.branch_id)
    try:
        admitted = admit_artifact_edit(
            store,
            service,
            project_id,
            RunRequest(
                message=general,
                artifact_context=ArtifactContextRequest(
                    operation_id=origin.operation_id,
                    artifact_id=artifact_id,
                    fresh_session=body.fresh_session,
                    edit_now=body.edit_now,
                    selections=selections,
                ),
            ),
        )
        return start_artifact_edit(
            background_tasks,
            project_id,
            admitted,
            authorized_by=author,
        ).model_dump(mode="json")
    except ArtifactFreshSessionRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "fresh_session_required", "message": str(exc)},
        ) from exc
    except AgentTaskAdmissionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, StateUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/api/projects/{project_id}/artifacts/{artifact_id}/undo",
    dependencies=[Depends(require_project_write_admission)],
)
def undo_artifact(
    project_id: str,
    artifact_id: str,
    *,
    store: Annotated[AppStore, Depends(get_store)],
):
    artifact = store.artifact(artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    try:
        return store.undo_artifact(artifact_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
