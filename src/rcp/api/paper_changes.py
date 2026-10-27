"""Project-scoped review routes; app.py registers this router during S1 integration."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
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
from rcp.paper.changes import OutdatedChange, get_paper_changes

router = APIRouter(dependencies=[Depends(require_project_membership)])


class TaskChangesRequest(BaseModel):
    task_id: str


def _manager(project_id: str, request: Request):
    catalog = get_catalog(request)
    canonical = catalog.resolve_project_id(project_id)
    return canonical, get_paper_changes(get_project_service(catalog, canonical))


@router.get("/api/projects/{project_id}/paper/changes")
def changes(project_id: str, request: Request, task_id: str | None = None):
    canonical, manager = _manager(project_id, request)
    return {"groups": [group.public() for group in manager.groups(canonical, task_id)]}


@router.post("/api/projects/{project_id}/paper/changes/accept-all")
def accept_all(project_id: str, body: TaskChangesRequest, request: Request):
    actor = get_identity_access(request).require_patch_capable_identity(request).user_id
    with project_write_admission(project_id, request) as canonical:
        _, manager = _manager(canonical, request)
        try:
            return {
                "groups": [
                    group.public() for group in manager.accept_all(canonical, body.task_id, actor)
                ]
            }
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/projects/{project_id}/paper/changes/undo-all")
def undo_all(project_id: str, body: TaskChangesRequest, request: Request):
    actor = get_identity_access(request).require_patch_capable_identity(request).user_id
    with project_write_admission(project_id, request) as canonical:
        _, manager = _manager(canonical, request)
        try:
            return {
                "groups": [
                    group.public() for group in manager.undo_all(canonical, body.task_id, actor)
                ]
            }
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _continue(project_id: str, task_id: str, request: Request, *, group_id: str | None = None):
    from rcp.api.tasks import start_ordinary_task

    store = get_store(request)
    previous = store.agent_task(task_id)
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
    if group_id is None:
        diagnostics = store.agent_task_receipts_by_category(task_id, "paper_changes_invalid")
        if not diagnostics:
            raise HTTPException(
                status_code=409, detail="This task has no invalid-output diagnostics"
            )
        body.update(
            retry_source_task_id=task_id,
            refresh_source_task_id=None,
            refresh_group_ids=[],
            refresh_file_ids=None,
        )
    else:
        _, manager = _manager(project_id, request)
        group = next(
            (group for group in manager.groups(project_id, task_id) if group.group_id == group_id),
            None,
        )
        if group is None or group.state not in {"pending", "outdated"}:
            raise HTTPException(
                status_code=409, detail="Only undecided paper changes can be refreshed"
            )
        body.update(
            refresh_source_task_id=task_id,
            refresh_group_ids=[group_id],
            refresh_file_ids=sorted(manager.affected_file_ids(project_id, {group_id})),
            retry_source_task_id=None,
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


@router.post("/api/projects/{project_id}/paper/changes/retry", status_code=202)
def retry_changes(project_id: str, body: TaskChangesRequest, request: Request):
    with project_write_admission(project_id, request) as canonical:
        return _continue(canonical, body.task_id, request)


@router.post("/api/projects/{project_id}/paper/changes/{group_id}/{action}")
def decide(
    project_id: str,
    group_id: str,
    action: Literal["accept", "reject", "undo", "refresh"],
    request: Request,
):
    actor = get_identity_access(request).require_patch_capable_identity(request).user_id
    with project_write_admission(project_id, request) as canonical:
        _, manager = _manager(canonical, request)
        try:
            if action == "refresh":
                group = next(
                    (group for group in manager.groups(canonical) if group.group_id == group_id),
                    None,
                )
                if group is None:
                    raise KeyError("Paper change group not found")
                return _continue(canonical, group.task_id, request, group_id=group_id)
            return getattr(manager, action)(canonical, group_id, actor).public()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Paper change group not found") from exc
        except (OutdatedChange, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
