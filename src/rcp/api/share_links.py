"""Team share links: copy one opaque URL for an item, and resolve it on open.

Every failure to resolve answers one fixed 404, so a token never reveals
whether its item exists, moved out of reach, or was never real.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_store,
    require_project_membership,
)
from rcp.core.transition_models import GraphTargetRef
from rcp.digest import deep_link
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore
from rcp.storage.chat_names import chat_title
from rcp.storage.comments import CommentError, readable_comment_target
from rcp.storage.membership import live_project_member
from rcp.storage.share_links import ShareLinkKind, ShareLinkRecord
from rcp.team_access import team_access_url
from rcp.transport import StateUnavailable

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]


class CreateShareLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ShareLinkKind
    item_id: str = Field(min_length=1)
    graph_target: str = Field(pattern=r"^(main|branch:.+)$")


class ResolveShareLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


class _Unreadable(Exception):
    pass


def _not_available() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


def _require_team(request: Request) -> None:
    if request.app.state.space_kind != "team":
        raise _not_available()


def _graph_nodes(catalog: ProjectCatalog, project_id: str, graph_target: str) -> dict:
    target = (
        GraphTargetRef()
        if graph_target == "main"
        else GraphTargetRef(kind="branch", branch_id=graph_target.removeprefix("branch:"))
    )
    try:
        service = catalog.open(project_id).for_graph_target(target, initialize=False)
        return service.history.current_materialization().state.nodes
    except (KeyError, ValueError, OSError, StateUnavailable) as exc:
        raise _Unreadable from exc


def _item_title(
    store: AppStore,
    connection: sqlite3.Connection,
    user_id: str,
    project_id: str,
    kind: str,
    item_id: str,
) -> str | None:
    if kind == "comment":
        try:
            thread = store.get_comment_thread(project_id, item_id)
        except CommentError as exc:
            raise _Unreadable from exc
        return _item_title(
            store, connection, user_id, project_id, thread["target_kind"], thread["target_id"]
        )
    if kind == "chat":
        inventory = store.chat_inventory(project_id, item_id)
        if not inventory or store.private_chat_owner(item_id) not in {None, user_id}:
            raise _Unreadable
        return inventory[0]["title"]
    route = readable_comment_target(store, connection, user_id, project_id, kind, item_id)
    if route is None:
        raise _Unreadable
    if kind == "artifact":
        artifact = store.artifact(item_id)
        return (artifact.display_title or artifact.source_name) if artifact else None
    if kind == "paper":
        row = connection.execute(
            "SELECT path FROM paper_files WHERE project_id=? AND file_id=?", (project_id, item_id)
        ).fetchone()
        return str(row[0]) if row else None
    return chat_title(connection, project_id, route.chat_id) if route.chat_id else None


def share_link_title(
    store: AppStore,
    catalog: ProjectCatalog,
    user_id: str,
    project_id: str,
    kind: str,
    item_id: str,
    graph_target: str,
) -> str | None:
    """The one readability check for a share link: the item's title, or `_Unreadable`."""
    with store.connection() as connection:
        if not live_project_member(connection, project_id, user_id):
            raise _Unreadable
        if kind != "node":
            if graph_target != "main":
                _graph_nodes(catalog, project_id, graph_target)
            return _item_title(store, connection, user_id, project_id, kind, item_id)
    node = _graph_nodes(catalog, project_id, graph_target).get(item_id)
    if node is None:
        raise _Unreadable
    return node.title


@router.post("/api/projects/{project_id}/share-links")
def create_share_link(
    body: CreateShareLink,
    request: Request,
    *,
    project: Annotated[str, Depends(require_project_membership)],
    store: StoreDependency,
) -> dict[str, str]:
    _require_team(request)
    user_id = get_identity_access(request).acting_user(request).user_id
    try:
        share_link_title(
            store,
            get_catalog(request),
            user_id,
            project,
            body.kind,
            body.item_id,
            body.graph_target,
        )
    except _Unreadable as exc:
        raise _not_available() from exc
    access_url = team_access_url()
    if access_url is None:
        raise HTTPException(status_code=409, detail="This team space has no access address.")
    token = store.share_link_token(
        project, body.kind, body.item_id, body.graph_target, created_by=user_id
    )
    return {"url": f"{access_url}/open#l={token}"}


@router.post("/api/share-links/resolve")
def resolve_share_link(
    body: ResolveShareLink, request: Request, *, store: StoreDependency
) -> dict[str, str | None]:
    _require_team(request)
    link: ShareLinkRecord | None = store.share_link(body.token)
    user_id = get_identity_access(request).acting_user(request).user_id
    try:
        if link is None:
            raise _Unreadable
        title = share_link_title(
            store,
            get_catalog(request),
            user_id,
            link.project_id,
            link.kind,
            link.item_id,
            link.graph_target,
        )
        project = store.project(link.project_id)
        if project is None:
            raise _Unreadable
    except _Unreadable as exc:
        raise _not_available() from exc
    return {
        "project_id": link.project_id,
        "project_name": project.name,
        "kind": link.kind,
        "title": title,
        "link": deep_link(
            link.project_id, link.graph_target.removeprefix("branch:"), link.kind, link.item_id
        ),
    }
