from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_notification_sender,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.identity import IdentityAccess
from rcp.notifications import NotificationSender
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
SenderDependency = Annotated[NotificationSender, Depends(get_notification_sender)]


class NotificationPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: StrictBool | None = None
    decision: StrictBool | None = None
    blocker: StrictBool | None = None
    episode_needs_action: StrictBool | None = None
    episode_finished: StrictBool | None = None


class DesktopNotification(BaseModel):
    notification_id: str
    reason: Literal["proposal", "decision", "blocker", "episode_needs_action", "episode_finished"]
    project_name: str
    deep_link: str


class NotificationAcknowledgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["posted", "failed"]


def _device_identity(
    request: Request, store: AppStore, identity: IdentityAccess
) -> tuple[str, str | None]:
    member = identity.acting_user(request)
    if member.identity_kind != "team_member":
        return member.user_id, None
    secret = identity.authenticating_team_session(request)
    if secret is not None:
        for session in store.team_sessions(member.user_id, authenticating_session=secret):
            if session.is_current:
                return member.user_id, session.session_id
    raise HTTPException(status_code=401, detail="An active device session is required.")


def _require_device(
    device_id: str, request: Request, store: AppStore, identity: IdentityAccess
) -> None:
    user_id, session_id = _device_identity(request, store, identity)
    device = store.notification_device(device_id)
    if (
        device is None
        or device["user_id"] != user_id
        or device["session_id"] != session_id
        or device["kind"] != "desktop"
    ):
        raise HTTPException(status_code=404, detail="Notification device not found.")


@router.post("/api/notifications/devices/desktop")
def register_desktop(
    request: Request, *, store: StoreDependency, identity: IdentityDependency
) -> dict[str, object]:
    user_id, session_id = _device_identity(request, store, identity)
    try:
        device = store.register_notification_device(user_id, session_id=session_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=401, detail="An active device identity is required."
        ) from exc
    return {"device_id": device["device_id"], "kind": device["kind"]}


@router.get(
    "/api/notifications/devices/{device_id}/pending", response_model=list[DesktopNotification]
)
def pending_desktop(
    device_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity: IdentityDependency,
    sender: SenderDependency,
) -> list[dict[str, object]]:
    _require_device(device_id, request, store, identity)
    return sender.pending_desktop(device_id)


@router.post("/api/notifications/devices/{device_id}/items/{notification_id}")
def acknowledge_desktop(
    device_id: str,
    notification_id: str,
    body: NotificationAcknowledgment,
    request: Request,
    *,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, bool]:
    _require_device(device_id, request, store, identity)
    if not store.acknowledge_notification(
        device_id, notification_id, posted=body.status == "posted"
    ):
        raise HTTPException(status_code=404, detail="Notification item not found.")
    return {"ok": True}


@router.get(
    "/api/projects/{project_id}/notifications",
    dependencies=[Depends(require_project_membership)],
)
def notification_preferences(
    project_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, bool]:
    require_registered_project(catalog, project_id)
    return store.notification_preferences(project_id, identity.acting_user(request).user_id)


@router.patch(
    "/api/projects/{project_id}/notifications",
    dependencies=[Depends(require_project_membership), Depends(require_project_write_admission)],
)
def update_notification_preferences(
    project_id: str,
    body: NotificationPreferencesUpdate,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, bool]:
    require_registered_project(catalog, project_id)
    return store.set_notification_preferences(
        project_id,
        identity.acting_user(request).user_id,
        body.model_dump(exclude_none=True),
    )
