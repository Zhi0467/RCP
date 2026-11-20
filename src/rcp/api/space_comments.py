"""Per-agent comment routes share the project comment handlers and placement scope."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request

from rcp.agents.agent_owner import resolve_agent
from rcp.api import comments
from rcp.api.agent_threads import IdentityDependency, StoreDependency
from rcp.api.identity import IdentityAccess
from rcp.comments.models import CommentPostBody, CreateCommentThread
from rcp.storage import AppStore
from rcp.storage.comments import CommentTargetRoute, comment_target_not_found

router = APIRouter()


def _owner(
    store: AppStore, request: Request, identity: IdentityAccess, thread_id: str
) -> tuple[str, str]:
    user_id = identity.acting_user(request).user_id
    owner = resolve_agent(store, thread_id, user_id)
    return user_id, owner.scope_id


def _route(
    store: AppStore, user_id: str, thread_id: str, scope: str, kind: str, target_id: str
) -> CommentTargetRoute:
    route = store.comment_target_route(user_id, kind, target_id, project_id=scope)
    if route is None or route.chat_id != thread_id:
        raise comment_target_not_found()
    return route


def _own_thread(store: AppStore, user_id: str, thread_id: str, scope: str, comment_id: str) -> None:
    thread = store.get_comment_thread(scope, comment_id)
    _route(store, user_id, thread_id, scope, thread["target_kind"], thread["target_id"])


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
    user_id, scope = _owner(store, request, identity_access, thread_id)
    if chat_id is not None:
        if chat_id != thread_id:
            raise comment_target_not_found()
    elif target_id is not None:
        _route(store, user_id, thread_id, scope, target_kind, target_id)
    return comments.list_threads(
        request,
        project=scope,
        store=store,
        target_kind=target_kind,
        target_id=target_id,
        chat_id=chat_id,
    )


@router.get("/api/agent-threads/{thread_id}/comment-threads/{comment_id}")
def get_space_thread(
    thread_id: str,
    comment_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    user_id, scope = _owner(store, request, identity_access, thread_id)
    _own_thread(store, user_id, thread_id, scope, comment_id)
    return comments.get_thread(comment_id, request, project=scope, store=store)


@router.post("/api/agent-threads/{thread_id}/comment-threads")
def create_space_thread(
    thread_id: str,
    body: CreateCommentThread,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    user_id, scope = _owner(store, request, identity_access, thread_id)
    _route(store, user_id, thread_id, scope, body.target.kind, body.target.id)
    return comments.create_thread(body, request, project=scope, store=store)


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
    user_id, scope = _owner(store, request, identity_access, thread_id)
    _own_thread(store, user_id, thread_id, scope, comment_id)
    return comments.reply(comment_id, body, request, project=scope, store=store)


def _settle(
    store: AppStore,
    request: Request,
    identity: IdentityAccess,
    thread_id: str,
    comment_id: str,
    resolved: bool,
) -> dict[str, object]:
    user_id, scope = _owner(store, request, identity, thread_id)
    _own_thread(store, user_id, thread_id, scope, comment_id)
    change = comments.resolve if resolved else comments.reopen
    return change(comment_id, request, project=scope, store=store)


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
