from __future__ import annotations

import logging
import uuid
from typing import Annotated, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from rcp.api.dependencies import (
    get_catalog,
    get_graph_service,
    get_store,
    require_project_membership,
    require_registered_project,
)
from rcp.artifacts import AgentArtifactDescriptor
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
    viewer_url: str
    can_open: bool = True
    unavailable_reason: str | None = None


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
                    episode = store.episode(task.episode_id)
                    route = store.auto_research_child_experiment(task.episode_id)
                    if (
                        episode is None
                        or episode.project_id != project_id
                        or episode.mode != "experiment_loop"
                        or episode.graph_target != task.graph_target
                        or not episode.control_node_id
                        or route is None
                        or route.project_id != project_id
                        or route.control_node_id != episode.control_node_id
                        or route.auto_research_episode_id != branch_id
                    ):
                        continue
                    # This session belongs to a bounded episode. Runs owns its
                    # read-only transcript; ordinary Chats would expose a composer.
                    query = {
                        "view": "runs",
                        "experiment": episode.control_node_id,
                        "episode": episode.episode_id,
                        "target": "branch",
                        "branch": branch_id,
                        "parent": route.auto_research_episode_id,
                    }
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
            if artifact.kept_filename is None:
                continue
            entries.append(
                SavedArtifactResponse(
                    id=f"artifact:{task.operation_id}:{artifact.artifact_id}",
                    name=artifact.name,
                    kind="artifact",
                    created_at=artifact.kept_at or task.created_at,
                    path=f"artifacts/{artifact.kept_filename}",
                    operation_id=task.operation_id,
                    artifact_id=artifact.artifact_id,
                    episode_mode=episode_mode,
                    source_chat_href=chat_origins.get(task.operation_id),
                    viewer_url=(
                        f"{base}/tasks/{quote(task.operation_id, safe='')}/artifacts/"
                        f"{artifact.artifact_id}/viewer"
                    ),
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
                created_at=report.created_at,
                episode_id=report.episode_id,
                episode_mode=report.mode,
                source_chat_href=chat_origins.get(origin.operation_id) if origin else None,
                viewer_url=f"{base}/episodes/{quote(report.episode_id, safe='')}/report/viewer",
            )
        )
    return sorted(entries, key=lambda entry: (entry.created_at, entry.id), reverse=True)
