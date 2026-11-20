"""Schedule controls on the agent seam."""

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


def require_schedule_owner(
    store: AppStore, thread_id: str, user_id: str, schedule_id: str | None = None
) -> None:
    """The member sees the agent and, for one schedule, authorized its creating turn."""
    from rcp.api.agent_threads import accessible_agent

    accessible_agent(store, thread_id, user_id)
    if schedule_id is not None and not any(
        item.schedule_id == schedule_id for item in _member_schedules(store, thread_id, user_id)
    ):
        raise HTTPException(status_code=404, detail="Schedule not found")


def _member_schedules(store: AppStore, thread_id: str, user_id: str) -> list:
    def author(schedule) -> str | None:
        task = store.agent_task(schedule.created_operation_id)
        return task.authorized_by.user_id if task and task.authorized_by else None

    return [item for item in store.agent_schedules(thread_id) if author(item) == user_id]


@router.get("/api/agent-threads/{thread_id}/schedules")
def list_schedules(
    thread_id: str, request: Request, *, store: Store, identity_access: Identity
) -> dict:
    user = identity_access.acting_user(request)
    require_schedule_owner(store, thread_id, user.user_id)
    return {
        "schedules": [_view(item) for item in _member_schedules(store, thread_id, user.user_id)]
    }


def _view(schedule) -> dict:
    """A waiting note keeps pending_since and why it has not started."""
    return schedule.model_dump(
        mode="json",
        exclude={
            "thread_id",
            "created_operation_id",
            "idempotency_key",
            "created_at",
            "pending_fires",
        },
    )


@router.delete("/api/agent-threads/{thread_id}/schedules/{schedule_id}", status_code=204)
def cancel_schedule(
    thread_id: str, schedule_id: str, request: Request, *, store: Store, identity_access: Identity
) -> Response:
    human = identity_access.require_patch_capable_identity(request)
    require_schedule_owner(store, thread_id, human.user_id, schedule_id)
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
    require_schedule_owner(store, thread_id, human.user_id, schedule_id)
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
    require_schedule_owner(store, thread_id, human.user_id, schedule_id)
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
    require_schedule_owner(store, thread_id, human.user_id, schedule_id)
    try:
        return _view(resume_schedule(store, thread_id, schedule_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
