from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_project_writer,
    get_store,
    project_write_admission,
    require_project_membership,
)
from rcp.comments.models import COMMENT_MEDIA_TYPES, CommentPostBody, CreateCommentThread
from rcp.history import ReplayHalted
from rcp.storage import AppStore
from rcp.storage.comments import CommentError

router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
ProjectDependency = Annotated[str, Depends(require_project_membership)]


def _readable(store: AppStore, request: Request, project: str, kind: str, target: str) -> str:
    user_id = get_identity_access(request).acting_user(request).user_id
    if not store.comment_target_readable(user_id, kind, target, project_id=project):
        raise HTTPException(status_code=404, detail="Comment target not found")
    return user_id


def _thread(store: AppStore, request: Request, project: str, thread_id: str) -> dict[str, Any]:
    try:
        thread = store.get_comment_thread(project, thread_id)
    except CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    if thread is None:
        raise HTTPException(status_code=404, detail="Comment thread not found")
    _readable(store, request, project, thread["target_kind"], thread["target_id"])
    return thread


def _post_thread(store: AppStore, request: Request, project: str, post_id: str) -> dict[str, Any]:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT thread_id FROM comment_posts WHERE project_id=? AND post_id=?",
            (project, post_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Comment post not found")
    return _thread(store, request, project, row["thread_id"])


@contextmanager
def _write(request: Request, project: str) -> Iterator[str]:
    with project_write_admission(project, request):
        try:
            get_project_writer(get_catalog(request), project).history.require_writable()
            yield get_identity_access(request).acting_user(request).user_id
        except CommentError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _can_send(store: AppStore, request: Request, project: str, thread: dict[str, Any]) -> bool:
    try:
        user = get_identity_access(request).require_patch_capable_identity(request)
        store.require_project_accepts_new_work(project)
        get_project_writer(get_catalog(request), project).history.require_writable()
    except (HTTPException, ValueError, ReplayHalted):
        return False
    if thread["target_kind"] == "paper":
        return True
    if thread["target_kind"] == "artifact":
        artifact = store.artifact(thread["target_id"])
        task = store.agent_task(artifact.origin_operation_id or "") if artifact else None
        chat_id = artifact.chat_id if artifact else None
    else:
        task = store.agent_task(thread["target_id"])
        chat_id = None
    if task is not None:
        chat_id = task.request.get("chat_id") or chat_id
    return (
        isinstance(chat_id, str)
        and bool(chat_id)
        and store.private_chat_owner(chat_id) in {None, user.user_id}
    )


def _response(
    store: AppStore, request: Request, project: str, thread: dict[str, Any]
) -> dict[str, Any]:
    return {**thread, "can_send": _can_send(store, request, project, thread)}


@router.get("/api/projects/{project_id}/comment-threads")
def list_threads(
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
    target_kind: Literal["artifact", "paper", "reply"],
    target_id: str,
) -> dict[str, object]:
    _readable(store, request, project, target_kind, target_id)
    return {
        "threads": [
            _response(store, request, project, thread)
            for thread in store.list_comment_threads(project, target_kind, target_id)
        ]
    }


@router.get("/api/projects/{project_id}/comment-threads/{thread_id}")
def get_thread(
    thread_id: str,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    return {
        "thread": _response(store, request, project, _thread(store, request, project, thread_id))
    }


@router.post("/api/projects/{project_id}/comment-threads")
def create_thread(
    body: CreateCommentThread,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    _readable(store, request, project, body.target.kind, body.target.id)
    graph_target = "main"
    operation_id = body.target.id
    if body.target.kind == "artifact":
        artifact = store.artifact(body.target.id)
        if artifact is None or artifact.media_type not in COMMENT_MEDIA_TYPES[body.anchor.kind]:
            raise HTTPException(
                status_code=422, detail="Anchor is not supported by this media type"
            )
        operation_id = artifact.origin_operation_id or "" if artifact else ""
    if body.target.kind != "paper" and (task := store.agent_task(operation_id)) is not None:
        graph_target = task.graph_target.key
    with _write(request, project) as user_id:
        thread = store.create_comment_thread(
            project,
            target_kind=body.target.kind,
            target_id=body.target.id,
            graph_target=graph_target,
            anchor=body.anchor.model_dump(mode="json"),
            drawn_version=body.drawn_version,
            text=body.text,
            user_id=user_id,
        )
    return {"thread": _response(store, request, project, thread)}


@router.post("/api/projects/{project_id}/comment-threads/{thread_id}/posts")
def reply(
    thread_id: str,
    body: CommentPostBody,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    _thread(store, request, project, thread_id)
    with _write(request, project) as user_id:
        store.add_comment_post(project, thread_id, user_id=user_id, text=body.text)
    return {
        "thread": _response(store, request, project, _thread(store, request, project, thread_id))
    }


@router.patch("/api/projects/{project_id}/comment-posts/{post_id}")
def edit_post(
    post_id: str,
    body: CommentPostBody,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    thread = _post_thread(store, request, project, post_id)
    with _write(request, project) as user_id:
        store.edit_comment_post(project, post_id, user_id=user_id, text=body.text)
    return {
        "thread": _response(
            store, request, project, _thread(store, request, project, thread["thread_id"])
        )
    }


@router.delete("/api/projects/{project_id}/comment-posts/{post_id}")
def delete_post(
    post_id: str,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    _post_thread(store, request, project, post_id)
    with _write(request, project) as user_id:
        store.delete_comment_post(project, post_id, user_id=user_id)
    return {"ok": True}


@router.post("/api/projects/{project_id}/comment-threads/{thread_id}/resolve")
def resolve(
    thread_id: str,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    _thread(store, request, project, thread_id)
    with _write(request, project) as user_id:
        store.resolve_comment_thread(project, thread_id, user_id=user_id)
    return {
        "thread": _response(store, request, project, _thread(store, request, project, thread_id))
    }


@router.post("/api/projects/{project_id}/comment-threads/{thread_id}/reopen")
def reopen(
    thread_id: str,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    _thread(store, request, project, thread_id)
    with _write(request, project) as user_id:
        store.reopen_comment_thread(project, thread_id, user_id=user_id)
    return {
        "thread": _response(store, request, project, _thread(store, request, project, thread_id))
    }


@router.get("/api/projects/{project_id}/comment-mentions")
def mentions(
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    user_id = get_identity_access(request).acting_user(request).user_id
    return {"mentions": store.open_comment_mentions(project, user_id)}
