"""Member-owned thread index and controls, sharing the existing chat owners."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rcp.api.chats import (
    CatalogDependency,
    ChatBrowserPreference,
    ChatReadBody,
    ChatTitleBody,
    IdentityDependency,
    StoreDependency,
    _delete_browser_if_still_off,
    chat,
    mark_chat_read,
    rename_chat,
    set_chat_browser,
)
from rcp.api.dependencies import project_write_admission
from rcp.api.tasks import (
    BackgroundTasksDependency,
    RetryAgentTaskRequest,
    SteerAgentTaskRequest,
    _agent_task_response,
    _artifact_viewer_response,
    _reject_history_only_control,
    content_agent_artifact,
    download_agent_artifact,
)
from rcp.providers import ProviderId
from rcp.runs.agent_names import DEFAULT_AGENT_NAMES
from rcp.runs.agent_threads import task_owner, thread_projection, thread_scope_id
from rcp.runs.space_chat import SpaceThreadOwner
from rcp.runs.steering import begin_chat_steer, finish_chat_steer
from rcp.service import ChatTranscript, RunRequest, read_space_chat_transcript
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.agent_threads import AgentThreadRecord
from rcp.storage.chat_names import ChatNameError

router = APIRouter()


def owned_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = store.agent_thread(thread_id)
    if thread is None or thread.user_id != user_id:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread.project_id and not store.is_project_member(thread.project_id, user_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


@router.get("/api/agent-threads")
def agent_threads(request: Request, *, store: StoreDependency, identity_access: IdentityDependency):
    user = identity_access.acting_user(request)
    project_ids = store.member_project_ids(user.user_id)
    threads = store.ensure_agent_threads(
        user.user_id, sorted(project_ids), default_names=DEFAULT_AGENT_NAMES
    )
    for thread in threads:
        if thread.project_id and thread.project_id not in project_ids and not thread.closed_at:
            store.close_agent_thread(thread.thread_id, reason="access_lost")
    rows = [
        thread_projection(store, thread)
        for thread in threads
        if thread.project_id is None or thread.project_id in project_ids
    ]
    rows.sort(key=lambda row: str(row["last_activity_at"] or ""), reverse=True)
    rows.sort(key=lambda row: row["kind"] != "space")
    return {"threads": rows}


@router.get("/api/agent-threads/{thread_id}/transcript", response_model=ChatTranscript)
def thread_transcript(
    thread_id: str,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    title = store.chat_name(thread_scope_id(store, thread), thread_id, "")
    if thread.project_id:
        try:
            return chat(thread.project_id, thread_id, catalog=catalog, store=store)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    else:
        transcript = read_space_chat_transcript(
            store.path.parent, thread.user_id, thread_id, title=title
        )
        if transcript is not None:
            return transcript
    return ChatTranscript(
        chat_id=thread_id,
        kind="project_chat" if thread.project_id else "space_chat",
        node_id=None,
        title=title,
        updated_at=thread.created_at,
        message_count=0,
        last_message_preview="",
    )


@router.post("/api/agent-threads/{thread_id}/title")
def rename_thread(
    thread_id: str,
    body: ChatTitleBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.project_id:
        with project_write_admission(thread.project_id, request):
            rename_chat(
                thread.project_id,
                thread_id,
                body,
                request,
                catalog=catalog,
                store=store,
                identity_access=identity_access,
            )
        return thread_projection(store, thread)
    try:
        store.set_chat_title(thread_scope_id(store, thread), thread_id, thread.user_id, body.title)
    except ChatNameError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return thread_projection(store, thread)


@router.post("/api/agent-threads/{thread_id}/read")
def read_thread(
    thread_id: str,
    body: ChatReadBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.project_id:
        mark_chat_read(
            thread.project_id,
            thread_id,
            body,
            request,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
        )
    else:
        store.mark_chat_read(
            thread_scope_id(store, thread), thread_id, thread.user_id, body.read_through
        )
    return thread_projection(store, thread)


@router.put("/api/agent-threads/{thread_id}/browser", response_model=ChatBrowserPreference)
def thread_browser(
    thread_id: str,
    body: ChatBrowserPreference,
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.project_id:
        with project_write_admission(thread.project_id, request):
            return set_chat_browser(
                thread.project_id, thread_id, body, background_tasks, catalog=catalog, store=store
            )
    scope = thread_scope_id(store, thread)
    store.set_chat_browser_requested(scope, thread_id, browser_requested=body.browser_requested)
    if not body.browser_requested:
        background_tasks.add_task(_delete_browser_if_still_off, store, scope, thread_id)
    return body


class SpaceTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(min_length=1)
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    timezone: str | None = None


def _space_task(store: AppStore, thread: AgentThreadRecord, operation_id: str) -> AgentTaskRecord:
    task = store.agent_task(operation_id)
    if (
        thread.kind != "space"
        or task is None
        or task.kind != "space_chat"
        or task.project_id != store.space_id
        or task.request.get("chat_id") != thread.thread_id
        or not task.visible
    ):
        raise HTTPException(status_code=404, detail="Agent task not found")
    return task


@router.post("/api/agent-threads/{thread_id}/turns", status_code=202)
def space_turn(
    thread_id: str,
    body: SpaceTurnBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    if thread.kind != "space":
        raise HTTPException(status_code=404, detail="Space thread not found")
    owner = SpaceThreadOwner(store, catalog, thread_id)
    profile = owner.resolve_agent_profile(
        provider=body.provider, model=body.model, reasoning=body.reasoning
    )
    try:
        turn = RunRequest(
            chat_id=thread_id,
            chat_scope="project",
            mode="work",
            message=body.message,
            provider=profile.provider,
            model=profile.model,
            reasoning=profile.reasoning,
            run_on=profile.run_on,
            timezone=body.timezone,
            browser_requested=store.chat_browser_requested(store.space_id, thread_id),
        )
        task = background_tasks.start(store.space_id, "space_chat", turn, authorized_by=authorized)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"operation_id": task.operation_id}


@router.get("/api/agent-threads/{thread_id}/tasks/{operation_id}")
def space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    task = _space_task(store, thread, operation_id)
    detail = _agent_task_response(store, task, background_tasks)
    detail["events"] = [
        event.model_dump(mode="json") for event in store.agent_task_events(operation_id)
    ]
    return detail


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/pause", status_code=202)
def pause_space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    _reject_history_only_control(_space_task(store, thread, operation_id))
    try:
        return background_tasks.pause(operation_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/resume", status_code=202)
def resume_space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    _reject_history_only_control(_space_task(store, thread, operation_id))
    try:
        return background_tasks.resume(operation_id, authorized_by=authorized).model_dump(
            mode="json"
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/retry", status_code=202)
def retry_space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    body: RetryAgentTaskRequest | None = None,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    _reject_history_only_control(_space_task(store, thread, operation_id))
    if body is not None and body.run_on not in {None, "local"}:
        raise HTTPException(status_code=422, detail="Space threads run on the backend machine")
    try:
        return background_tasks.retry(
            operation_id,
            authorized_by=authorized,
            **(body.model_dump(exclude_none=True) if body else {}),
        ).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/steer")
def steer_space_task(
    thread_id: str,
    operation_id: str,
    body: SteerAgentTaskRequest,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    task = _space_task(store, thread, operation_id)
    owner = task_owner(store, task, catalog=catalog)
    if not isinstance(owner, SpaceThreadOwner):
        raise HTTPException(status_code=404, detail="Space thread not found")
    try:
        delivery = begin_chat_steer(
            owner,
            background_tasks,
            task,
            message_id=str(body.message_id),
            attempt=body.attempt,
            expected_turn_id=body.expected_turn_id,
            text=body.message,
        )
        return finish_chat_steer(owner, background_tasks, task, delivery)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.api_route(
    "/api/agent-threads/{thread_id}/tasks/{operation_id}/artifacts/{artifact_id}/{action}",
    methods=["GET", "HEAD"],
)
async def space_artifact(
    thread_id: str,
    operation_id: str,
    artifact_id: str,
    action: Literal["content", "download", "viewer"],
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    user = identity_access.acting_user(request)
    thread = owned_thread(store, thread_id, user.user_id)
    _space_task(store, thread, operation_id)
    if action == "viewer":
        return await _artifact_viewer_response(
            store.space_id,
            operation_id,
            artifact_id,
            catalog=catalog,
            store=store,
            head=request.method == "HEAD",
        )
    reader = content_agent_artifact if action == "content" else download_agent_artifact
    return await reader(
        store.space_id,
        operation_id,
        artifact_id,
        request,
        catalog=catalog,
        store=store,
    )
