from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from .helpers import create_named_app

RouteEntry = tuple[tuple[str, ...], str]


# This is the structural safety net for route extraction. Keep this literal
# unchanged when a handler moves: it records the route surface, not ownership.
_FROZEN_ROUTE_INVENTORY: tuple[RouteEntry, ...] = (
    (("GET", "HEAD"), "/openapi.json"),
    (("GET", "HEAD"), "/docs"),
    (("GET", "HEAD"), "/docs/oauth2-redirect"),
    (("GET", "HEAD"), "/redoc"),
    (("GET",), "/api/health"),
    (("GET",), "/api/identity"),
    (("PATCH",), "/api/identity"),
    (("POST",), "/api/team/enroll"),
    (("POST",), "/api/team/session/exchange"),
    (("POST",), "/api/team/session/logout"),
    (("GET",), "/api/team/invitations"),
    (("POST",), "/api/team/invitations"),
    (("POST",), "/api/team/credential/rotate"),
    (("POST",), "/api/team/credential/revoke"),
    (("PATCH",), "/api/team/space"),
    (("GET",), "/api/projects"),
    (("GET",), "/api/episodes"),
    (("GET",), "/api/space/users"),
    (("GET",), "/api/project-invitations"),
    (("POST",), "/api/project-invitations/{invitation_id}/{response}"),
    (("GET",), "/api/providers"),
    (("POST",), "/api/projects"),
    (("POST",), "/api/project-setup/preflight"),
    (("POST",), "/api/project-setup/create"),
    (("DELETE",), "/api/caches"),
    (("GET",), "/api/skills/{kind}/{package_id}"),
    (("DELETE",), "/api/projects/{project_id}"),
    (("GET",), "/api/projects/{project_id}"),
    (("GET",), "/api/projects/{project_id}/members"),
    (("POST",), "/api/projects/{project_id}/invitations"),
    (("POST",), "/api/projects/{project_id}/leave"),
    (("GET",), "/api/projects/{project_id}/cached"),
    (("GET",), "/api/projects/{project_id}/cached/revision"),
    (("GET",), "/api/projects/{project_id}/readiness"),
    (("GET",), "/api/projects/{project_id}/graph"),
    (("GET",), "/api/projects/{project_id}/revision"),
    (("HEAD",), "/api/projects/{project_id}/repositories/files/preview"),
    (("GET",), "/api/projects/{project_id}/repositories/files/preview"),
    (("PUT",), "/api/projects/{project_id}/settings"),
    (("POST",), "/api/projects/{project_id}/machines/{machine_alias}/providers/{provider}/resolve"),
    (("GET",), "/api/projects/{project_id}/history"),
    (("GET",), "/api/projects/{project_id}/history/summaries"),
    (("GET",), "/api/projects/{project_id}/sources"),
    (("POST",), "/api/projects/{project_id}/sync"),
    (("GET",), "/api/projects/{project_id}/transition-manifest"),
    (("POST",), "/api/projects/{project_id}/sync/preview"),
    (("DELETE",), "/api/projects/{project_id}/caches"),
    (("POST",), "/api/projects/{project_id}/chats/{chat_id}/attachments"),
    (("DELETE",), "/api/projects/{project_id}/chats/{chat_id}/attachments/{attachment_id}"),
    (("POST",), "/api/projects/{project_id}/tasks/{kind}"),
    (("POST",), "/api/projects/{project_id}/experiments/{node_id:path}/run"),
    (("GET",), "/api/projects/{project_id}/tasks"),
    (("GET",), "/api/projects/{project_id}/usage"),
    (("GET",), "/api/projects/{project_id}/watchers"),
    (("POST",), "/api/projects/{project_id}/watchers/{watcher_id}/check"),
    (("POST",), "/api/projects/{project_id}/watchers/{watcher_id}/stop"),
    (("POST",), "/api/projects/{project_id}/experiments/{node_id:path}/watchers/stop"),
    (("POST",), "/api/projects/{project_id}/experiments/{node_id:path}/stop"),
    (("GET",), "/api/projects/{project_id}/chats"),
    (("GET",), "/api/projects/{project_id}/chats/{chat_id}"),
    (("GET",), "/api/projects/{project_id}/episodes"),
    (("POST",), "/api/projects/{project_id}/episodes"),
    (("POST",), "/api/projects/{project_id}/episodes/{episode_id}/stop"),
    (("POST",), "/api/projects/{project_id}/episodes/{episode_id}/merge"),
    (("POST",), "/api/projects/{project_id}/episodes/{episode_id}/reauthorize"),
    (("GET",), "/api/projects/{project_id}/episodes/{episode_id}/messages"),
    (("POST",), "/api/projects/{project_id}/episodes/{episode_id}/messages"),
    (("HEAD",), "/api/projects/{project_id}/episodes/{episode_id}/report/preview"),
    (("GET",), "/api/projects/{project_id}/episodes/{episode_id}/report/preview"),
    (("GET",), "/api/projects/{project_id}/result-views"),
    (("HEAD",), "/api/projects/{project_id}/result-views/{view_id}/preview"),
    (("GET",), "/api/projects/{project_id}/result-views/{view_id}/preview"),
    (("POST",), "/api/projects/{project_id}/result-views/{view_id}/keep"),
    (("GET",), "/api/projects/{project_id}/tasks/{operation_id}"),
    (("HEAD",), "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview"),
    (("GET",), "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview"),
    (("HEAD",), "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download"),
    (("GET",), "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download"),
    (("POST",), "/api/projects/{project_id}/tasks/{operation_id}/pause"),
    (("POST",), "/api/projects/{project_id}/tasks/{operation_id}/resume"),
    (("POST",), "/api/projects/{project_id}/tasks/{operation_id}/repair-graph-update"),
    (("POST",), "/api/projects/{project_id}/tasks/{operation_id}/retry"),
    (("GET",), "/api/projects/{project_id}/paper"),
    (("POST",), "/api/projects/{project_id}/paper/create"),
    (("PUT",), "/api/projects/{project_id}/paper"),
    (("GET",), "/api/projects/{project_id}/paper/sessions"),
)


