from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from rcp.api.dependencies import (
    get_identity_access,
    get_store,
    require_project_membership,
    require_project_write_admission,
)
from rcp.api.identity import IdentityAccess
from rcp.core.models import AuthorizedHuman
from rcp.storage import AppStore
from rcp.storage.research_views import ResearchViewConflict, ResearchViewPin

router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
ProjectDependency = Annotated[str, Depends(require_project_membership)]


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    branch_id: str | None = None


class PinsResponse(BaseModel):
    pins: list[ResearchViewPin]


@router.get("/api/projects/{project_id}/research-views")
def research_views(
    request: Request,
    *,
    project_id: ProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
    branch_id: str | None = None,
) -> PinsResponse:
    try:
        return PinsResponse(
            pins=store.research_view_pins(
                project_id, identity.acting_user(request).user_id, branch_id
            )
        )
    except ResearchViewConflict as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.post(
    "/api/projects/{project_id}/research-views",
    dependencies=[Depends(require_project_write_admission)],
)
def pin_research_view(
    body: PinRequest,
    request: Request,
    *,
    project_id: ProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> PinsResponse:
    user = identity.acting_user(request)
    human = (
        AuthorizedHuman(
            space_id=store.space_id, user_id=user.user_id, display_name=user.display_name
        )
        if user.display_name and user.display_name.strip()
        else None
    )
    try:
        return PinsResponse(
            pins=store.pin_research_view(
                project_id, user.user_id, body.artifact_id, body.branch_id, resolved_by=human
            )
        )
    except ResearchViewConflict as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc


@router.delete(
    "/api/projects/{project_id}/research-views/{artifact_id}",
    dependencies=[Depends(require_project_write_admission)],
)
def unpin_research_view(
    artifact_id: str,
    request: Request,
    *,
    project_id: ProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
    branch_id: str | None = None,
) -> PinsResponse:
    try:
        return PinsResponse(
            pins=store.unpin_research_view(
                project_id, identity.acting_user(request).user_id, artifact_id, branch_id
            )
        )
    except ResearchViewConflict as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from exc
