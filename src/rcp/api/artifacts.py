from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from rcp.api.dependencies import (
    get_catalog,
    get_store,
    require_project_membership,
    require_registered_project,
)
from rcp.artifacts import AgentArtifactDescriptor
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore

router = APIRouter(dependencies=[Depends(require_project_membership)])


class SavedArtifactResponse(BaseModel):
    id: str
    name: str
    kind: Literal["artifact", "report"]
    created_at: str
    path: str | None = None
    operation_id: str | None = None
    artifact_id: str | None = None
    episode_id: str | None = None
    viewer_url: str
    can_open: bool = True
    unavailable_reason: str | None = None


@router.get("/api/projects/{project_id}/artifacts", response_model=list[SavedArtifactResponse])
def saved_artifacts(
    project_id: str,
    *,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
    store: Annotated[AppStore, Depends(get_store)],
) -> list[SavedArtifactResponse]:
    """Project saved output inventory, independent of recent task/episode windows."""
    require_registered_project(catalog, project_id)
    base = f"/api/projects/{quote(project_id, safe='')}"
    entries: list[SavedArtifactResponse] = []
    for task in store.project_tasks_with_kept_artifacts(project_id):
        raw_artifacts = task.result.get("artifacts") if task.result else None
        if not isinstance(raw_artifacts, list):
            continue
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
                    viewer_url=(
                        f"{base}/tasks/{quote(task.operation_id, safe='')}/artifacts/"
                        f"{artifact.artifact_id}/viewer"
                    ),
                )
            )
    for report in store.project_episode_report_summaries(project_id):
        label = "Experiment" if report.mode == "experiment_loop" else "Auto-research"
        subject = report.control_node_id or report.instruction or report.episode_id[:8]
        entries.append(
            SavedArtifactResponse(
                id=f"report:{report.report_id}",
                name=f"{label} report: {subject[:160]}",
                kind="report",
                created_at=report.created_at,
                episode_id=report.episode_id,
                viewer_url=f"{base}/episodes/{quote(report.episode_id, safe='')}/report/viewer",
            )
        )
    return sorted(entries, key=lambda entry: (entry.created_at, entry.id), reverse=True)
