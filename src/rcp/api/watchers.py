from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from rcp.api.dependencies import (
    get_catalog,
    get_graph_service,
    get_store,
    require_project_membership,
    require_registered_project,
)
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore, StoredWatcherRecord, WatcherRecord

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]


@router.get("/api/projects/{project_id}/watchers")
def project_watchers(
    project_id: str,
    branch_id: str | None = None,
    all_targets: bool = False,
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
        _watcher_response(store, record)
        for record in store.watchers(catalog.resolve_project_id(project_id))
        if target is None or all_targets or record.graph_target == target
    ]


def _watcher_response(
    store: AppStore,
    record: StoredWatcherRecord,
) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload["can_stop_watching"] = False
    requester = (
        store.space_user(record.cancel_requested_by)
        if isinstance(record, WatcherRecord) and record.cancel_requested_by
        else None
    )
    payload["cancel_requested_by_name"] = requester.display_name if requester else None
    payload["can_cancel"] = False
    payload["wakes_chat"] = wakes_chat(record)
    payload["can_check_now"] = False
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


def wakes_chat(watcher: StoredWatcherRecord) -> bool:
    """Whether a chat's own Work turn armed this watcher and it will still wake that chat."""

    return (
        watcher.origin_task_kind in {"node_chat", "project_chat"}
        and watcher.continuation.patch_kind == "work"
        and watcher.status in {"active", "degraded"}
        and not watcher.notified
    )


__all__ = ["project_watchers", "router"]
