"""Space machine cards: list, add, rename, grant writable paths, and browse folders."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from rcp.agents.grant_paths import check_writable_path_text, refuse_grants_inside
from rcp.agents.hidden_read import cached_hidden_read_readiness, hidden_read_defaults
from rcp.agents.write_scope import rcp_owned_paths
from rcp.api.dependencies import get_catalog, get_identity_access, get_store
from rcp.api.identity import IdentityAccess
from rcp.browser import install_browser, readiness
from rcp.config import MachineConfig, load_manifest
from rcp.core.models import MachineHiddenReadProjection
from rcp.limits import HIDDEN_READ_PATH_MAX_COUNT
from rcp.projects import ProjectCatalog
from rcp.rcp_home import command_socket_directory
from rcp.setup import MachineBrowseFailure, browse_machine_directory, run_machine_directory_request
from rcp.storage import AppStore
from rcp.storage.models import SpaceMachineRecord
from rcp.transport.ssh import control_directory_candidate

logger = logging.getLogger(__name__)

router = APIRouter()
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]

#: Held across "is this card used?" and delete, and across adding a card to a
#: project, so a delete cannot slip between an add's read and its manifest write.
MACHINE_ATTACHMENT_LOCK = threading.Lock()

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
    hidden_folders: list[str] | None = Field(default=None, max_length=HIDDEN_READ_PATH_MAX_COUNT)

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
) -> tuple[dict[str, list[dict[str, str]]], bool]:
    """Which registered projects name each machine account, and whether every manifest read."""

    usage: dict[str, list[dict[str, str]]] = {}
    complete = True
    for project in store.projects():
        try:
            manifest = load_manifest(project.locator)
        except (OSError, ValueError):
            logger.warning("Machine usage skipped an unreadable project manifest.")
            complete = False
            continue
        for machine in manifest.machines:
            usage.setdefault(machine.host, []).append(
                {
                    "project_id": project.project_id,
                    "project_name": project.name,
                    "alias": machine.alias,
                }
            )
    return usage, complete


def _hidden_read_projection(
    machine: SpaceMachineRecord, data_dir: Path
) -> MachineHiddenReadProjection:
    from rcp.server_ops.doctor import local_hidden_read_status

    home = "~" if machine.host else str(Path.home())
    # Remote defaults are display templates; only launch-host resolution knows overrides.
    environment = {} if machine.host else os.environ
    groups = hidden_read_defaults(
        home=home,
        app_data_dir=home + "/.local/share/rcp" if machine.host else str(data_dir),
        credential_roots=(),
        provider_login_files=(
            environment.get("CODEX_HOME", home + "/.codex") + "/auth.json",
            environment.get("CLAUDE_CONFIG_DIR", home + "/.claude") + "/.credentials.json",
            environment.get("XDG_DATA_HOME", home + "/.local/share") + "/opencode/auth.json",
        ),
        control_socket_dir=None if machine.host else str(control_directory_candidate()),
    )
    paths = {
        "~/" + path[len(home) + 1 :] if path.startswith(home + "/") else path
        for group in groups
        for path in group
    }
    return MachineHiddenReadProjection(
        default_paths=tuple(sorted(paths)),
        user_folders=tuple(machine.hidden_folders),
        readiness=(
            None
            if machine.host
            else local_hidden_read_status(cached_hidden_read_readiness(), app_data_dir=data_dir)
        ),
    )


def _machine_view(
    machine: SpaceMachineRecord,
    usage: dict[str, list[dict[str, str]]],
    complete: bool,
    visible: set[str],
    data_dir: Path,
) -> dict[str, object]:
    projects = usage.get(machine.host, [])
    return {
        "machine_id": machine.machine_id,
        "name": machine.name,
        "host": machine.host,
        "os_account": machine.os_account,
        "writable_paths": list(machine.writable_paths),
        "hidden_folders": list(machine.hidden_folders),
        "hidden_read": _hidden_read_projection(machine, data_dir).model_dump(mode="json"),
        # Use counts every project; only the viewer's own projects are named.
        "projects": [project for project in projects if project["project_id"] in visible],
        "in_use": True if projects else (False if complete else None),
    }


def _one_machine_view(
    store: AppStore, machine: SpaceMachineRecord, visible: set[str]
) -> dict[str, object]:
    return _machine_view(machine, *_machine_usage(store), visible, store.path.parent)


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


def _repository_state_paths(catalog: ProjectCatalog, machine: SpaceMachineRecord) -> list[str]:
    """Every registered repository's canonical state on this host, admitted or not."""

    return sorted(
        {
            str(PurePosixPath(item.path) / ".research")
            for item in catalog.repository_ownership_inventory()
            if item.execution_host == machine.host
        }
    )


