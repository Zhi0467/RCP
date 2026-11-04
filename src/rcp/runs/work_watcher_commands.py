"""A member's Work chat listing, stopping or cancelling the watchers it armed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rcp.agents.command_protocol import CommandResponse, WatcherCommandRequest
from rcp.agents.staged_command_client import watcher_usages
from rcp.storage import GraphWatcherRecord, StoredWatcherRecord, WatcherRecord

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.storage import AgentTaskRecord


def chat_armed_watcher(task: AgentTaskRecord, watcher: StoredWatcherRecord) -> bool:
    """Whether a Work turn of ``task``'s chat armed this watcher for the chat itself.

    An Experiment loop's watcher wakes its episode, not the chat, so it stops
    with that episode's Stop instead.
    """

    chat_id = task.request.get("chat_id")
    return (
        bool(chat_id)
        and watcher.project_id == task.project_id
        and watcher.chat_id == chat_id
        and watcher.origin_task_kind in {"node_chat", "project_chat"}
        and watcher.continuation.patch_kind == "work"
    )


def handle_work_watcher_command(
    execution: AgentTaskExecution, request: WatcherCommandRequest
) -> CommandResponse:
    """List this chat's live watchers, or stop or cancel one of them now."""

    def refuse(message: str, code: str) -> CommandResponse:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=message,
            result={"code": code},
        )

    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None or task.authorized_by is None:
        return CommandResponse(
            request_id=request.request_id,
            status="unavailable",
            message="Watcher control is unavailable for this turn.",
        )
    arguments = request.arguments
    if arguments.action == "list":
        live = [
            _summary(watcher)
            for watcher in store.watchers(
                task.project_id, chat_id=str(task.request.get("chat_id") or "")
            )
            if chat_armed_watcher(task, watcher)
            and watcher.status in {"active", "degraded"}
            and not watcher.notified
        ]
        return CommandResponse(
            request_id=request.request_id, status="ok", result={"watchers": live}
        )
    assert arguments.watcher_id is not None
    watcher = store.watcher(arguments.watcher_id)
    if watcher is None or not chat_armed_watcher(task, watcher):
        return refuse("This chat may control only watchers it armed.", "watcher_not_armed_here")
    if arguments.action == "stop":
        try:
            (watcher,) = store.stop_watchers(
                task.project_id, [watcher.watcher_id], observing_only=True, stopped_by="agent"
            )
        except (KeyError, ValueError) as exc:
            return refuse(str(exc), "watcher_not_stoppable")
    else:
        if execution.watcher_canceller is None:
            return CommandResponse(
                request_id=request.request_id,
                status="unavailable",
                message="Watcher cancel is unavailable for this turn.",
            )
        try:
            watcher = execution.watcher_canceller(
                task.project_id, watcher.watcher_id, task.authorized_by.user_id
            )
        except (KeyError, ValueError) as exc:
            return refuse(str(exc), "watcher_not_cancellable")
        if watcher.cancel_error:
            return CommandResponse(
                request_id=request.request_id,
                status="unavailable",
                message=watcher.cancel_error,
                result=_summary(watcher),
            )
    store.record_agent_task_event(
        execution.operation_id,
        f"Watcher {watcher.watcher_id} {arguments.action}: {watcher.status}.",
    )
    return CommandResponse(request_id=request.request_id, status="ok", result=_summary(watcher))


def _summary(watcher: StoredWatcherRecord) -> dict[str, object]:
    summary: dict[str, object] = {"watcher_id": watcher.watcher_id, "status": watcher.status}
    if isinstance(watcher, GraphWatcherRecord):
        summary["condition"] = watcher.condition.model_dump(mode="json")
    elif isinstance(watcher, WatcherRecord):
        summary.update(
            log_path=watcher.log_path,
            check_command=watcher.check_command,
            can_cancel=watcher.can_cancel,
        )
    return summary


def work_watcher_command_usage(client: str) -> str:
    """The usage a Work turn renders when it is granted ``watcher``."""

    listing, stop, cancel = (f"`{client} {usage}`" for usage in watcher_usages())
    return (
        f"Watchers this chat armed: {listing} shows the live ones with their ids. When the "
        f"human asks, {stop} stops watching: the job keeps running and no wake comes. "
        f"{cancel} runs that watcher's cancel_command now to end its job, as the human's "
        "Cancel would. Both act at once. An Experiment's own watchers stop with its episode."
    )
