"""Owner-only schedule controls on the private thread seam."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from rcp.api.dependencies import get_identity_access, get_store
from rcp.api.identity import IdentityAccess
from rcp.runs.agent_schedules import resume_schedule, thread_schedule
from rcp.storage import AppStore
from rcp.storage.agent_schedules import resume_fire

router = APIRouter()
Store = Annotated[AppStore, Depends(get_store)]
Identity = Annotated[IdentityAccess, Depends(get_identity_access)]


def require_schedule_owner(store: AppStore, thread_id: str, user_id: str) -> None:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM agent_threads WHERE thread_id=? AND user_id=? AND closed_at IS NULL",
            (thread_id, user_id),
        ).fetchone()
    if row is None or (
        row["project_id"] and not store.is_project_member(row["project_id"], user_id)
    ):
        raise HTTPException(status_code=404, detail="Thread not found")


@router.get("/api/agent-threads/{thread_id}/schedules")
def list_schedules(
    thread_id: str, request: Request, *, store: Store, identity_access: Identity
) -> dict:
    user = identity_access.acting_user(request)
    require_schedule_owner(store, thread_id, user.user_id)
    return {"schedules": [_view(item) for item in store.agent_schedules(thread_id)]}


def _view(schedule) -> dict:
    """A waiting note keeps pending_since and why it has not started."""
    return schedule.model_dump(
        mode="json", exclude={"thread_id", "created_operation_id", "idempotency_key", "created_at"}
    )


@router.delete("/api/agent-threads/{thread_id}/schedules/{schedule_id}", status_code=204)
def cancel_schedule(
    thread_id: str, schedule_id: str, request: Request, *, store: Store, identity_access: Identity
) -> Response:
    human = identity_access.require_patch_capable_identity(request)
    require_schedule_owner(store, thread_id, human.user_id)
    try:
        store.cancel_agent_schedule(thread_id, schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    return Response(status_code=204)


@router.post("/api/agent-threads/{thread_id}/schedules/{schedule_id}/enable")
def enable_schedule(
    thread_id: str, schedule_id: str, request: Request, *, store: Store, identity_access: Identity
) -> dict:
    human = identity_access.require_patch_capable_identity(request)
    require_schedule_owner(store, thread_id, human.user_id)
    try:
        schedule = thread_schedule(store, thread_id, schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    due = resume_fire(schedule, datetime.fromisoformat(store.now()))
    if due is None:
        # A past one-time schedule cannot resume; Cancel removes it.
        raise HTTPException(status_code=409, detail="Its last run time has passed")
    try:
        enabled = store.reactivate_agent_schedule(
            thread_id, schedule_id, due.isoformat(), was="disabled"
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(enabled)


@router.post("/api/agent-threads/{thread_id}/schedules/{schedule_id}/pause")
def pause_schedule(
    thread_id: str, schedule_id: str, request: Request, *, store: Store, identity_access: Identity
) -> dict:
    human = identity_access.require_patch_capable_identity(request)
    require_schedule_owner(store, thread_id, human.user_id)
    try:
        return _view(store.pause_agent_schedule(thread_id, schedule_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/agent-threads/{thread_id}/schedules/{schedule_id}/resume")
def resume_schedule_route(
    thread_id: str, schedule_id: str, request: Request, *, store: Store, identity_access: Identity
) -> dict:
    """Resume at the next fire after now; one whose limits have passed is done."""
    human = identity_access.require_patch_capable_identity(request)
    require_schedule_owner(store, thread_id, human.user_id)
    try:
        return _view(resume_schedule(store, thread_id, schedule_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