def _validated_writable_paths(
    machine: SpaceMachineRecord, requested: list[str], catalog: ProjectCatalog
) -> list[str]:
    paths = sorted({check_writable_path_text(path) for path in requested})
    if not paths:
        return []
    # Resolved on the machine too, because `.research` may be a symlink.
    research = _repository_state_paths(catalog, machine)
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
    # Launches keep retained /tmp stages read-only, so a grant must not cover one.
    legacy = result.get("legacy_stages") or []
    if not isinstance(legacy, list) or not all(isinstance(item, str) for item in legacy):
        raise ValueError("the machine returned an invalid folder check")
    owned.extend(legacy)
    owned.extend(research)
    owned.extend(real for path in research if isinstance(real := resolved[path], str))
    for path in paths:
        real = resolved[path]
        if not isinstance(real, str):
            raise ValueError(f"{path} is not a folder on {machine.name}")
        # Launchers mount the resolved folder, so it must pass the same text rule,
        # and a symlink must not route a grant into RCP's own storage.
        check_writable_path_text(real)
        refuse_grants_inside([path, real], owned)
    return paths


def _validated_hidden_folders(
    machine: SpaceMachineRecord, requested: list[str], catalog: ProjectCatalog
) -> list[str]:
    from rcp.agents.hidden_read import SYSTEM_RUNTIME_ROOTS, validate_machine_hidden_folders

    # Launches refuse a folder covering a configured provider binary; refuse it at save too.
    provider_roots = tuple(
        str(PurePosixPath(path).parent)
        for _provider, host, path in catalog.provider_targets()
        if host == machine.host and path
    )
    # The policy owner checks syntax and bounds before any host request.
    paths = validate_machine_hidden_folders(
        requested, protected_roots=(*SYSTEM_RUNTIME_ROOTS, *provider_roots)
    )
    if not paths:
        return []
    checkouts = [
        item.path
        for item in catalog.repository_ownership_inventory()
        if item.execution_host == machine.host
    ]
    result = run_machine_directory_request(
        machine.host,
        {"mode": "check", "paths": sorted({*paths, *checkouts})},
        os_account=machine.os_account,
    )
    home = result.get("home")
    resolved = result.get("resolved")
    if (
        not isinstance(home, str)
        or not PurePosixPath(home).is_absolute()
        or not isinstance(resolved, dict)
        or set(resolved) != {*paths, *checkouts}
    ):
        raise ValueError("The machine returned an invalid folder check.")
    root = PurePosixPath(home) / ".rcp"
    protected = [
        *checkouts,
        str(root / "stages"),
        str(root / "tools"),
        str(root / "browser"),
        command_socket_directory(home),
        # Settings has no launch-host key confirmation: these parents must remain readable.
        str(PurePosixPath(home) / ".ssh"),
        str(PurePosixPath(home) / ".ssh/known_hosts"),
        str(PurePosixPath(home) / ".local/share/rcp/credentials"),
        *SYSTEM_RUNTIME_ROOTS,
        *provider_roots,
    ]
    if not machine.host:
        protected.extend(str(catalog.data_dir / name) for name in ("run-stage", "tools", "browser"))
        from rcp.server_ops.config import load_installed_server_config
        from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT

        if DEFAULT_SERVER_LAYOUT.config_path.exists():
            layout = load_installed_server_config(DEFAULT_SERVER_LAYOUT.config_path).paths
            if Path(layout.data_dir).resolve() == catalog.data_dir.resolve():
                protected.append(layout.credentials_root)
    legacy = result.get("legacy_stages", [])
    if not isinstance(legacy, list) or not all(isinstance(path, str) for path in legacy):
        raise ValueError("The machine returned an invalid folder check.")
    protected.extend(legacy)
    # Resolve protected directories on that host as well; never realpath an SSH path locally.
    targets = run_machine_directory_request(
        machine.host,
        {"path": home, "limit": 1, "protect": sorted(set(protected))},
        os_account=machine.os_account,
    ).get("protected_targets")
    if not isinstance(targets, list) or not all(isinstance(value, str) for value in targets):
        raise ValueError("The machine returned an invalid protected-folder check.")
    protected.extend(targets)
    actual = []
    for path in paths:
        value = resolved[path]
        if not isinstance(value, str):
            raise ValueError(f"{path} is not a folder on {machine.name}.")
        actual.append(value)
    validate_machine_hidden_folders(paths, protected_roots=tuple(protected))
    return validate_machine_hidden_folders(actual, protected_roots=tuple(protected))


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
            _machine_view(machine, usage, complete, visible, store.path.parent)
            for machine in store.space_machines()
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
    # SSH picks the account from the host route and sign-in is keyed by host, so
    # a second account on one host could not be told apart. The local card is
    # filled from the running server's own account.
    if not machine.host or any(card.host == machine.host for card in store.space_machines()):
        raise HTTPException(status_code=409, detail="This space already has that machine.")
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
    hidden_folders = None
    if body.hidden_folders is not None:
        try:
            hidden_folders = _validated_hidden_folders(machine, body.hidden_folders, catalog)
        except (MachineBrowseFailure, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": getattr(exc, "code", "hidden_folder_rejected"),
                    "message": str(exc),
                },
            ) from exc
    updated = store.update_space_machine(
        machine_id, name=body.name, writable_paths=writable_paths, hidden_folders=hidden_folders
    )
    return _one_machine_view(store, updated, _visible_project_ids(request, identity_access, store))