# This map is intentionally independent from _FROZEN_ROUTE_INVENTORY. Update
# only this map as Phase 5 extracts handlers into their owning API modules.
_HANDLER_MODULE_MAP: dict[str, str] = {
    "agent_task": "src/rcp/api/app.py",
    "agent_tasks": "src/rcp/api/app.py",
    "agent_usage": "src/rcp/api/app.py",
    "answer_project_invitation": "src/rcp/api/app.py",
    "cached_project": "src/rcp/api/app.py",
    "cached_project_revision": "src/rcp/api/app.py",
    "chat": "src/rcp/api/app.py",
    "chats": "src/rcp/api/app.py",
    "check_watcher_now": "src/rcp/api/app.py",
    "clear_all_rebuildable_caches": "src/rcp/api/app.py",
    "clear_rebuildable_caches": "src/rcp/api/app.py",
    "create_paper": "src/rcp/api/paper.py",
    "create_project": "src/rcp/api/app.py",
    "create_team_invitation": "src/rcp/api/app.py",
    "delete_project": "src/rcp/api/app.py",
    "download_agent_artifact": "src/rcp/api/app.py",
    "enroll_team_member": "src/rcp/api/app.py",
    "episode_messages": "src/rcp/api/app.py",
    "episodes": "src/rcp/api/app.py",
    "exchange_team_session": "src/rcp/api/app.py",
    "experiment_episodes": "src/rcp/api/app.py",
    "get_identity": "src/rcp/api/app.py",
    "get_paper": "src/rcp/api/paper.py",
    "graph": "src/rcp/api/app.py",
    "graph_transition_manifest": "src/rcp/api/app.py",
    "health": "src/rcp/api/app.py",
    "history": "src/rcp/api/app.py",
    "history_summaries": "src/rcp/api/app.py",
    "invite_project_member": "src/rcp/api/app.py",
    "keep_result_view": "src/rcp/api/app.py",
    "leave_project": "src/rcp/api/app.py",
    "logout_team_session": "src/rcp/api/app.py",
    "merge_episode_branch": "src/rcp/api/app.py",
    "paper_sessions": "src/rcp/api/paper.py",
    "pause_agent_task": "src/rcp/api/app.py",
    "preflight_project": "src/rcp/api/app.py",
    "preview_agent_artifact": "src/rcp/api/app.py",
    "preview_episode_report": "src/rcp/api/app.py",
    "preview_graph_sync": "src/rcp/api/app.py",
    "preview_repository_file": "src/rcp/api/app.py",
    "preview_result_view": "src/rcp/api/app.py",
    "project": "src/rcp/api/app.py",
    "project_invitations_for_me": "src/rcp/api/app.py",
    "project_members": "src/rcp/api/app.py",
    "project_readiness": "src/rcp/api/app.py",
    "project_revision": "src/rcp/api/app.py",
    "project_watchers": "src/rcp/api/app.py",
    "projects": "src/rcp/api/app.py",
    "providers": "src/rcp/api/app.py",
    "read_skill_package": "src/rcp/api/app.py",
    "reauthorize_episode": "src/rcp/api/app.py",
    "register_project": "src/rcp/api/app.py",
    "remove_chat_attachment": "src/rcp/api/app.py",
    "repair_agent_task_graph_update": "src/rcp/api/app.py",
    "resolve_project_provider_path": "src/rcp/api/app.py",
    "result_views": "src/rcp/api/app.py",
    "resume_agent_task": "src/rcp/api/app.py",
    "retry_agent_task": "src/rcp/api/app.py",
    "revoke_team_credential": "src/rcp/api/app.py",
    "rotate_team_credential": "src/rcp/api/app.py",
    "run_experiment": "src/rcp/api/app.py",
    "save_paper": "src/rcp/api/paper.py",
    "send_episode_message": "src/rcp/api/app.py",
    "sources": "src/rcp/api/app.py",
    "space_users": "src/rcp/api/app.py",
    "start_agent_task": "src/rcp/api/app.py",
    "start_episode": "src/rcp/api/app.py",
    "stop_episode": "src/rcp/api/app.py",
    "stop_experiment_loop": "src/rcp/api/app.py",
    "stop_experiment_watchers": "src/rcp/api/app.py",
    "stop_watcher": "src/rcp/api/app.py",
    "sync_graph": "src/rcp/api/app.py",
    "team_invitations": "src/rcp/api/app.py",
    "update_identity": "src/rcp/api/app.py",
    "update_project_settings": "src/rcp/api/app.py",
    "update_team_space": "src/rcp/api/app.py",
    "upload_chat_attachment": "src/rcp/api/app.py",
}


