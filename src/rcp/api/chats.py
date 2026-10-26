from __future__ import annotations

import heapq
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import UUID4, AwareDatetime, BaseModel, ConfigDict

from rcp.api.dependencies import (
    get_attachment_store,
    get_catalog,
    get_graph_service,
    get_identity_access,
    get_project_service,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.identity import IdentityAccess
from rcp.attachments import ChatAttachmentStore, ChatAttachmentUpload
from rcp.conversation_worktrees import (
    ConversationWorktreeResponse,
    conversation_worktree_locks,
    project_conversation_worktree,
    remove_conversation_worktree,
)
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import CHAT_PAGE_DEFAULT_LIMIT, CHAT_PAGE_MAX_LIMIT
from rcp.projects import ProjectCatalog
from rcp.runs.browser_lifecycle import close_chat_browser_owners
from rcp.runs.chat_admission import require_chat_graph_target
from rcp.service import (
    ArtifactContextRequest,
    ChatAgentSetup,
    ChatArtifactComments,
    ChatArtifactQuote,
    ChatMessage,
    ChatOriginView,
    ChatSummary,
    ChatSummaryPage,
    ChatTranscript,
    RunRequest,
)
from rcp.storage import AppStore
from rcp.storage.chat_names import ChatNameError

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
AttachmentStoreDependency = Annotated[ChatAttachmentStore, Depends(get_attachment_store)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]


class BranchChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_target: GraphTargetRef
    # One per branch attempt; a retried request returns the chat it created.
    request_id: UUID4


@router.get("/api/projects/{project_id}/chats/{chat_id}/branch", status_code=204)
def branchable_chat(
    project_id: str, chat_id: str, *, catalog: CatalogDependency, store: StoreDependency
) -> None:
    """Refuse an unbranchable chat before the client creates a graph branch for it."""
    try:
        store.require_chat_fork_source(
            catalog.resolve_project_id(project_id), _canonical_chat_id(chat_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/api/projects/{project_id}/chats/{chat_id}/branch",
    dependencies=[Depends(require_project_write_admission)],
    response_model=ChatTranscript,
)
def branch_chat(
    project_id: str,
    chat_id: str,
    body: BranchChatBody,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> ChatTranscript:
    project_id = catalog.resolve_project_id(project_id)
    chat_id = _canonical_chat_id(chat_id)
    target_service = get_graph_service(
        catalog, project_id, body.graph_target.branch_id, initialize=False, write=True
    )
    target_node_ids = set(target_service.history.current_materialization().state.nodes)
    try:
        with conversation_worktree_locks(f"{store.path}:{project_id}:{chat_id}"):
            fork = store.create_chat_fork(
                project_id,
                chat_id,
                body.graph_target,
                str(body.request_id),
                target_node_ids=target_node_ids,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return chat(
        project_id,
        fork.chat_id,
        catalog=catalog,
        store=store,
        branch_id=body.graph_target.branch_id,
    )


class ChatArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    archived: bool


class ChatBrowserPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    browser_requested: bool


class ChatPinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool


class ChatTitleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class ChatDisplay(BaseModel):
    archived: list[str]
    titles: dict[str, str]
    # The acting user's pins, newest first.
    pinned: list[str]


class ChatReadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The finish time of the newest turn the reader saw, as the server reported it.
    read_through: AwareDatetime


class ChatReads(BaseModel):
    """The acting user's read markers; a chat without one is read through `baseline`."""

    baseline: str
    reads: dict[str, str]
    latest_finished: dict[str, str]
    # Loaded turns of these chats never count as unread either.
    archived: list[str]


def _canonical_chat_id(chat_id: str) -> str:
    try:
        canonical = str(uuid.UUID(chat_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="chat_id must be a UUID") from exc
    if canonical != chat_id:
        raise HTTPException(status_code=422, detail="chat_id must be a canonical UUID")
    return canonical


@router.get("/api/projects/{project_id}/chat-display", response_model=ChatDisplay)
def chat_display(
    project_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> ChatDisplay:
    """Archive and names are the project's; pins are the acting user's."""
    project_id = catalog.resolve_project_id(project_id)
    user = identity_access.acting_user(request)
    return ChatDisplay(**store.chat_display(project_id, user.user_id))


@router.get(
    "/api/projects/{project_id}/chats/{chat_id}/browser",
    response_model=ChatBrowserPreference,
)
def chat_browser(
    project_id: str,
    chat_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> ChatBrowserPreference:
    chat_id = _canonical_chat_id(chat_id)
    project_id = catalog.resolve_project_id(project_id)
    return ChatBrowserPreference(
        browser_requested=store.chat_browser_requested(project_id, chat_id)
    )


@router.put(
    "/api/projects/{project_id}/chats/{chat_id}/browser",
    dependencies=[Depends(require_project_write_admission)],
    response_model=ChatBrowserPreference,
)
def set_chat_browser(
    project_id: str,
    chat_id: str,
    body: ChatBrowserPreference,
    background_tasks: BackgroundTasks,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> ChatBrowserPreference:
    """Takes effect at the next turn; turning it off deletes the chat's browser profile."""
    chat_id = _canonical_chat_id(chat_id)
    project_id = catalog.resolve_project_id(project_id)
    store.set_chat_browser_requested(project_id, chat_id, browser_requested=body.browser_requested)
    if not body.browser_requested:
        # Cleanup reaches the execution host, so it runs after the response. An
        # active turn keeps its browser; its owner is closed when the turn ends.
        background_tasks.add_task(_delete_browser_if_still_off, store, project_id, chat_id)
    return body


def _delete_browser_if_still_off(store: AppStore, project_id: str, chat_id: str) -> None:
    close_chat_browser_owners(
        store, project_id, chat_id, delete_profile=True, unless_browser_on=True
    )


@router.post(
    "/api/projects/{project_id}/chats/{chat_id}/archive",
    dependencies=[Depends(require_project_write_admission)],
    response_model=ChatDisplay,
)
def archive_chat(
    project_id: str,
    chat_id: str,
    body: ChatArchiveBody,
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> ChatDisplay:
    """Hide or restore one conversation in the agent list; nothing is deleted."""
    chat_id = _canonical_chat_id(chat_id)
    project_id = catalog.resolve_project_id(project_id)
    user = identity_access.acting_user(request)
    store.set_chat_archived(project_id, chat_id, user.user_id, archived=body.archived)
    if body.archived:
        background_tasks.add_task(
            close_chat_browser_owners, store, project_id, chat_id, delete_profile=False
        )
    return ChatDisplay(**store.chat_display(project_id, user.user_id))


@router.post(
    "/api/projects/{project_id}/chats/{chat_id}/pin",
    dependencies=[Depends(require_project_write_admission)],
    response_model=ChatDisplay,
)
def pin_chat(
    project_id: str,
    chat_id: str,
    body: ChatPinBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> ChatDisplay:
    """Keep one conversation at the top of the agent list, or let it go back."""
    chat_id = _canonical_chat_id(chat_id)
    project_id = catalog.resolve_project_id(project_id)
    user = identity_access.acting_user(request)
    store.set_chat_pinned(project_id, chat_id, user.user_id, pinned=body.pinned)
    return ChatDisplay(**store.chat_display(project_id, user.user_id))


@router.post(
    "/api/projects/{project_id}/chats/{chat_id}/title",
    dependencies=[Depends(require_project_write_admission)],
    response_model=ChatDisplay,
)
def rename_chat(
    project_id: str,
    chat_id: str,
    body: ChatTitleBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> ChatDisplay:
    """Name a conversation in the agent list; its transcript is unchanged."""
    chat_id = _canonical_chat_id(chat_id)
    project_id = catalog.resolve_project_id(project_id)
    user = identity_access.acting_user(request)
    try:
        store.set_chat_title(project_id, chat_id, user.user_id, body.title)
    except ChatNameError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return ChatDisplay(**store.chat_display(project_id, user.user_id))


@router.get("/api/projects/{project_id}/chat-reads", response_model=ChatReads)
def chat_reads(
    project_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    branch_id: str | None = None,
    inventory: bool = False,
) -> ChatReads:
    """Latest finishes cover the selected target or the whole project inventory."""
    service = get_graph_service(
        catalog, project_id, None if inventory else branch_id, initialize=False
    )
    project_id = catalog.resolve_project_id(project_id)
    user = identity_access.acting_user(request)
    return ChatReads(
        **store.chat_reads(
            project_id, user.user_id, None if inventory else service.history.graph_target
        )
    )


# A viewer's own marker, not project work, so it skips the write-admission fence.
@router.post("/api/projects/{project_id}/chats/{chat_id}/read", response_model=ChatReads)
def mark_chat_read(
    project_id: str,
    chat_id: str,
    body: ChatReadBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    branch_id: str | None = None,
    inventory: bool = False,
) -> ChatReads:
    """Record that the acting user has seen this conversation's turns up to a time."""
    chat_id = _canonical_chat_id(chat_id)
    service = get_graph_service(
        catalog, project_id, None if inventory else branch_id, initialize=False, write=True
    )
    project_id = catalog.resolve_project_id(project_id)
    user = identity_access.acting_user(request)
    store.mark_chat_read(project_id, chat_id, user.user_id, body.read_through)
    return ChatReads(
        **store.chat_reads(
            project_id, user.user_id, None if inventory else service.history.graph_target
        )
    )


@router.post(
    "/api/projects/{project_id}/chats/{chat_id}/attachments",
    response_model=ChatAttachmentUpload,
)
def upload_chat_attachment(
    project_id: str,
    chat_id: str,
    file: Annotated[UploadFile, File()],
    client_id: Annotated[str, Form()],
    attachment_set_id: Annotated[str | None, Form()] = None,
    *,
    catalog: CatalogDependency,
    attachment_store: AttachmentStoreDependency,
) -> ChatAttachmentUpload:
    require_registered_project(catalog, project_id)
    try:
        return attachment_store.add(
            project_id=project_id,
            chat_id=chat_id,
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


@router.delete(
    "/api/projects/{project_id}/chats/{chat_id}/attachments/{attachment_id}",
)
def remove_chat_attachment(
    project_id: str,
    chat_id: str,
    attachment_id: str,
    client_id: str,
    attachment_set_id: str,
    *,
    catalog: CatalogDependency,
    attachment_store: AttachmentStoreDependency,
) -> dict[str, bool]:
    require_registered_project(catalog, project_id)
    try:
        attachment_store.remove(
            project_id=project_id,
            chat_id=chat_id,
            client_id=client_id,
            attachment_set_id=attachment_set_id,
            attachment_id=attachment_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"removed": True}


@router.get(
    "/api/projects/{project_id}/chats",
    response_model=ChatSummaryPage,
)
def chats(
    project_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(
        default=CHAT_PAGE_DEFAULT_LIMIT,
        ge=1,
        le=CHAT_PAGE_MAX_LIMIT,
    ),
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    branch_id: str | None = None,
    inventory: bool = False,
) -> ChatSummaryPage:
    # Only the retained main service owns the project-wide canonical scan cache.
    service = (
        get_project_service(catalog, project_id)
        if inventory
        else get_graph_service(catalog, project_id, branch_id, initialize=False)
    )
    project_id = catalog.resolve_project_id(project_id)
    return service.chat_summaries(
        offset=offset,
        limit=limit,
        inventory=inventory,
        task_summaries=[
            ChatSummary.model_validate(row) for row in store.chat_inventory(project_id)
        ],
    )


@router.get(
    "/api/projects/{project_id}/chats/{chat_id}",
    response_model=ChatTranscript,
)
def chat(
    project_id: str,
    chat_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    branch_id: str | None = None,
) -> ChatTranscript:
    service = get_graph_service(catalog, project_id, branch_id, initialize=False)
    try:
        transcript = service.chat_transcript(chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    project_id = catalog.resolve_project_id(project_id)
    summary = next(iter(store.chat_inventory(project_id, chat_id)), None)
    if (
        summary is not None
        and summary["conversation_kind"] == "auto_research"
        and summary["graph_target"] == service.history.graph_target.model_dump()
    ):
        # The orchestrator's conversation is its chain's tasks and human mail; human turns
        # after the chain ends add a chat file. Each source keeps its own order; the two interleave.
        messages = list(
            heapq.merge(
                [
                    ChatMessage.model_validate(message)
                    for message in store.orchestrator_chat_messages(project_id, chat_id)
                ],
                transcript.messages if transcript is not None else [],
                key=lambda message: datetime.fromisoformat(message.timestamp),
            )
        )
        if messages:
            transcript = ChatTranscript.model_validate(
                {**summary, "message_count": len(messages), "messages": messages}
            )
    if (
        transcript is None
        and summary is not None
        and summary.get("origin") is not None
        and summary["graph_target"] == service.history.graph_target.model_dump()
    ):
        transcript = ChatTranscript.model_validate({**summary, "messages": []})
    if transcript is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    transcript = transcript.model_copy(
        update={
            "graph_title": "Main"
            if transcript.graph_target.kind == "main"
            else (transcript.graph_target.branch_id or "")[:8],
            # The same setup the chat list shows, so every client sends on one agent.
            "agent_setup": ChatAgentSetup.model_validate(summary["agent_setup"])
            if summary is not None
            and summary.get("agent_setup") is not None
            and summary["graph_target"] == transcript.graph_target.model_dump()
            else transcript.agent_setup,
            **(
                {
                    key: ChatOriginView.model_validate(summary[key])
                    if key == "origin" and summary[key] is not None
                    else summary[key]
                    for key in (
                        "origin",
                        "conversation_kind",
                        "orchestrator_episode_id",
                        "human_turn_refusal",
                    )
                    if key in summary
                }
                if summary is not None
                else {}
            ),
        }
    )
    for message in transcript.messages:
        if message.operation_id is not None:
            message.browser_status = store.browser_turn_status(message.operation_id).public()
            # An old turn can fall outside the bounded task list the chat polls.
            task = store.agent_task(message.operation_id)
            if message.role == "assistant":
                message.agent_status = task.agent_status if task is not None else None
            elif task is not None and message.steering is None:
                message.artifact_comments = _artifact_comments(store, task.request)
    return transcript


def _artifact_comments(store: AppStore, request: dict[str, object]) -> ChatArtifactComments | None:
    """What the turn's numbered artifact comments were drawn on, so the bubble can quote it."""
    raw = request.get("artifact_context")
    if raw is None:
        return None
    context = ArtifactContextRequest.model_validate(raw)
    if not context.selections:
        return None
    artifact = store.artifact(context.artifact_id)
    quotes = []
    for selection in context.selections:
        if selection.kind == "text":
            quote = selection.text
        elif selection.elements is None:
            quote = selection.labels  # A box from the viewer before elements were named.
        else:
            quote = ", ".join(element.label or element.text for element in selection.elements)
        quotes.append(
            ChatArtifactQuote(
                kind=selection.kind, quote=" ".join(quote.split()), comment=selection.comment
            )
        )
    return ChatArtifactComments(
        artifact_id=context.artifact_id,
        name=artifact.source_name if artifact is not None else None,
        quotes=quotes,
    )


__all__ = [
    "archive_chat",
    "chat",
    "chat_display",
    "chat_reads",
    "chats",
    "mark_chat_read",
    "pin_chat",
    "rename_chat",
    "remove_chat_attachment",
    "router",
    "upload_chat_attachment",
]


@router.get("/api/projects/{project_id}/chats/{chat_id}/worktree")
def conversation_worktree_controls(
    project_id: str,
    chat_id: str,
    *,
    catalog: CatalogDependency,
    store: Annotated[AppStore, Depends(get_store)],
    run_on: str | None = None,
    inspect_removal: bool = False,
    run_truth_scope: Annotated[list[str] | None, Query()] = None,
    chat_scope: Literal["node", "project"] = "project",
    node_id: str | None = None,
    branch_id: str | None = None,
) -> ConversationWorktreeResponse:
    service = get_graph_service(catalog, project_id, branch_id, initialize=False)
    try:
        require_chat_graph_target(service, store, project_id, chat_id)
        profile = service.resolve_agent_profile(
            "node_chat" if chat_scope == "node" else "project_chat", run_on=run_on
        )
        return project_conversation_worktree(
            service,
            store,
            project_id,
            RunRequest(
                chat_id=chat_id,
                chat_scope=chat_scope,
                node_id=node_id,
                run_on=profile.run_on,
                run_truth_scope=[] if run_truth_scope == [""] else run_truth_scope,
            ),
            inspect_removal=inspect_removal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete(
    "/api/projects/{project_id}/chats/{chat_id}/worktree",
    dependencies=[Depends(require_project_write_admission)],
)
def delete_conversation_worktree(
    project_id: str,
    chat_id: str,
    *,
    catalog: CatalogDependency,
    store: Annotated[AppStore, Depends(get_store)],
    branch_id: str | None = None,
) -> ConversationWorktreeResponse:
    service = get_graph_service(catalog, project_id, branch_id, write=True)
    try:
        with conversation_worktree_locks(f"{store.path}:{project_id}:{chat_id}"):
            require_chat_graph_target(service, store, project_id, chat_id)
            binding = remove_conversation_worktree(service, store, project_id, chat_id)
        return project_conversation_worktree(
            service,
            store,
            project_id,
            RunRequest(
                chat_id=chat_id,
                chat_scope=binding.chat_scope,
                node_id=binding.node_id,
                run_on=binding.machine,
                run_truth_scope=[binding.repository_alias],
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
