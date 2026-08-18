from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from rcp.api.dependencies import (
    get_catalog,
    get_project_service,
    get_store,
    require_project_membership,
)
from rcp.core.transitions import transition_trigger_manifest
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]


@router.get("/api/projects/{project_id}/history")
def history(
    project_id: str,
    from_revision: int = 1,
    to_revision: int | None = None,
    *,
    catalog: CatalogDependency,
):
    service = get_project_service(catalog, project_id)
    return service.history.slice(from_revision, to_revision)


@router.get("/api/projects/{project_id}/history/summaries")
def history_summaries(
    project_id: str,
    from_revision: int = 1,
    to_revision: int | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
):
    service = get_project_service(catalog, project_id)
    summaries = service.history.revision_summaries(from_revision, to_revision)
    episode_ids = {
        episode_id
        for summary in summaries
        if isinstance(episode_id := summary.get("episode_id"), str)
    }
    episodes = {
        episode_id: _history_episode_decoration(store, project_id, episode_id)
        for episode_id in episode_ids
    }
    return [
        {
            **summary,
            "episode": episodes.get(summary.get("episode_id")),
        }
        for summary in summaries
    ]


@router.get("/api/projects/{project_id}/transition-manifest")
def graph_transition_manifest(
    project_id: str,
    *,
    catalog: CatalogDependency,
):
    get_project_service(catalog, project_id)
    return transition_trigger_manifest().model_dump(mode="json")


def _history_episode_decoration(
    store: AppStore,
    project_id: str,
    episode_id: str,
) -> dict[str, object] | None:
    episode = store.episode(episode_id)
    if episode is None or episode.project_id != project_id:
        return None
    report = None if episode.ending == "stopped" else store.episode_report(episode_id)
    return {
        "mode": episode.mode,
        "status": episode.status,
        "ending": episode.ending,
        "wrapup_state": episode.wrapup_state,
        "report": (
            {
                "report_id": report.report_id,
                "ending": report.ending,
                "created_at": report.created_at,
            }
            if report is not None
            else None
        ),
    }


__all__ = [
    "graph_transition_manifest",
    "history",
    "history_summaries",
    "router",
]
