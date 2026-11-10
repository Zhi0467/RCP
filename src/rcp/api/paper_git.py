"""Member-owned Paper Git transport and conflict decisions."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from rcp.api.dependencies import get_catalog, get_identity_access, require_project_membership
from rcp.paper.git_sync import PaperGit
from rcp.server_ops.maintenance import MaintenanceAdmissionClosed

router = APIRouter(
    prefix="/api/projects/{project_id}/paper/git",
    dependencies=[Depends(require_project_membership)],
)


class KeyRequest(BaseModel):
    repository: str
    branch: str = "main"


class ResolveRequest(BaseModel):
    path: str
    take: Literal["theirs", "mine"]


class DecideRequest(BaseModel):
    group_id: str
    decision: Literal["accept", "reject"]


class PushRequest(BaseModel):
    message: str


def _call(project_id: str, request: Request, action):
    canonical = get_catalog(request).resolve_project_id(project_id)
    manager: PaperGit = request.app.state.paper_git
    try:
        with manager.lock(canonical):
            require_project_membership(canonical, request)
            return action(manager, canonical)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Paper Git conflict not found") from exc
    except MaintenanceAdmissionClosed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, OSError) as exc:
        return JSONResponse(status_code=409, content={"reason": str(exc)})


def _member(request: Request):
    return get_identity_access(request).require_patch_capable_identity(request)


@router.get("")
def status(project_id: str, request: Request):
    return _call(project_id, request, lambda manager, project: manager.status(project))


@router.post("/key")
def key(project_id: str, body: KeyRequest, request: Request):
    member = _member(request)
    return _call(
        project_id,
        request,
        lambda manager, project: manager.key(project, body.repository, body.branch, member.user_id),
    )


@router.post("/link")
def link(project_id: str, request: Request):
    return _call(project_id, request, lambda manager, project: manager.link(project))


@router.delete("")
def unlink(project_id: str, request: Request):
    def remove(manager, project):
        manager.disconnect(project)
        return Response(status_code=204)

    return _call(project_id, request, remove)


@router.post("/fetch")
def fetch(project_id: str, request: Request):
    return _call(project_id, request, lambda manager, project: manager.pull(project))


@router.get("/changes")
def changes(project_id: str, request: Request):
    return _call(project_id, request, lambda manager, project: manager.changed_files(project))


@router.get("/conflicts")
def conflicts(project_id: str, request: Request):
    return _call(project_id, request, lambda manager, project: manager.conflicts(project))


@router.post("/conflicts/resolve")
def resolve(project_id: str, body: ResolveRequest, request: Request):
    member = _member(request)
    return _call(
        project_id,
        request,
        lambda manager, project: manager.resolve(project, body.path, body.take, member.user_id),
    )


@router.post("/conflicts/decide")
def decide(project_id: str, body: DecideRequest, request: Request):
    member = _member(request)
    return _call(
        project_id,
        request,
        lambda manager, project: manager.decide(
            project, body.group_id, body.decision, member.user_id
        ),
    )


@router.post("/conflicts/refresh")
def refresh(project_id: str, request: Request):
    return _call(project_id, request, lambda manager, project: manager.pull(project, refresh=True))


@router.post("/push")
def push(project_id: str, body: PushRequest, request: Request):
    member = _member(request)
    return _call(
        project_id,
        request,
        lambda manager, project: {
            "commit": manager.push(project, body.message, member.display_name, member.user_id)
        },
    )
