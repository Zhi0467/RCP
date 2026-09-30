from __future__ import annotations

import logging
import uuid
from typing import Annotated, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from rcp.api.dependencies import (
    get_catalog,
    get_graph_service,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.episodes import episode_on_branch
from rcp.api.tasks import _agent_artifact_response
from rcp.artifact_views import artifact_content, artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor, ArtifactView, artifact_view
from rcp.projects import ProjectCatalog
from rcp.storage import AgentTaskRecord, AppStore, EpisodeMode
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
    viewer_url: str | None
    view: ArtifactView
    available: bool
    can_download: bool
    download_url: str | None
    can_open: bool
    unavailable_reason: str | None = None


def _episode_runs_query(
    store: AppStore,
    project_id: str,
    task: AgentTaskRecord,
    branch_id: str,
) -> dict[str, str] | None:
    """Name the Runs surface that owns one episode-bound branch conversation."""
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
        if task.project_id != project_id or task.kind not in {"node_chat", "project_chat"}:
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
                if task.episode_id is not None:
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
        episode = store.episode(task.episode_id) if task.episode_id else None
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
        entries.append(
            SavedArtifactResponse(
                id=f"report:{report.report_id}",
                name=report.display_title or "Report",
                kind="report",
                view="html",
                available=True,
                can_open=True,
                can_download=False,
                download_url=None,
                created_at=report.created_at,
                episode_id=report.episode_id,
                episode_mode=report.mode,
                source_chat_href=chat_origins.get(origin.operation_id) if origin else None,
                viewer_url=f"{base}/episodes/{quote(report.episode_id, safe='')}/report/viewer",
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
    if artifact is None or artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if (
        artifact.expires_at is not None
        and artifact.expires_at <= store.now()
        and artifact_id not in store.protected_revision_artifact_ids()
    ):
        raise HTTPException(status_code=410, detail="Artifact expired")
    return artifact


@router.get("/api/projects/{project_id}/artifacts/{artifact_id}/content")
@router.head("/api/projects/{project_id}/artifacts/{artifact_id}/content")
def stored_artifact_content(
    project_id: str,
    artifact_id: str,
    request: Request,
    *,
    store: Annotated[AppStore, Depends(get_store)],
) -> Response:
    artifact = _stored_artifact(store, project_id, artifact_id)
    try:
        data = store.read_artifact_bytes(artifact_id)
        document, media_type, csp = artifact_content(
            artifact.source_name, artifact.media_type, data
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
            content_url=f"{base}/content",
            keep_url=f"{base}/keep" if artifact.expires_at else None,
            state="temporary" if artifact.expires_at else "kept",
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


@router.post(
    "/api/projects/{project_id}/artifacts/{artifact_id}/keep",
    dependencies=[Depends(require_project_write_admission)],
)
def keep_stored_artifact(
    project_id: str,
    artifact_id: str,
    *,
    store: Annotated[AppStore, Depends(get_store)],
):
    _stored_artifact(store, project_id, artifact_id)
    return store.keep_artifact(artifact_id)


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
