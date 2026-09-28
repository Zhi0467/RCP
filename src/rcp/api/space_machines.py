"""Space machine cards: list, add, rename, grant writable paths, and browse folders."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from rcp.agents.grant_paths import check_writable_path_text, refuse_grants_inside
from rcp.agents.write_scope import rcp_owned_paths
from rcp.api.dependencies import get_catalog, get_identity_access, get_store
from rcp.api.identity import IdentityAccess
from rcp.config import MachineConfig, load_manifest
from rcp.projects import ProjectCatalog
from rcp.setup import MachineBrowseFailure, browse_machine_directory, run_machine_directory_request
from rcp.storage import AppStore
from rcp.storage.models import SpaceMachineRecord

logger = logging.getLogger(__name__)

router = APIRouter()
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]

#: Bounds one PATCH; a card holds a handful of grants, not a mount table.
_MAX_WRITABLE_PATHS = 64


def _one_line(value: str, *, label: str, maximum: int) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{label} must be 1 to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must be one line")
    return value


class CreateSpaceMachineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    host: str = Field(default="", max_length=255)
    os_account: str = Field(default="", max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _one_line(value, label="machine name", maximum=80)


class UpdateSpaceMachineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    writable_paths: list[str] | None = Field(default=None, max_length=_MAX_WRITABLE_PATHS)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return None if value is None else _one_line(value, label="machine name", maximum=80)


class MachineDirectoriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, max_length=1024)
    filter: str = Field(default="", max_length=255)
    offset: int = Field(default=0, ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("a folder path must be one line")
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("a folder path must be absolute")
        return str(path)


def _machine_usage(
    store: AppStore,
) -> tuple[dict[tuple[str, str], list[dict[str, str]]], bool]:
    """Which registered projects name each machine account, and whether every manifest read."""

    usage: dict[tuple[str, str], list[dict[str, str]]] = {}
    complete = True
    for project in store.projects():
        try:
            manifest = load_manifest(project.locator)
        except (OSError, ValueError):
            logger.warning("Machine usage skipped an unreadable project manifest.")
            complete = False
            continue
        for machine in manifest.machines:
            usage.setdefault((machine.host, machine.os_account), []).append(
                {
                    "project_id": project.project_id,
                    "project_name": project.name,
                    "alias": machine.alias,
                }
            )
    return usage, complete


def _machine_view(
    machine: SpaceMachineRecord,
    usage: dict[tuple[str, str], list[dict[str, str]]],
    complete: bool,
    visible: set[str],
) -> dict[str, object]:
    projects = usage.get((machine.host, machine.os_account), [])
    return {
        "machine_id": machine.machine_id,
        "name": machine.name,
        "host": machine.host,
        "os_account": machine.os_account,
        "writable_paths": list(machine.writable_paths),
        # Use counts every project; only the viewer's own projects are named.
        "projects": [project for project in projects if project["project_id"] in visible],
        "in_use": True if projects else (False if complete else None),
    }


def _one_machine_view(
    store: AppStore, machine: SpaceMachineRecord, visible: set[str]
) -> dict[str, object]:
    return _machine_view(machine, *_machine_usage(store), visible)


def _visible_project_ids(
    request: Request, identity_access: IdentityAccess, store: AppStore
) -> set[str]:
    return store.member_project_ids(identity_access.acting_user(request).user_id)


def _machine_or_404(store: AppStore, machine_id: str) -> SpaceMachineRecord:
    try:
        return store.space_machine(machine_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Machine not found") from exc


def _owned_paths(machine: SpaceMachineRecord, home: str, data_dir: Path) -> list[str]:
    return rcp_owned_paths(
        account_home=home,
        app_data_dir=None if machine.host else data_dir,
        remote=bool(machine.host),
    )


def _is_protected(path: str, owned: list[str]) -> bool:
    try:
        refuse_grants_inside([path], owned)
    except ValueError:
        return True
    return False


def _validated_writable_paths(
    machine: SpaceMachineRecord, requested: list[str], catalog: ProjectCatalog
) -> list[str]:
    paths = sorted({check_writable_path_text(path) for path in requested})
    if not paths:
        return []
    # Every registered repository's canonical state on this host, admitted or
    # not; resolved on the machine because `.research` may be a symlink.
    research = sorted(
        {
            str(PurePosixPath(item.path) / ".research")
            for item in catalog.repository_ownership_inventory()
            if item.execution_host == machine.host
        }
    )
    result = run_machine_directory_request(
        machine.host,
        {"mode": "check", "paths": sorted({*paths, *research})},
        os_account=machine.os_account,
    )
    home = result.get("home")
    resolved = result.get("resolved")
    if (
        not isinstance(home, str)
        or not PurePosixPath(home).is_absolute()
        or not isinstance(resolved, dict)
        or set(resolved) != {*paths, *research}
    ):
        raise ValueError("the machine returned an invalid folder check")
    owned = _owned_paths(machine, home, catalog.data_dir)
    owned.extend(research)
    owned.extend(real for path in research if isinstance(real := resolved[path], str))
    for path in paths:
        real = resolved[path]
        if not isinstance(real, str):
            raise ValueError(f"{path} is not a folder on {machine.name}")
        # A symlink must not route a grant into RCP's own storage.
        refuse_grants_inside([path, real], owned)
    return paths


@router.get("/api/space/machines")
def list_space_machines(
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> dict[str, object]:
    visible = _visible_project_ids(request, identity_access, store)
    usage, complete = _machine_usage(store)
    return {
        "machines": [
            _machine_view(machine, usage, complete, visible) for machine in store.space_machines()
        ]
    }


@router.post("/api/space/machines")
def create_space_machine(
    body: CreateSpaceMachineRequest,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> dict[str, object]:
    identity_access.acting_user(request)
    if store.space_kind == "team" and not body.os_account:
        # Team projects are backed up, and a backup records each machine's account.
        raise HTTPException(status_code=422, detail="A team machine needs its account.")
    try:
        machine = MachineConfig(alias="card", host=body.host, os_account=body.os_account)
    except ValidationError as exc:
        detail = exc.errors()[0]["msg"].removeprefix("Value error, ")
        raise HTTPException(status_code=422, detail=detail) from None
    try:
        created = store.create_space_machine(
            name=body.name, host=machine.host, os_account=machine.os_account
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _one_machine_view(store, created, _visible_project_ids(request, identity_access, store))


@router.patch("/api/space/machines/{machine_id}")
def update_space_machine(
    machine_id: str,
    body: UpdateSpaceMachineRequest,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    writable_paths = None
    if body.writable_paths is not None:
        try:
            writable_paths = _validated_writable_paths(machine, body.writable_paths, catalog)
        except (MachineBrowseFailure, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    updated = store.update_space_machine(machine_id, name=body.name, writable_paths=writable_paths)
    return _one_machine_view(store, updated, _visible_project_ids(request, identity_access, store))


@router.delete("/api/space/machines/{machine_id}")
def delete_space_machine(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> dict[str, object]:
    view = _one_machine_view(
        store,
        _machine_or_404(store, machine_id),
        _visible_project_ids(request, identity_access, store),
    )
    if view["in_use"] is not False:
        detail = (
            "A project uses this machine; remove it from the project first."
            if view["in_use"]
            else "RCP could not read every project, so it cannot tell whether this machine is used."
        )
        raise HTTPException(status_code=409, detail=detail)
    store.delete_space_machine(machine_id)
    return {"deleted": machine_id}


@router.post("/api/space/machines/{machine_id}/directories")
def list_machine_directories(
    machine_id: str,
    body: MachineDirectoriesRequest,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    """One page of the folders directly inside one folder on this machine."""

    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    try:
        page = browse_machine_directory(
            machine.host,
            body.path,
            os_account=machine.os_account,
            name_filter=body.filter,
            offset=body.offset,
        )
    except (MachineBrowseFailure, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    owned = _owned_paths(machine, page.home, catalog.data_dir)
    return {
        "path": page.path,
        "parent": page.parent,
        "entries": [
            {
                "name": entry.name,
                "path": entry.path,
                "protected": _is_protected(entry.path, owned),
            }
            for entry in page.entries
        ],
        "total": page.total,
        "next_offset": page.next_offset,
    }
