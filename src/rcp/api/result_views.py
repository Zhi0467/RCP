from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from rcp.api.artifacts import _stored_artifact
from rcp.api.dependencies import (
    get_store,
    require_project_membership,
    require_project_write_admission,
)
from rcp.storage import AppStore

router = APIRouter(dependencies=[Depends(require_project_membership)])


@router.get("/api/projects/{project_id}/result-views")
def result_views(project_id: str) -> RedirectResponse:
    return RedirectResponse(f"/api/projects/{quote(project_id, safe='')}/artifacts")


@router.get("/api/projects/{project_id}/result-views/{view_id}/preview")
@router.head("/api/projects/{project_id}/result-views/{view_id}/preview")
def preview_result_view(
    project_id: str, view_id: str, *, store: Annotated[AppStore, Depends(get_store)]
) -> RedirectResponse:
    _stored_artifact(store, project_id, view_id)
    return RedirectResponse(
        f"/api/projects/{quote(project_id, safe='')}/artifacts/{quote(view_id, safe='')}/viewer"
    )


@router.post(
    "/api/projects/{project_id}/result-views/{view_id}/keep",
    dependencies=[Depends(require_project_write_admission)],
)
def keep_result_view(
    project_id: str, view_id: str, *, store: Annotated[AppStore, Depends(get_store)]
) -> RedirectResponse:
    _stored_artifact(store, project_id, view_id)
    return RedirectResponse(
        f"/api/projects/{quote(project_id, safe='')}/artifacts/{quote(view_id, safe='')}/keep"
    )
