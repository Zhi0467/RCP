from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import (
    get_catalog,
    get_graph_service,
    get_identity_access,
    get_store,
    get_watcher_poller,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.identity import IdentityAccess
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore, StoredWatcherRecord, WatcherClaimConflict, WatcherRecord
from rcp.watchers import WatcherPoller

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
WatcherPollerDependency = Annotated[WatcherPoller, Depends(get_watcher_poller)]


@router.get("/api/projects/{project_id}/watchers")
def project_watchers(
    project_id: str,
    branch_id: str | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> list[dict[str, object]]:
    require_registered_project(catalog, project_id)
    target = (
        get_graph_service(catalog, project_id, branch_id, initialize=False).history.graph_target
        if branch_id is not None
        else None
    )
    return [
        _watcher_response(record)
        for record in store.watchers(catalog.resolve_project_id(project_id))
        if target is None or record.graph_target == target
    ]


@router.post("/api/projects/{project_id}/watchers/{watcher_id}/check")
def check_watcher_now(
    project_id: str,
    watcher_id: str,
    *,
    catalog: CatalogDependency,
    watcher_poller: WatcherPollerDependency,
) -> dict[str, object]:
    require_registered_project(catalog, project_id)
    try:
        watcher = watcher_poller.check_now(project_id, watcher_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watcher not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _watcher_response(watcher)


@router.post("/api/projects/{project_id}/watchers/{watcher_id}/stop")
def stop_watcher(
    project_id: str,
    watcher_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> dict[str, object]:
    require_registered_project(catalog, project_id)
    watcher = store.watcher(watcher_id)
    if watcher is None or watcher.project_id != project_id:
        raise HTTPException(status_code=404, detail="Watcher not found")
    if watcher.continuation.patch_kind == "experiment_loop":
        raise HTTPException(
            status_code=409,
            detail="Use Stop loop to stop an Experiment loop and its watchers gracefully.",
        )
    try:
        stopped = store.stop_watchers(project_id, [watcher_id])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watcher not found") from exc
    except WatcherClaimConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _watcher_response(stopped[0])


@router.post(
    "/api/projects/{project_id}/watchers/{watcher_id}/cancel",
    dependencies=[Depends(require_project_write_admission)],
)
def cancel_watcher(
    project_id: str,
    watcher_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    watcher_poller: WatcherPollerDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    human = identity_access.require_patch_capable_identity(request)
    project_id = catalog.resolve_project_id(project_id)
    try:
        watcher = watcher_poller.cancel(project_id, watcher_id, human.user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watcher not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _watcher_response(watcher)


def _watcher_response(record: StoredWatcherRecord) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["can_cancel"] = isinstance(record, WatcherRecord) and record.can_cancel
    payload["can_check_now"] = bool(
        isinstance(record, WatcherRecord) and record.status == "degraded" and not record.notified
    )
    if record.notification_operation_id:
        payload["delivery_label"] = "Delivery claimed"
    elif record.status == "stopped":
        payload["delivery_label"] = "Stopped · not delivered"
    elif record.status == "completed" and not record.notified:
        payload["delivery_label"] = "Pending delivery"
    elif record.notified:
        payload["delivery_label"] = "Acknowledged · not delivered"
    else:
        payload["delivery_label"] = "Not delivered"
    return payload


__all__ = [
    "cancel_watcher",
    "check_watcher_now",
    "project_watchers",
    "router",
    "stop_watcher",
]
