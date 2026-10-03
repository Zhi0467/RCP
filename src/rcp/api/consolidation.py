"""Member-owned consolidation schedules and shared Inbox dispositions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.identity import IdentityAccess
from rcp.consolidation import next_occurrence
from rcp.limits import CONSOLIDATION_RECENT_NIGHTS
from rcp.projects import ProjectCatalog
from rcp.runs.consolidation import CONSOLIDATION_PRELAUNCH_ERRORS
from rcp.storage import AppStore
from rcp.storage.consolidation import ConsolidationRun, ConsolidationSchedule

router = APIRouter(dependencies=[Depends(require_project_membership)])
Store = Annotated[AppStore, Depends(get_store)]
Catalog = Annotated[ProjectCatalog, Depends(get_catalog)]
Identity = Annotated[IdentityAccess, Depends(get_identity_access)]


class ScheduleRequest(BaseModel):
    model_config = {"extra": "forbid"}

    local_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must name an IANA time zone") from exc
        return value


def schedule_response(schedule: ConsolidationSchedule | None, now: str) -> dict | None:
    if schedule is None:
        return None
    result = schedule.model_dump(
        mode="json",
        exclude={
            "project_id",
            "covered_head",
            "notification_observed_at",
            "last_occurrence_date",
            "skipped_dates",
        },
    )
    result["authorized_by"] = schedule.authorized_by.model_dump(include={"user_id", "display_name"})
    result["expired"] = datetime.fromisoformat(schedule.expires_at) <= datetime.fromisoformat(now)
    return result


def item_response(run: ConsolidationRun) -> dict:
    prelaunch_failure = run.error_code in CONSOLIDATION_PRELAUNCH_ERRORS
    return {
        "run_id": run.run_id,
        "kind": run.kind,
        "occurrence_date": run.occurrence_date,
        "created_at": run.created_at,
        "operation_id": None if prelaunch_failure else run.operation_id,
        "chat_id": None if prelaunch_failure else run.chat_id,
        "report": {"artifact_id": run.report_artifact_id, "title": run.report_title}
        if run.report_artifact_id
        else None,
        "applied_revisions": run.applied_revisions,
        "revisions_verified": run.revisions_verified,
        "proposals_created": run.proposals_created,
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
        "state": run.state,
    }


def recent_nights(
    schedule: ConsolidationSchedule | None, runs: list[ConsolidationRun]
) -> list[dict]:
    """The newest occurrences, oldest first: run outcomes plus recorded skips."""
    nights = {date: "skipped" for date in (schedule.skipped_dates if schedule else [])}
    for run in runs:
        nights[run.occurrence_date] = (
            "running"
            if run.outcome_settled_at is None
            else "succeeded"
            if run.kind == "report"
            else "failed"
        )
    newest = sorted(nights)[-CONSOLIDATION_RECENT_NIGHTS:]
    return [{"occurrence_date": date, "outcome": nights[date]} for date in newest]


@router.get("/api/projects/{project_id}/consolidation")
def get_consolidation(project_id: str, *, store: Store, catalog: Catalog) -> dict:
    require_registered_project(catalog, project_id)
    project_id = catalog.resolve_project_id(project_id)
    try:
        store.require_project_accepts_new_work(project_id)
        can_write = True
    except ValueError:
        can_write = False
    schedule = store.consolidation_schedule(project_id)
    runs = store.consolidation_runs(project_id)
    return {
        "can_write": can_write,
        "schedule": schedule_response(schedule, store.now()),
        "inbox": [
            item_response(run)
            for run in runs
            if run.state == "open" and run.outcome_settled_at is not None
        ],
        "recent_nights": recent_nights(schedule, runs),
    }


@router.put(
    "/api/projects/{project_id}/consolidation/schedule",
    dependencies=[Depends(require_project_write_admission)],
)
def put_schedule(
    project_id: str,
    body: ScheduleRequest,
    request: Request,
    *,
    store: Store,
    catalog: Catalog,
    identity_access: Identity,
) -> dict:
    human = identity_access.require_patch_capable_identity(request)
    require_registered_project(catalog, project_id)
    project_id = catalog.resolve_project_id(project_id)
    now = store.now()
    schedule = store.put_consolidation_schedule(
        project_id,
        local_time=body.local_time,
        timezone=body.timezone,
        authorized_by=human,
        next_due_at=next_occurrence(
            body.local_time, body.timezone, datetime.fromisoformat(now)
        ).isoformat(),
        now=now,
    )
    return {"schedule": schedule_response(schedule, now)}


@router.delete(
    "/api/projects/{project_id}/consolidation/schedule",
    dependencies=[Depends(require_project_write_admission)],
)
def delete_schedule(
    project_id: str, request: Request, *, store: Store, catalog: Catalog, identity_access: Identity
) -> dict:
    identity_access.require_patch_capable_identity(request)
    require_registered_project(catalog, project_id)
    store.delete_consolidation_schedule(catalog.resolve_project_id(project_id))
    return {"schedule": None}


def _resolve(project_id, run_id, request, store, catalog, identity_access, state):
    human = identity_access.require_patch_capable_identity(request)
    require_registered_project(catalog, project_id)
    try:
        run = store.resolve_consolidation_run(
            catalog.resolve_project_id(project_id), run_id, state=state, resolved_by=human
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Consolidation row not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"item": item_response(run)}


@router.post(
    "/api/projects/{project_id}/consolidation/runs/{run_id}/keep",
    dependencies=[Depends(require_project_write_admission)],
)
def keep_run(
    project_id: str,
    run_id: str,
    request: Request,
    *,
    store: Store,
    catalog: Catalog,
    identity_access: Identity,
) -> dict:
    return _resolve(project_id, run_id, request, store, catalog, identity_access, "kept")


@router.post(
    "/api/projects/{project_id}/consolidation/runs/{run_id}/dismiss",
    dependencies=[Depends(require_project_write_admission)],
)
def dismiss_run(
    project_id: str,
    run_id: str,
    request: Request,
    *,
    store: Store,
    catalog: Catalog,
    identity_access: Identity,
) -> dict:
    return _resolve(project_id, run_id, request, store, catalog, identity_access, "dismissed")
