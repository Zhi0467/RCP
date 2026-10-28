"""Project-scoped review routes for saved Paper changes."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from rcp.api.dependencies import (
    get_attachment_store,
    get_background_tasks,
    get_catalog,
    get_identity_access,
    get_project_service,
    get_store,
    project_write_admission,
    require_project_membership,
)
from rcp.paper.changes import ChangeGroup, OutdatedChange, PaperChanges, get_paper_changes
from rcp.paper.models import PaperConflict, PaperPaused
from rcp.server_ops.maintenance import MaintenanceAdmissionClosed

router = APIRouter(dependencies=[Depends(require_project_membership)])


class TaskChangesRequest(BaseModel):
    task_id: str


def _manager(project_id: str, request: Request):
    catalog = get_catalog(request)
    canonical = catalog.resolve_project_id(project_id)
    manager = get_paper_changes(get_project_service(catalog, canonical))

    @contextmanager
    def admission(project: str):
        with manager.admission(project):
            require_project_membership(project, request)
            yield

    return canonical, PaperChanges(manager.store, manager.live, admission)


@router.get("/api/projects/{project_id}/paper/changes")
def changes(project_id: str, request: Request, task_id: str | None = None):
    canonical, manager = _manager(project_id, request)
    try:
        return {"groups": [group.public() for group in manager.groups(canonical, task_id)]}
    except (PaperConflict, MaintenanceAdmissionClosed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/paper/changes/{change_id}/proposed")
def proposed(project_id: str, change_id: str, request: Request):
    canonical, manager = _manager(project_id, request)
    try:
        return PlainTextResponse(
            manager.proposed(canonical, change_id),
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "attachment",
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Paper change not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/paper/changes/undo-all")
def undo_all(project_id: str, body: TaskChangesRequest, request: Request):
    actor = get_identity_access(request).require_patch_capable_identity(request).user_id
    canonical, manager = _manager(project_id, request)
    try:
        return {
            "groups": [group.public() for group in manager.undo_all(canonical, body.task_id, actor)]
        }
    except (PaperPaused, MaintenanceAdmissionClosed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _continue(project_id: str, group: ChangeGroup, request: Request):
    from rcp.api.tasks import start_ordinary_task

    store = get_store(request)
    previous = store.agent_task(group.task_id)
    if (
        previous is None
        or previous.project_id != project_id
        or previous.kind != "paper_coach"
        or not previous.visible
    ):
        raise HTTPException(status_code=404, detail="Paper coach task not found")
    if previous.history_only:
        raise HTTPException(status_code=409, detail="History-only coach tasks cannot continue")
    if not previous.native_session_id or previous.status not in {
        "succeeded",
        "failed",
        "interrupted",
    }:
        raise HTTPException(
            status_code=409, detail="The coach must have a completed native session to continue"
        )
    body = dict(previous.request)
    body.update(
        session_id=previous.native_session_id,
        provider=previous.request.get("provider"),
        run_on=previous.request.get("run_on"),
    )
    if group.state not in {"pending", "outdated"}:
        raise HTTPException(status_code=409, detail="Only undecided paper changes can be refreshed")
    body.update(
        refresh_source_task_id=group.task_id,
        refresh_group_ids=[group.group_id],
    )
    authorized_by = get_identity_access(request).require_patch_capable_identity(request)
    return start_ordinary_task(
        project_id,
        "paper_coach",
        body,
        catalog=get_catalog(request),
        store=store,
        attachment_store=get_attachment_store(request),
        background_tasks=get_background_tasks(request),
        authorized_by=authorized_by,
        branch_id=None,
    ).model_dump(mode="json")


@router.post("/api/projects/{project_id}/paper/changes/{group_id}/{action}")
def decide(
    project_id: str,
    group_id: str,
    action: Literal["accept", "reject", "undo", "refresh"],
    request: Request,
):
    actor = get_identity_access(request).require_patch_capable_identity(request).user_id
    canonical, manager = _manager(project_id, request)
    try:
        if action == "refresh":
            group = manager.group(canonical, group_id)
            with project_write_admission(project_id, request):
                return _continue(canonical, group, request)
        if action == "accept":
            decided = manager.accept(canonical, group_id, actor)
        elif action == "reject":
            decided = manager.reject(canonical, group_id, actor)
        else:
            decided = manager.undo(canonical, group_id, actor)
        return decided.public()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Paper change group not found") from exc
    except (PaperPaused, MaintenanceAdmissionClosed) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OutdatedChange, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
