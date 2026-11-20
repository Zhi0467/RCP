"""A member's Space agent thread keeps its comment threads like a project chat, owner only."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request

from rcp.api.agent_threads import IdentityDependency, StoreDependency, owned_space_thread
from rcp.api.comments import _response
from rcp.api.identity import IdentityAccess
from rcp.comments.models import CommentPostBody, CreateCommentThread
from rcp.storage import AppStore
from rcp.storage.comments import CommentTargetRoute, comment_target_not_found

router = APIRouter()


def _owner(store: AppStore, request: Request, identity: IdentityAccess, thread_id: str) -> str:
    user_id = identity.acting_user(request).user_id
    owned_space_thread(store, thread_id, user_id)
    return user_id


def _route(
    store: AppStore, user_id: str, thread_id: str, kind: str, target_id: str
) -> CommentTargetRoute:
    route = store.comment_target_route(user_id, kind, target_id, project_id=store.space_id)
    if route is None or route.chat_id != thread_id:
        raise comment_target_not_found()
    return route


def _sender(request: Request, identity: IdentityAccess, user_id: str) -> str | None:
    try:
        identity.require_patch_capable_identity(request)
    except HTTPException:
        return None
    return user_id


def _respond(
    store: AppStore,
    request: Request,
    identity: IdentityAccess,
    user_id: str,
    thread_id: str,
    thread: dict[str, Any],
) -> dict[str, object]:
    route = _route(store, user_id, thread_id, thread["target_kind"], thread["target_id"])
    return {"thread": _response(thread, route, _sender(request, identity, user_id))}


def _own_thread(store: AppStore, user_id: str, thread_id: str, comment_id: str) -> None:
    thread = store.get_comment_thread(store.space_id, comment_id)
    _route(store, user_id, thread_id, thread["target_kind"], thread["target_id"])


@router.get("/api/agent-threads/{thread_id}/comment-threads")
def list_space_threads(
    thread_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    target_kind: Literal["artifact", "reply"],
    target_id: str | None = None,
    chat_id: str | None = None,
) -> dict[str, object]:
    """One target's threads, or with `chat_id` every reply thread in this thread."""
    user_id = _owner(store, request, identity_access, thread_id)
    sender = _sender(request, identity_access, user_id)
    if chat_id is not None and target_id is None and target_kind == "reply":
        if chat_id != thread_id:
            raise comment_target_not_found()
        listed = store.list_chat_reply_threads(store.space_id, chat_id, user_id)
        if listed is None:
            raise comment_target_not_found()
        return {"threads": [_response(thread, route, sender) for thread, route in listed]}
    if target_id is None or chat_id is not None:
        raise HTTPException(status_code=422, detail="Name one commented item.")
    route = _route(store, user_id, thread_id, target_kind, target_id)
    return {
        "threads": [
            _response(thread, route, sender)
            for thread in store.list_comment_threads(store.space_id, target_kind, target_id)
        ]
    }


@router.get("/api/agent-threads/{thread_id}/comment-threads/{comment_id}")
def get_space_thread(
    thread_id: str,
    comment_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    user_id = _owner(store, request, identity_access, thread_id)
    thread = store.get_comment_thread(store.space_id, comment_id)
    return _respond(store, request, identity_access, user_id, thread_id, thread)


@router.post("/api/agent-threads/{thread_id}/comment-threads")
def create_space_thread(
    thread_id: str,
    body: CreateCommentThread,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    user_id = _owner(store, request, identity_access, thread_id)
    _route(store, user_id, thread_id, body.target.kind, body.target.id)
    thread = store.create_comment_thread(
        store.space_id,
        target_kind=body.target.kind,
        target_id=body.target.id,
        anchor=body.anchor.model_dump(mode="json"),
        drawn_version=body.drawn_version,
        text=body.text,
        user_id=user_id,
    )
    return _respond(store, request, identity_access, user_id, thread_id, thread)


@router.post("/api/agent-threads/{thread_id}/comment-threads/{comment_id}/posts")
def reply_space_thread(
    thread_id: str,
    comment_id: str,
    body: CommentPostBody,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    user_id = _owner(store, request, identity_access, thread_id)
    _own_thread(store, user_id, thread_id, comment_id)
    thread = store.add_comment_post(store.space_id, comment_id, user_id=user_id, text=body.text)
    return _respond(store, request, identity_access, user_id, thread_id, thread)


def _settle(
    store: AppStore,
    request: Request,
    identity: IdentityAccess,
    thread_id: str,
    comment_id: str,
    resolved: bool,
) -> dict[str, object]:
    user_id = _owner(store, request, identity, thread_id)
    _own_thread(store, user_id, thread_id, comment_id)
    change = store.resolve_comment_thread if resolved else store.reopen_comment_thread
    thread = change(store.space_id, comment_id, user_id=user_id)
    return _respond(store, request, identity, user_id, thread_id, thread)


@router.post("/api/agent-threads/{thread_id}/comment-threads/{comment_id}/resolve")
def resolve_space_thread(
    thread_id: str,
    comment_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    return _settle(store, request, identity_access, thread_id, comment_id, True)


@router.post("/api/agent-threads/{thread_id}/comment-threads/{comment_id}/reopen")
def reopen_space_thread(
    thread_id: str,
    comment_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    return _settle(store, request, identity_access, thread_id, comment_id, False)
