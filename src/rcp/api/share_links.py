"""Team share links: copy one opaque URL for an item, and resolve it on open.

Every failure to resolve answers one fixed 404, so a token never reveals
whether its item exists, moved out of reach, or was never real.
"""

from __future__ import annotations

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
from rcp.history.branches import read_branch_metadata
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore
from rcp.storage.comments import CommentError, CommentTargetRoute
from rcp.storage.membership import live_project_member
from rcp.storage.share_links import ShareLinkKind, ShareLinkRecord
from rcp.team_access import team_access_url
from rcp.transport import StateMissing, StateUnavailable

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


def _target_ref(graph_target: str) -> GraphTargetRef:
    if graph_target == "main":
        return GraphTargetRef()
    return GraphTargetRef(kind="branch", branch_id=graph_target.removeprefix("branch:"))


def _graph_nodes(catalog: ProjectCatalog, project_id: str, graph_target: str) -> dict:
    try:
        service = catalog.open(project_id).for_graph_target(
            _target_ref(graph_target), initialize=False
        )
        return service.history.current_materialization().state.nodes
    except (KeyError, ValueError, OSError, StateMissing) as exc:
        raise _Unreadable from exc


def _require_branch(catalog: ProjectCatalog, project_id: str, graph_target: str) -> None:
    target = _target_ref(graph_target)
    try:
        with catalog.open(project_id).history.read_view() as reader:
            if target.branch_id is not None:
                read_branch_metadata(reader, target.branch_id)
    except (KeyError, ValueError, OSError, StateMissing) as exc:
        raise _Unreadable from exc


def _readable_route(
    store: AppStore, user_id: str, project_id: str, kind: str, item_id: str
) -> CommentTargetRoute:
    if kind == "comment":
        try:
            thread = store.get_comment_thread(project_id, item_id)
        except CommentError as exc:
            raise _Unreadable from exc
        kind, item_id = thread["target_kind"], thread["target_id"]
    route = store.comment_target_route(user_id, kind, item_id, project_id=project_id)
    if route is None:
        raise _Unreadable
    return route


def share_link_item(
    store: AppStore,
    catalog: ProjectCatalog,
    user_id: str,
    project_id: str,
    kind: str,
    item_id: str,
    graph_target: str,
) -> tuple[str, str | None]:
    """The one readability check for a share link: its graph target and title.

    Raises `_Unreadable` for a 404, and `StateUnavailable` only to a live member.
    """
    with store.connection() as connection:
        if not live_project_member(connection, project_id, user_id):
            raise _Unreadable
    if kind != "node":
        route = _readable_route(store, user_id, project_id, kind, item_id)
        _require_branch(catalog, project_id, route.graph_target)
        return route.graph_target, route.title
    node = _graph_nodes(catalog, project_id, graph_target).get(item_id)
    if node is None:
        raise _Unreadable
    return graph_target, node.title


def _state_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Project state is unavailable.")


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
        graph_target, _ = share_link_item(
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
    except StateUnavailable as exc:
        raise _state_unavailable() from exc
    access_url = team_access_url()
    if access_url is None:
        raise HTTPException(status_code=409, detail="This team space has no access address.")
    token = store.share_link_token(
        project, body.kind, body.item_id, graph_target, created_by=user_id
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
        graph_target, title = share_link_item(
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
    except StateUnavailable as exc:
        raise _state_unavailable() from exc
    return {
        "project_id": link.project_id,
        "project_name": project.name,
        "kind": link.kind,
        "title": title,
        "link": deep_link(
            link.project_id, graph_target.removeprefix("branch:"), link.kind, link.item_id
        ),
    }
