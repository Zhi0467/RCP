from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from rcp.api.identity import IdentityAccess
from rcp.attachments import ChatAttachmentStore
from rcp.keyed_locks import KeyedLocks
from rcp.projects import ProjectCatalog, ProjectDisplayCache
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.watchers import WatcherDelivery, WatcherPoller


@dataclass(frozen=True, slots=True)
class ApiServices:
    """Composition-only runtime services exposed to API dependencies."""

    store: AppStore
    catalog: ProjectCatalog
    identity_access: IdentityAccess
    attachment_store: ChatAttachmentStore
    watcher_poller: WatcherPoller
    result_view_keep_locks: KeyedLocks
    project_display_cache: ProjectDisplayCache
    watcher_delivery: WatcherDelivery
    experiment_operation_lock: KeyedLocks


def _api_services(request: Request) -> ApiServices:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, ApiServices):
        raise RuntimeError("API services have not been configured.")
    return services


def get_store(request: Request) -> AppStore:
    return _api_services(request).store


def get_catalog(request: Request) -> ProjectCatalog:
    return _api_services(request).catalog


def get_identity_access(request: Request) -> IdentityAccess:
    return _api_services(request).identity_access


def get_attachment_store(request: Request) -> ChatAttachmentStore:
    return _api_services(request).attachment_store


def get_watcher_poller(request: Request) -> WatcherPoller:
    return _api_services(request).watcher_poller


def get_result_view_keep_locks(request: Request) -> KeyedLocks:
    return _api_services(request).result_view_keep_locks


def get_project_display_cache(request: Request) -> ProjectDisplayCache:
    return _api_services(request).project_display_cache


def get_watcher_delivery(request: Request) -> WatcherDelivery:
    return _api_services(request).watcher_delivery


def get_experiment_operation_lock(request: Request) -> KeyedLocks:
    return _api_services(request).experiment_operation_lock


def get_project_service(catalog: ProjectCatalog, project_id: str) -> ProjectService:
    try:
        return catalog.open(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def require_registered_project(catalog: ProjectCatalog, project_id: str) -> None:
    try:
        catalog.card(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def require_project_membership(project_id: str, request: Request) -> str:
    canonical = get_catalog(request).resolve_project_id(project_id)
    member = get_identity_access(request).acting_user(request)
    if not get_store(request).is_project_member(canonical, member.user_id):
        # A refusal is indistinguishable from an unknown project. A 403 would
        # confirm the project exists, which is the one thing a non-member must
        # not learn.
        raise HTTPException(status_code=404, detail="Project not found")
    return canonical


__all__ = [
    "ApiServices",
    "get_attachment_store",
    "get_catalog",
    "get_identity_access",
    "get_experiment_operation_lock",
    "get_project_service",
    "get_project_display_cache",
    "get_result_view_keep_locks",
    "get_store",
    "get_watcher_delivery",
    "get_watcher_poller",
    "require_registered_project",
    "require_project_membership",
]
