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
from rcp.comments.models import CommentPostBody, CreateCommentThread
from rcp.history import ReplayHalted
from rcp.storage import AppStore
from rcp.storage.chat_names import chat_title
from rcp.storage.comments import CommentError, CommentTargetRoute

router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
ProjectDependency = Annotated[str, Depends(require_project_membership)]


def _route(
    store: AppStore, request: Request, project: str, kind: str, target: str
) -> CommentTargetRoute:
    user_id = get_identity_access(request).acting_user(request).user_id
    route = store.comment_target_route(user_id, kind, target, project_id=project)
    if route is None:
        raise HTTPException(status_code=404, detail="comment_target_not_found")
    return route


def _sender(store: AppStore, request: Request, project: str) -> str | None:
    try:
        user = get_identity_access(request).require_patch_capable_identity(request)
        store.require_project_accepts_new_work(project)
        get_project_writer(get_catalog(request), project).history.require_writable()
    except (HTTPException, ValueError, ReplayHalted):
        return None
    return user.user_id


def _response(
    thread: dict[str, Any], route: CommentTargetRoute, sender: str | None
) -> dict[str, Any]:
    can_send = (
        sender is not None
        and thread["state"] == "open"
        and (
            thread["target_kind"] == "paper"
            or (route.chat_id is not None and route.chat_owner in {None, sender})
        )
    )
    return {**thread, "chat_id": route.chat_id, "can_send": can_send}


def _respond(
    store: AppStore, request: Request, project: str, thread: dict[str, Any]
) -> dict[str, object]:
    route = _route(store, request, project, thread["target_kind"], thread["target_id"])
    return {"thread": _response(thread, route, _sender(store, request, project))}


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


@router.get("/api/projects/{project_id}/comment-threads")
def list_threads(
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
    target_kind: Literal["artifact", "paper", "reply"],
    target_id: str,
) -> dict[str, object]:
    route = _route(store, request, project, target_kind, target_id)
    sender = _sender(store, request, project)
    return {
        "threads": [
            _response(thread, route, sender)
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
    try:
        thread = store.get_comment_thread(project, thread_id)
    except CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    return _respond(store, request, project, thread)


@router.post("/api/projects/{project_id}/comment-threads")
def create_thread(
    body: CreateCommentThread,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    with _write(request, project) as user_id:
        thread = store.create_comment_thread(
            project,
            target_kind=body.target.kind,
            target_id=body.target.id,
            anchor=body.anchor.model_dump(mode="json"),
            drawn_version=body.drawn_version,
            text=body.text,
            user_id=user_id,
        )
    return _respond(store, request, project, thread)


@router.post("/api/projects/{project_id}/comment-threads/{thread_id}/posts")
def reply(
    thread_id: str,
    body: CommentPostBody,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    with _write(request, project) as user_id:
        thread = store.add_comment_post(project, thread_id, user_id=user_id, text=body.text)
    return _respond(store, request, project, thread)


@router.patch("/api/projects/{project_id}/comment-posts/{post_id}")
def edit_post(
    post_id: str,
    body: CommentPostBody,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    with _write(request, project) as user_id:
        thread = store.edit_comment_post(project, post_id, user_id=user_id, text=body.text)
    return _respond(store, request, project, thread)


@router.delete("/api/projects/{project_id}/comment-posts/{post_id}")
def delete_post(
    post_id: str,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
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
    with _write(request, project) as user_id:
        thread = store.resolve_comment_thread(project, thread_id, user_id=user_id)
    return _respond(store, request, project, thread)


@router.post("/api/projects/{project_id}/comment-threads/{thread_id}/reopen")
def reopen(
    thread_id: str,
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    with _write(request, project) as user_id:
        thread = store.reopen_comment_thread(project, thread_id, user_id=user_id)
    return _respond(store, request, project, thread)


@router.get("/api/projects/{project_id}/comment-mentions")
def mentions(
    request: Request,
    *,
    project: ProjectDependency,
    store: StoreDependency,
) -> dict[str, object]:
    user_id = get_identity_access(request).acting_user(request).user_id
    rows = store.open_comment_mentions(project, user_id)
    with store.connection() as connection:
        for row in rows:
            thread = row["thread"]
            if row["target_kind"] == "artifact":
                artifact = store.artifact(row["target_id"])
                row["target_name"] = artifact.source_name if artifact else None
            elif row["target_kind"] == "paper":
                file = connection.execute(
                    "SELECT path FROM paper_files WHERE project_id=? AND file_id=?",
                    (project, row["target_id"]),
                ).fetchone()
                row["target_name"] = file["path"] if file else None
            else:
                row["target_name"] = chat_title(connection, project, thread["chat_id"] or "")
            row["members"] = [
                {"user_id": member["id"], "display_name": member["display_name"]}
                for member in {
                    member["id"]: member for post in thread["posts"] for member in post["mentions"]
                }.values()
            ]
    return {"mentions": rows}
