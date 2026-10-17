"""Space-local project group inventory and human mutations."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from rcp.api.dependencies import get_identity_access, get_store
from rcp.api.identity import IdentityAccess
from rcp.storage import AppStore
from rcp.storage.models import ProjectGroupRecord
from rcp.storage.project_groups import ProjectGroupError

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]


class ProjectGroupNameRequest(BaseModel):
    name: str


class ProjectGroupAssignmentRequest(BaseModel):
    group_id: str | None


class ProjectGroupResponse(BaseModel):
    id: str
    name: str
    created_at: str

    @classmethod
    def from_record(cls, record: ProjectGroupRecord) -> ProjectGroupResponse:
        return cls(id=record.group_id, name=record.name, created_at=record.created_at)


def project_group_http_error(error: ProjectGroupError) -> HTTPException:
    status = {
        "project_group_not_found": 404,
        "project_group_not_empty": 409,
        "project_group_name_invalid": 422,
    }[error.code]
    return HTTPException(status_code=status, detail={"code": error.code, "message": str(error)})


def require_visible_project_group(store: AppStore, group_id: str | None, user_id: str) -> None:
    if group_id is not None and not any(
        group.group_id == group_id for group in store.project_groups(user_id)
    ):
        raise project_group_http_error(ProjectGroupError("project_group_not_found"))


@router.get("/api/project-groups")
def project_groups(
    request: Request, *, store: StoreDependency, identity: IdentityDependency
) -> list[ProjectGroupResponse]:
    return [
        ProjectGroupResponse.from_record(group)
        for group in store.project_groups(identity.acting_user(request).user_id)
    ]


@router.post("/api/project-groups", status_code=201)
def create_project_group(
    body: ProjectGroupNameRequest,
    request: Request,
    *,
    store: StoreDependency,
    identity: IdentityDependency,
) -> ProjectGroupResponse:
    try:
        return ProjectGroupResponse.from_record(
            store.create_project_group(body.name, user_id=identity.acting_user(request).user_id)
        )
    except ProjectGroupError as exc:
        raise project_group_http_error(exc) from exc


@router.patch("/api/project-groups/{group_id}")
def rename_project_group(
    group_id: str,
    body: ProjectGroupNameRequest,
    request: Request,
    *,
    store: StoreDependency,
    identity: IdentityDependency,
) -> ProjectGroupResponse:
    try:
        return ProjectGroupResponse.from_record(
            store.rename_project_group(
                group_id, body.name, user_id=identity.acting_user(request).user_id
            )
        )
    except ProjectGroupError as exc:
        raise project_group_http_error(exc) from exc


@router.delete("/api/project-groups/{group_id}", status_code=204)
def delete_project_group(
    group_id: str, request: Request, *, store: StoreDependency, identity: IdentityDependency
) -> Response:
    try:
        store.delete_project_group(group_id, user_id=identity.acting_user(request).user_id)
    except ProjectGroupError as exc:
        raise project_group_http_error(exc) from exc
    return Response(status_code=204)
