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
from rcp.limits import BULLETIN_REFS_MAX_COUNT, BULLETIN_TEXT_MAX_CHARS
from rcp.storage import AppStore
from rcp.storage.bulletin import BulletinError

router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
ProjectDependency = Annotated[str, Depends(require_project_membership)]
WriteProjectDependency = Annotated[str, Depends(require_project_write_admission)]


class BulletinPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=BULLETIN_TEXT_MAX_CHARS)
    key: str = Field(min_length=1, max_length=200)
    refs: list[str] = Field(default_factory=list, max_length=BULLETIN_REFS_MAX_COUNT)
    reply_to: str | None = None


def _target(store: AppStore, project: str, branch_id: str | None) -> GraphTargetRef:
    if branch_id is None:
        return GraphTargetRef()
    if branch_id not in store.branch_names(project):
        raise HTTPException(status_code=404, detail="Branch not found")
    return GraphTargetRef(kind="branch", branch_id=branch_id)


def _error(exc: BulletinError) -> HTTPException:
    status = 422 if exc.code.endswith("invalid") else 409
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
    try:
        return {
            "roster": bulletin_roster(store, project, target),
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
        post = store.add_bulletin_post(
            project,
            target.key,
            body.text,
            user_id=human.user_id,
            key=body.key,
            refs=body.refs,
            reply_to=body.reply_to,
        )
        return {"post": post}
    except BulletinError as exc:
        raise _error(exc) from exc