@router.delete("/api/space/machines/{machine_id}")
def delete_space_machine(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
) -> dict[str, object]:
    with MACHINE_ATTACHMENT_LOCK:
        view = _one_machine_view(
            store,
            _machine_or_404(store, machine_id),
            _visible_project_ids(request, identity_access, store),
        )
        if view["in_use"] is not False:
            detail = (
                "A project uses this machine; remove it from the project first."
                if view["in_use"]
                else "RCP could not read every project, so it cannot tell whether this "
                "machine is used."
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
    research = _repository_state_paths(catalog, machine)
    try:
        page = browse_machine_directory(
            machine.host,
            body.path,
            os_account=machine.os_account,
            name_filter=body.filter,
            offset=body.offset,
            protect=research,
        )
    except (MachineBrowseFailure, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Saving locks a state folder's symlink target too, so the picker does.
    owned = [
        *_owned_paths(machine, page.home, catalog.data_dir),
        *research,
        *page.protected_targets,
    ]
    return {
        "path": page.path,
        "parent": page.parent,
        "entries": [
            {
                "name": entry.name,
                "path": entry.path,
                # A symlink is judged by where it lands too, as saving does.
                "protected": _is_protected(entry.path, owned)
                or _is_protected(entry.resolved, owned),
            }
            for entry in page.entries
        ],
        "total": page.total,
        "next_offset": page.next_offset,
    }


@router.get("/api/space/machines/{machine_id}/browser")
def machine_browser(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    """Readiness reaches the host, so cards load it per machine, not with the list."""
    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    return readiness(
        host=machine.host, os_account=machine.os_account, data_dir=catalog.data_dir
    ).model_dump()


@router.post("/api/space/machines/{machine_id}/browser/install")
def install_machine_browser(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    """Explicit bounded install on the authenticated member's selected machine."""
    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    return install_browser(
        host=machine.host, os_account=machine.os_account, data_dir=catalog.data_dir
    ).model_dump()
