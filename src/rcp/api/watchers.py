from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import (
    get_catalog,
    get_graph_service,
    get_identity_access,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.identity import IdentityAccess
from rcp.core.transition_models import GraphTargetRef
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore
from rcp.storage.models import AgentTaskRecord, WatcherCheckRecord, WatcherRecord
from rcp.watchers import cancel_watched_job

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]

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


@router.post(
    "/api/projects/{project_id}/watchers/{watcher_id}/cancel",
    dependencies=[Depends(require_project_write_admission)],
)
def cancel_job(
    project_id: str,
    watcher_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    """Run the job's saved Cancel command; an accepted Cancel ends its watcher too."""

    human = identity_access.require_patch_capable_identity(request)
    project_id = catalog.resolve_project_id(project_id)
    try:
        watcher = cancel_watched_job(store, project_id, watcher_id, human.user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Watcher not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _single_view(store, watcher, watcher_id)


@router.post("/api/projects/{project_id}/watchers/{watcher_id}/stop")
def stop_watching(
    project_id: str,
    watcher_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> dict[str, object]:
    """Release a live observer whose Experiment already ended, leaving its job alone."""

    require_registered_project(catalog, project_id)
    project_id = catalog.resolve_project_id(project_id)
    watcher = store.watcher_of_check(watcher_id)
    if watcher is None or watcher.project_id != project_id:
        raise HTTPException(status_code=404, detail="Watcher not found")
    check = next(item for item in watcher.checks if item.check_id == watcher_id)
    turn = store.agent_task(watcher.origin_turn_id)
    if not _can_stop_watching(store, watcher, check, turn, {}):
        raise HTTPException(
            status_code=409,
            detail="Only a live observer of an ended Experiment can stop watching.",
        )
    store.cancel_watchers(
        [watcher.watcher_id],
        by="human",
        reason="A member stopped watching after the Experiment ended.",
    )
    current = store.watcher(watcher.watcher_id)
    assert current is not None
    return _single_view(store, current, watcher_id)


def _single_view(store: AppStore, watcher: WatcherRecord, check_id: str) -> dict[str, object]:
    return next(
        view for view in watcher_check_views(store, [watcher]) if view["watcher_id"] == check_id
    )


def _can_stop_watching(
    store: AppStore,
    watcher: WatcherRecord,
    check: WatcherCheckRecord,
    turn: AgentTaskRecord | None,
    ended: dict[str, bool],
) -> bool:
    """Only a live external observer of an Experiment that has already ended.

    A running Experiment owns its watchers through Stop. An ended one cannot take
    Stop, and its stale observer would otherwise block a fresh Run.
    """

    if (
        turn is None
        or turn.request.get("patch_kind") != "experiment_loop"
        or not watcher.open
        or check.definition != "external"
        or check.removed_at is not None
        or check.completed_at is not None
    ):
        return False
    episode_id = turn.request.get("control_episode_id")
    if not isinstance(episode_id, str) or not episode_id:
        return False
    if episode_id not in ended:
        episode = store.episode(episode_id)
        ended[episode_id] = episode is not None and episode.ending is not None
    return ended[episode_id]


def watcher_check_views(store: AppStore, watchers: list[WatcherRecord]) -> list[dict[str, object]]:
    """One response object per check, in the shape the Web reads.

    The arming turn supplies the node, episode, branch, and continuation.
    """

    turns = store.agent_tasks_by_ids({watcher.origin_turn_id for watcher in watchers})
    ended: dict[str, bool] = {}
    return [
        _check_view(
            store,
            watcher,
            check,
            turns.get(watcher.origin_turn_id),
            _can_stop_watching(store, watcher, check, turns.get(watcher.origin_turn_id), ended),
        )
        for watcher in watchers
        for check in watcher.checks
    ]


def _check_view(
    store: AppStore,
    watcher: WatcherRecord,
    check: WatcherCheckRecord,
    turn: AgentTaskRecord | None,
    can_stop_watching: bool,
) -> dict[str, object]:
    request = turn.request if turn is not None else {}
    arming_target = turn.graph_target if turn is not None else GraphTargetRef()
    patch_kind = "experiment_loop" if request.get("patch_kind") == "experiment_loop" else "work"
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
            "patch_kind": patch_kind,
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
        "can_stop_watching": can_stop_watching,
        "wakes_chat": (
            turn is not None
            and turn.kind in {"node_chat", "project_chat"}
            and patch_kind == "work"
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
    requester = store.space_user(check.cancel_requested_by) if check.cancel_requested_by else None
    view.update(
        check_command=check.check_command,
        log_path=check.log_path,
        cwd=check.cwd,
        purpose=watcher.purpose,
        cancel_command=check.cancel_command,
        cancel_requested_by=check.cancel_requested_by,
        cancel_requested_by_name=requester.display_name if requester else None,
        cancel_requested_at=check.cancel_requested_at,
        cancel_error=check.cancel_error,
        can_cancel=bool(
            check.cancel_command
            and watcher.open
            and check.removed_at is None
            and check.completed_at is None
            and (check.cancel_requested_at is None or check.cancel_error is not None)
        ),
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
