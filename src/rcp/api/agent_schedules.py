"""Owner-only schedule controls on the private thread seam."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from rcp.api.dependencies import get_identity_access, get_store
from rcp.api.identity import IdentityAccess
from rcp.runs.agent_schedules import validate_schedule
from rcp.storage import AppStore

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
    human = identity_access.require_patch_capable_identity(request)
    require_schedule_owner(store, thread_id, human.user_id)
    return {
        "schedules": [
            item.model_dump(
                mode="json",
                exclude={
                    "thread_id",
                    "created_operation_id",
                    "idempotency_key",
                    "pending_since",
                    "created_at",
                },
            )
            for item in store.agent_schedules(thread_id)
        ]
    }


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
    schedule = next(
        (item for item in store.agent_schedules(thread_id) if item.schedule_id == schedule_id), None
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    try:
        due = validate_schedule(
            cron=schedule.cron,
            at=schedule.at_local,
            timezone=schedule.timezone,
            after=datetime.fromisoformat(store.now()),
        )
        enabled = store.enable_agent_schedule(thread_id, schedule_id, due.isoformat())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return enabled.model_dump(
        mode="json",
        exclude={
            "thread_id",
            "created_operation_id",
            "idempotency_key",
            "pending_since",
            "created_at",
        },
    )
