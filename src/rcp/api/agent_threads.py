"""Member-owned thread index and controls, sharing the existing chat owners."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, Self

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator

from rcp.api.chats import (
    AttachmentStoreDependency,
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
from rcp.attachments import ChatAttachmentUpload
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


@router.post("/api/agent-threads/{thread_id}/attachments", response_model=ChatAttachmentUpload)
def upload_space_attachment(
    thread_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    client_id: Annotated[str, Form()],
    attachment_set_id: Annotated[str | None, Form()] = None,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
) -> ChatAttachmentUpload:
    try:
        thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
        if thread.kind != "space":
            raise HTTPException(status_code=404, detail="Space thread not found")
        return attachment_store.add(
            project_id=store.space_id,
            chat_id=thread_id,
            client_id=client_id,
            filename=file.filename or "",
            media_type=file.content_type,
            source=file.file,
            attachment_set_id=attachment_set_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        file.file.close()


@router.delete("/api/agent-threads/{thread_id}/attachments/{attachment_id}")
def remove_space_attachment(
    thread_id: str,
    attachment_id: str,
    client_id: str,
    attachment_set_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
) -> dict[str, bool]:
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.kind != "space":
        raise HTTPException(status_code=404, detail="Space thread not found")
    try:
        attachment_store.remove(
            project_id=store.space_id,
            chat_id=thread_id,
            client_id=client_id,
            attachment_set_id=attachment_set_id,
            attachment_id=attachment_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"removed": True}


class SpaceTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(min_length=1)
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    timezone: str | None = None
    attachment_set_id: str | None = Field(default=None, min_length=1)
    attachment_client_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_attachment_pair(self) -> Self:
        if (self.attachment_set_id is None) != (self.attachment_client_id is None):
            raise ValueError("Attachments require both attachment_set_id and attachment_client_id")
        return self


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
    attachment_store: AttachmentStoreDependency,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    if thread.kind != "space":
        raise HTTPException(status_code=404, detail="Space thread not found")
    try:
        with store.client_request_admission(
            store.space_id,
            authorized.user_id,
            str(idempotency_key) if idempotency_key is not None else None,
            f"agent-threads/{thread_id}/turns",
        ) as existing:
            if existing is not None:
                return {"operation_id": existing.operation_id}
            owner = SpaceThreadOwner(store, catalog, thread_id)
            profile = owner.resolve_agent_profile(
                provider=body.provider, model=body.model, reasoning=body.reasoning
            )
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
            operation_id = str(uuid.uuid4())
            claimed = None
            if body.attachment_set_id is not None:
                claimed = attachment_store.claim(
                    project_id=store.space_id,
                    chat_id=thread_id,
                    client_id=body.attachment_client_id,
                    attachment_set_id=body.attachment_set_id,
                    operation_id=operation_id,
                )
                turn = turn.model_copy(
                    update={
                        "attachment_batch_id": claimed.attachment_batch_id,
                        "attachments": claimed.attachments,
                    }
                )
            try:
                task = background_tasks.start(
                    store.space_id,
                    "space_chat",
                    turn,
                    operation_id=operation_id,
                    authorized_by=authorized,
                )
            except BaseException:
                if claimed is not None and store.agent_task(operation_id) is None:
                    attachment_store.release(claimed.attachment_batch_id, operation_id)
                raise
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
