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
from rcp.core.transition_models import GraphTargetRef
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore
from rcp.storage.agent_tasks import SESSION_TASK_KINDS
from rcp.storage.models import AgentTaskRecord, WatcherCheckRecord, WatcherRecord

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]

_STOPPED_BY = {"agent": "agent", "rcp": "loop", "human": "human"}


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
    watchers = store.project_watchers(catalog.resolve_project_id(project_id))
    if target is not None and not all_targets:
        turns = store.agent_tasks_by_ids({watcher.origin_turn_id for watcher in watchers})
        watchers = [
            watcher
            for watcher in watchers
            if (turn := turns.get(watcher.origin_turn_id)) is not None
            and turn.graph_target == target
        ]
    return watcher_check_views(store, watchers)


def watcher_check_views(store: AppStore, watchers: list[WatcherRecord]) -> list[dict[str, object]]:
    """One response object per check, in the shape the Web reads.

    The arming turn supplies the node, episode, branch, and continuation.
    """

    turns = store.agent_tasks_by_ids({watcher.origin_turn_id for watcher in watchers})
    return [
        _check_view(watcher, check, turns.get(watcher.origin_turn_id))
        for watcher in watchers
        for check in watcher.checks
    ]


def _check_view(
    watcher: WatcherRecord,
    check: WatcherCheckRecord,
    turn: AgentTaskRecord | None,
) -> dict[str, object]:
    request = turn.request if turn is not None else {}
    arming_target = turn.graph_target if turn is not None else GraphTargetRef()
    loop = request.get("patch_kind") == "experiment_loop"
    removed = watcher.cancelled_at is None and check.removed_at is not None
    status = (
        "stopped"
        if watcher.cancelled_at is not None or removed
        else "completed"
        if check.completed_at is not None
        else "degraded"
        if check.consecutive_error_count > 0
        else "active"
    )
    view: dict[str, object] = {
        "watcher_id": check.check_id,
        "project_id": watcher.project_id,
        "origin_operation_id": watcher.origin_turn_id,
        "origin_task_kind": turn.kind if turn is not None else None,
        "chat_id": watcher.agent_id,
        "node_id": request.get("node_id") or request.get("control_node_id"),
        "episode_id": turn.episode_id if turn is not None else None,
        "graph_target": (check.graph_target or arming_target).model_dump(mode="json"),
        "machine_id": check.machine_id,
        "continuation": {
            "patch_kind": request["patch_kind"] if loop else "work",
            "control_node_id": request.get("control_node_id"),
            "control_episode_id": request.get("control_episode_id"),
        },
        "status": status,
        "created_at": check.created_at,
        "completed_at": check.completed_at,
        "notified": not watcher.open,
        "notification_operation_id": watcher.wake_turn_id,
        "stopped_by": "agent"
        if removed
        else _STOPPED_BY[watcher.cancelled_by]
        if watcher.cancelled_by is not None
        else None,
        "stop_reason": watcher.cancel_reason,
        "stopped_at": watcher.cancelled_at or check.removed_at,
        "stop_operation_id": None,
        "can_check_now": False,
        "can_stop_watching": False,
        "wakes_chat": (
            turn is not None
            and turn.kind in SESSION_TASK_KINDS
            and not loop
            and status in {"active", "degraded"}
            and watcher.open
        ),
        "delivery_label": _delivery_label(watcher, status),
    }
    if check.definition == "graph":
        view.update(
            condition=check.condition.model_dump(mode="json") if check.condition else None,
            armed_revision=check.armed_head,
            last_evaluated_at=check.last_checked_at,
        )
        return view
    grouped = len(watcher.checks) > 1
    view.update(
        check_command=check.check_command,
        log_path=check.log_path,
        cwd=check.cwd,
        purpose=watcher.purpose,
        cancel_command=None,
        cancel_requested_by=None,
        cancel_requested_by_name=None,
        cancel_requested_at=None,
        cancel_error=None,
        can_cancel=False,
        last_checked_at=check.last_checked_at,
        last_exit_code=check.last_exit_code,
        last_error=check.last_error,
        next_check_at=check.next_check_at,
        consecutive_error_count=check.consecutive_error_count,
        group_id=watcher.watcher_id if grouped else None,
        group_label=watcher.purpose if grouped else None,
    )
    return view


def _delivery_label(watcher: WatcherRecord, status: str) -> str:
    if watcher.wake_turn_id:
        return "Delivery claimed"
    if status == "stopped":
        return "Stopped · not delivered"
    if status == "completed" and watcher.open:
        return "Pending delivery"
    if not watcher.open:
        return "Acknowledged · not delivered"
    return "Not delivered"


__all__ = ["project_watchers", "router", "watcher_check_views"]
