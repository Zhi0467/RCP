from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from rcp.api.dependencies import (
    get_catalog,
    get_project_service,
    get_project_writer,
    require_project_membership,
    require_project_write_admission,
)
from rcp.paper import PaperService
from rcp.paper.service import PaperDraftConflict
from rcp.projects import ProjectCatalog

_LOG = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_project_membership)])


def _refresh_cached_paper(catalog: ProjectCatalog, project_id: str, paper: PaperService) -> None:
    """Best effort: the write already succeeded, and Paper opens from /paper itself."""

    try:
        catalog.update_cached_snapshot_paper(project_id, paper.snapshot)
    except (KeyError, OSError, ValueError) as exc:
        _LOG.warning("Could not refresh the cached Paper of project %s: %s", project_id, exc)


class PaperSaveRequest(BaseModel):
    content: str
    base_hash: str | None = None
    draft_version: str | None


@router.get("/api/projects/{project_id}/paper")
def get_paper(
    project_id: str,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
):
    paper = get_project_service(catalog, project_id).paper
    return paper.snapshot().model_dump(mode="json")


@router.post(
    "/api/projects/{project_id}/paper/create",
    dependencies=[Depends(require_project_write_admission)],
)
def create_paper(
    project_id: str,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
):
    paper = get_project_writer(catalog, project_id).paper
    created = paper.create()
    _refresh_cached_paper(catalog, project_id, paper)
    return created.model_dump(mode="json")


@router.put(
    "/api/projects/{project_id}/paper",
    dependencies=[Depends(require_project_write_admission)],
)
def save_paper(
    project_id: str,
    body: PaperSaveRequest,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
):
    paper = get_project_writer(catalog, project_id).paper
    try:
        saved = paper.save(body.content, body.base_hash, body.draft_version)
    except PaperDraftConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _refresh_cached_paper(catalog, project_id, paper)
    return saved.model_dump(mode="json")


@router.get("/api/projects/{project_id}/paper/sessions")
def paper_sessions(
    project_id: str,
    catalog: Annotated[ProjectCatalog, Depends(get_catalog)],
):
    paper = get_project_service(catalog, project_id).paper
    return [item.model_dump(mode="json") for item in paper.sessions()]


__all__ = [
    "PaperSaveRequest",
    "create_paper",
    "get_paper",
    "paper_sessions",
    "router",
    "save_paper",
]
