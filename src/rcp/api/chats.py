from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from rcp.api.dependencies import (
    get_attachment_store,
    get_catalog,
    get_graph_service,
    get_store,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.attachments import ChatAttachmentStore, ChatAttachmentUpload
from rcp.conversation_worktrees import (
    ConversationWorktreeResponse,
    conversation_worktree_locks,
    project_conversation_worktree,
    remove_conversation_worktree,
)
from rcp.limits import CHAT_PAGE_DEFAULT_LIMIT, CHAT_PAGE_MAX_LIMIT
from rcp.projects import ProjectCatalog
from rcp.runs.chat_admission import require_chat_graph_target
from rcp.service import ChatSummaryPage, ChatTranscript, RunRequest
from rcp.storage import AppStore

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
AttachmentStoreDependency = Annotated[ChatAttachmentStore, Depends(get_attachment_store)]


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
    branch_id: str | None = None,
) -> ChatSummaryPage:
    service = get_graph_service(catalog, project_id, branch_id, initialize=False)
    return service.chat_summaries(offset=offset, limit=limit)


@router.get(
    "/api/projects/{project_id}/chats/{chat_id}",
    response_model=ChatTranscript,
)
def chat(
    project_id: str,
    chat_id: str,
    *,
    catalog: CatalogDependency,
    branch_id: str | None = None,
) -> ChatTranscript:
    service = get_graph_service(catalog, project_id, branch_id, initialize=False)
    try:
        transcript = service.chat_transcript(chat_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if transcript is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return transcript


__all__ = [
    "chat",
    "chats",
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
    service = get_graph_service(catalog, project_id, branch_id)
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
