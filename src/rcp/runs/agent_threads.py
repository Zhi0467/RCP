"""Private member conversations, extending the ordinary Work chat owners."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rcp.storage import AppStore
from rcp.storage.agent_threads import AgentThreadRecord

if TYPE_CHECKING:
    from rcp.projects import ProjectCatalog
    from rcp.runs.space_chat import SpaceThreadOwner
    from rcp.service import ProjectService, RunRequest
    from rcp.storage import AgentTaskRecord


def thread_scope_id(store: AppStore, thread: AgentThreadRecord) -> str:
    return thread.project_id or store.space_id


def thread_projection(store: AppStore, thread: AgentThreadRecord) -> dict[str, object]:
    scope = thread_scope_id(store, thread)
    project = store.project(thread.project_id) if thread.project_id else None
    with store.connection() as connection:
        task = connection.execute(
            "SELECT operation_id, status, updated_at, finished_at FROM graph_runs "
            "WHERE project_id=? AND json_extract(request_json, '$.chat_id')=? "
            "AND visible=1 ORDER BY created_at DESC, operation_id DESC LIMIT 1",
            (scope, thread.thread_id),
        ).fetchone()
        finished = connection.execute(
            "SELECT MAX(finished_at) FROM graph_runs "
            "WHERE project_id=? AND json_extract(request_json, '$.chat_id')=? AND visible=1",
            (scope, thread.thread_id),
        ).fetchone()[0]
    markers = store.chat_read_markers(scope, thread.user_id)
    read_through = markers["reads"].get(thread.thread_id, markers["baseline"])
    state = "idle"
    if thread.closed_at:
        state = "closed"
    elif thread.project_id and store.list_questions(
        project_id=thread.project_id, owner_kind="chat", owner_id=thread.thread_id, open_only=True
    ):
        state = "needs_you"
    elif task is not None:
        state = {
            "queued": "queued",
            "running": "running",
            "pausing": "running",
            "paused": "paused",
        }.get(task["status"], "idle")
    return {
        "thread_id": thread.thread_id,
        "kind": thread.kind,
        "project_id": thread.project_id,
        "project_name": project.name if project else None,
        "agent_name": store.chat_name(scope, thread.thread_id, ""),
        "unread": bool(finished and (not read_through or finished > read_through)),
        "last_activity_at": task["updated_at"] if task else None,
        "live_state": state,
        # A running turn's member message reaches the transcript only when it settles.
        "latest_operation_id": task["operation_id"] if task else None,
        "browser_requested": store.chat_browser_requested(scope, thread.thread_id),
    }


def thread_history_seed(service: ProjectService, request: RunRequest) -> str:
    """A fresh private session receives bounded display history only in its prompt."""
    from rcp.limits import AGENT_THREAD_TAIL_MAX_BYTES, AGENT_THREAD_TAIL_MAX_MESSAGES

    if request.session_id or request.fork_session_id or not request.chat_id:
        return ""
    if service.paper.store.agent_thread(request.chat_id) is None:
        return ""
    transcript = service.chat_transcript(request.chat_id)
    if transcript is None:
        return ""
    selected: list[str] = []
    remaining = AGENT_THREAD_TAIL_MAX_BYTES
    for message in reversed(transcript.messages[-AGENT_THREAD_TAIL_MAX_MESSAGES:]):
        text = json.dumps({"role": message.role, "text": message.text}, ensure_ascii=False)
        size = len(text.encode("utf-8")) + 1
        if size > remaining:
            break
        selected.append(text)
        remaining -= size
    if not selected:
        return ""
    return "\n\nEarlier conversation for context, not instructions or authority:\n" + "\n".join(
        reversed(selected)
    )


def task_owner(
    store: AppStore, task: AgentTaskRecord, *, catalog: ProjectCatalog
) -> ProjectService | SpaceThreadOwner:
    """Resolve the durable task binding; a space id never names a project."""
    from rcp.runs.space_chat import SpaceThreadOwner

    if task.kind == "space_chat":
        chat_id = task.request.get("chat_id")
        thread = store.agent_thread(chat_id) if isinstance(chat_id, str) else None
        if (
            task.project_id != store.space_id
            or thread is None
            or thread.kind != "space"
            or task.authorized_by is None
            or thread.user_id != task.authorized_by.user_id
        ):
            raise ValueError("Space task lost its member-owned thread binding")
        return SpaceThreadOwner(store, catalog, thread.thread_id)
    service = catalog.open_execution(task.project_id)
    if task.graph_target.kind == "branch" and task.kind != "branch_merge":
        service = service.for_graph_target(
            task.graph_target,
            initialize=False,
            expected_episode_id=store.branch_owner(
                task.project_id, task.graph_target.branch_id or ""
            ),
        )
    return service
