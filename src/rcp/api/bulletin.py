"""Member Bulletin reads and posts; neither operation dispatches agent work."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rcp.api.dependencies import (
    get_identity_access,
    get_store,
    require_project_membership,
    require_project_write_admission,
)
from rcp.api.identity import IdentityAccess
from rcp.bulletin import bulletin_roster
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    BULLETIN_KEY_MAX_CHARS,
    BULLETIN_REPLY_MAX_CHARS,
    BULLETIN_TEXT_MAX_CHARS,
)
from rcp.storage import AppStore
from rcp.storage.bulletin import BulletinError

router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
ProjectDependency = Annotated[str, Depends(require_project_membership)]
WriteProjectDependency = Annotated[str, Depends(require_project_write_admission)]


class BulletinEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=BULLETIN_TEXT_MAX_CHARS)
    key: str = Field(min_length=1, max_length=BULLETIN_KEY_MAX_CHARS)


class BulletinPost(BulletinEdit):
    reply_to: str | None = Field(default=None, min_length=1, max_length=BULLETIN_REPLY_MAX_CHARS)


def _target(store: AppStore, project: str, branch_id: str | None) -> GraphTargetRef:
    if branch_id is None:
        return GraphTargetRef()
    if branch_id not in store.branch_names(project):
        raise HTTPException(status_code=404, detail="Branch not found")
    return GraphTargetRef(kind="branch", branch_id=branch_id)


def _error(exc: BulletinError) -> HTTPException:
    status = (
        403
        if exc.code in {"bulletin_not_author", "bulletin_forbidden"}
        else 404
        if exc.code == "bulletin_post_not_found"
        else 422
        if exc.code.endswith("invalid")
        else 409
    )
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/api/projects/{project_id}/bulletin")
def list_bulletin(
    *,
    project: ProjectDependency,
    store: StoreDependency,
    branch_id: str | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    target = _target(store, project, branch_id)
    roster = bulletin_roster(store, project)
    try:
        store.require_project_accepts_new_work(project)
        can_post = True
    except ValueError:
        can_post = False
    try:
        return {
            "can_post": can_post,
            "members": store.bulletin_members(project),
            "roster": roster,
            **store.list_bulletin(project, target.key, cursor=cursor),
        }
    except BulletinError as exc:
        raise _error(exc) from exc


@router.post("/api/projects/{project_id}/bulletin")
def post_bulletin(
    body: BulletinPost,
    request: Request,
    *,
    project: WriteProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
    branch_id: str | None = None,
) -> dict[str, object]:
    human = identity.require_patch_capable_identity(request)
    target = _target(store, project, branch_id)
    try:
        post = store.mutate_bulletin_post(
            project,
            target.key,
            user_id=human.user_id,
            key=body.key,
            text=body.text,
            reply_to=body.reply_to,
        )
        return {"post": post}
    except BulletinError as exc:
        raise _error(exc) from exc


class BulletinRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_post_id: str = Field(min_length=1, max_length=BULLETIN_REPLY_MAX_CHARS)


@router.get("/api/projects/{project_id}/bulletin/channels")
def bulletin_channels(
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> list[dict[str, object]]:
    human = identity.acting_user(request)
    return store.bulletin_channels(project, human.user_id)


@router.post("/api/projects/{project_id}/bulletin/read")
def read_bulletin(
    body: BulletinRead,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
    branch_id: str | None = None,
) -> dict[str, object]:
    human = identity.acting_user(request)
    target = _target(store, project, branch_id)
    try:
        store.mark_bulletin_read(project, target.key, human.user_id, body.sequence_post_id)
        return {"ok": True}
    except BulletinError as exc:
        raise _error(exc) from exc


@router.patch("/api/projects/{project_id}/bulletin/{post_id}")
def edit_bulletin(
    post_id: str,
    body: BulletinEdit,
    request: Request,
    *,
    project: WriteProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
    branch_id: str | None = None,
) -> dict[str, object]:
    human = identity.require_patch_capable_identity(request)
    target = _target(store, project, branch_id)
    try:
        return {
            "post": store.mutate_bulletin_post(
                project,
                target.key,
                post_id=post_id,
                user_id=human.user_id,
                key=body.key,
                text=body.text,
                action="edit",
            )
        }
    except BulletinError as exc:
        raise _error(exc) from exc


@router.delete("/api/projects/{project_id}/bulletin/{post_id}")
def delete_bulletin(
    post_id: str,
    request: Request,
    *,
    project: WriteProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
    branch_id: str | None = None,
) -> dict[str, object]:
    human = identity.require_patch_capable_identity(request)
    target = _target(store, project, branch_id)
    try:
        return {
            "post": store.mutate_bulletin_post(
                project,
                target.key,
                post_id=post_id,
                user_id=human.user_id,
                key=f"delete:{post_id}",
                action="delete",
            )
        }
    except BulletinError as exc:
        raise _error(exc) from exc