def _walk_routes(routes: Iterable[object]) -> Iterable[object]:
    """Yield actual routes, including routes nested by ``include_router``."""

    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk_routes(inner.routes)
        elif hasattr(route, "methods"):
            yield route


def _route_entry(route: Any) -> RouteEntry:
    methods = tuple(sorted(route.methods))
    return methods, route.path


@pytest.fixture
def route_app(manifest, tmp_path: Path) -> FastAPI:
    return create_named_app(str(manifest.path), data_dir=tmp_path / "route-inventory")


def test_frozen_route_inventory(route_app: FastAPI) -> None:
    routes = list(_walk_routes(route_app.routes))
    entries = tuple(_route_entry(route) for route in routes)

    assert len(entries) == 86
    assert len(_FROZEN_ROUTE_INVENTORY) == 86
    # Registration order is not part of the route contract; membership is.
    assert frozenset(entries) == frozenset(_FROZEN_ROUTE_INVENTORY)

    # The count makes the application/generated split explicit. FastAPI's
    # built-in routes are ordinary Starlette Route objects, while application
    # routes are APIRoute objects (including those nested in the router).
    assert sum(isinstance(route, APIRoute) for route in routes) == 82
    assert len(routes) - sum(isinstance(route, APIRoute) for route in routes) == 4


def test_handler_module_map_is_separate_and_current(route_app: FastAPI) -> None:
    routes = list(_walk_routes(route_app.routes))
    application_routes = [route for route in routes if isinstance(route, APIRoute)]
    observed: dict[str, str] = {}
    repository_root = Path(__file__).resolve().parents[1]
    for route in application_routes:
        endpoint = route.endpoint
        source = inspect.getsourcefile(endpoint)
        assert source is not None
        observed[endpoint.__name__] = str(Path(source).resolve().relative_to(repository_root))

    assert len(observed) == 77
    assert observed == _HANDLER_MODULE_MAP
