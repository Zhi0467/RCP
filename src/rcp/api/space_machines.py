"""Space machine cards: list, add, configure, check, and browse folders.

A card is the one place a machine is configured; projects only name the cards
they use (`rcp.api.project_state`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import threading
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.agents import AgentLauncher
from rcp.agents.grant_paths import check_writable_path_text, refuse_grants_inside
from rcp.agents.hidden_read import cached_hidden_read_readiness, hidden_read_defaults
from rcp.agents.write_scope import rcp_owned_paths
from rcp.api.dependencies import (
    _api_services,
    get_catalog,
    get_identity_access,
    get_project_display_cache,
    get_store,
    project_write_admission,
)
from rcp.api.identity import IdentityAccess
from rcp.browser import BrowserReadiness, enable_linger, install_browser, readiness
from rcp.compute import probe_machine_reach
from rcp.compute_jobs.probe import refresh_compute_probes, settle_job_manager
from rcp.compute_jobs.routes import ComputeRoute
from rcp.config import (
    MachineComputeConfig,
    load_manifest,
    validate_machine_access_hint,
    validate_machine_provider_paths,
)
from rcp.core.models import MachineHiddenReadProjection
from rcp.limits import HIDDEN_READ_PATH_MAX_COUNT
from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey, invalidate_machine
from rcp.machine_login import MachineLoginFailure, login_account, trust_host_key
from rcp.operator_commands import operator_route
from rcp.projects import ProjectCatalog, ProjectDisplayCache
from rcp.providers import PROVIDER_IDS, ProviderId, profile_for
from rcp.rcp_home import command_socket_directory
from rcp.setup import MachineBrowseFailure, browse_machine_directory, run_machine_directory_request
from rcp.ssh_validation import validate_ssh_destination
from rcp.storage import AppStore
from rcp.storage.models import MACHINE_LOGIN_ID, SpaceMachineHostInUse, SpaceMachineRecord
from rcp.transport.ssh import control_directory_candidate

logger = logging.getLogger(__name__)

router = APIRouter()
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
DisplayCacheDependency = Annotated[ProjectDisplayCache, Depends(get_project_display_cache)]

#: Held across "is this card used?" and delete, and across adding a card to a
#: project, so a delete cannot slip between an add's read and its manifest write.
MACHINE_ATTACHMENT_LOCK = threading.Lock()

#: Bounds one PATCH; a card holds a handful of grants, not a mount table.
_MAX_WRITABLE_PATHS = 64

_OS_ACCOUNT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")

#: The last reach check of each card, by data directory and card: run on demand,
#: never on a read, because an unreachable SSH target can take its full timeout.
_REACH: dict[tuple[str, str], list[dict[str, object]]] = {}
_REACH_LOCK = threading.Lock()


def _one_line(value: str, *, label: str, maximum: int) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{label} must be 1 to {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must be one line")
    return value


def _validated_os_account(value: str) -> str:
    value = value.strip()
    if value and _OS_ACCOUNT.fullmatch(value) is None:
        raise ValueError("machine operating-system account is invalid")
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

    @field_validator("os_account")
    @classmethod
    def validate_os_account(cls, value: str) -> str:
        return _validated_os_account(value)


class TrustHostKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(max_length=255)
    #: The SHA256 fingerprint the human was shown and compared.
    fingerprint: str = Field(max_length=128)


class UpdateSpaceMachineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    #: A new SSH route for the card; checked by logging in, in `_checked_route`.
    host: str | None = Field(default=None, max_length=255)
    os_account: str | None = Field(default=None, max_length=128)
    writable_paths: list[str] | None = Field(default=None, max_length=_MAX_WRITABLE_PATHS)
    hidden_folders: list[str] | None = Field(default=None, max_length=HIDDEN_READ_PATH_MAX_COUNT)
    #: The providers to change; an empty value restores that CLI's own default.
    provider_autocompact: dict[str, Annotated[str, Field(max_length=16)]] | None = Field(
        default=None, max_length=len(PROVIDER_IDS)
    )

    provider_shell_timeout: dict[str, Annotated[str, Field(max_length=16)]] | None = Field(
        default=None, max_length=len(PROVIDER_IDS)
    )
    job_manager: Literal["slurm", "none"] | None = None
    jobs_root: str | None = Field(default=None, max_length=1024)
    access_hint: str | None = Field(default=None, max_length=512)
    #: The providers to change; an empty value forgets that provider's executable.
    provider_paths: dict[ProviderId, Annotated[str, Field(max_length=1024)]] | None = None

    @field_validator("jobs_root")
    @classmethod
    def validate_jobs_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return MachineComputeConfig(jobs_root=value.strip()).jobs_root

    @field_validator("access_hint")
    @classmethod
    def validate_access_hint(cls, value: str | None) -> str | None:
        return None if value is None else validate_machine_access_hint(value)

    @field_validator("provider_shell_timeout")
    @classmethod
    def validate_provider_shell_timeout(
        cls, value: dict[str, str] | None
    ) -> dict[str, str | None] | None:
        if value is None:
            return None
        result: dict[str, str | None] = {}
        for provider, setting in value.items():
            profile = profile_for(provider)
            result[provider] = profile.canonical_shell_timeout(setting) if setting.strip() else None
        return result

    @field_validator("provider_autocompact")
    @classmethod
    def validate_provider_autocompact(
        cls, value: dict[str, str] | None
    ) -> dict[str, str | None] | None:
        if value is None:
            return None
        return {
            provider: (
                profile_for(provider).canonical_autocompact(setting) if setting.strip() else None
            )
            for provider, setting in value.items()
        }

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return None if value is None else _one_line(value, label="machine name", maximum=80)

    @field_validator("os_account")
    @classmethod
    def validate_os_account(cls, value: str | None) -> str | None:
        return None if value is None else _validated_os_account(value)


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


class MachineUsage(NamedTuple):
    """Projects by the card ids their manifests name, and the cards agents and repositories use."""

    projects: dict[str, list[dict[str, str]]]
    complete: bool
    agent_machines: set[str]
    repository_machines: set[str]

    def deletable(self, machine: SpaceMachineRecord) -> bool:
        return (
            self.complete
            and machine.machine_id != BACKEND_MACHINE_ID
            and machine.machine_id not in self.repository_machines
        )


def _machine_usage(store: AppStore) -> MachineUsage:
    """Which registered projects name each card, whether every manifest read, and
    the cards with agents or repositories."""

    usage: dict[str, list[dict[str, str]]] = {}
    agent_machines: set[str] = set()
    repository_machines: set[str] = set()
    complete = True
    for project in store.projects():
        try:
            manifest = load_manifest(project.locator)
        except (OSError, ValueError):
            logger.warning("Machine usage skipped an unreadable project manifest.")
            complete = False
            continue
        for machine_id in manifest.machines:
            usage.setdefault(machine_id, []).append(
                {"project_id": project.project_id, "project_name": project.name}
            )
        agent_machines.add(manifest.base_machine)
        repository_machines.update(
            repository.machine for repository in manifest.repositories if repository.machine
        )
    return MachineUsage(usage, complete, agent_machines, repository_machines)


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


def _compute_probes(
    store: AppStore, machine_id: str, projects: list[dict[str, str]]
) -> dict[str, dict[str, object] | None]:
    """The card's recorded route checks; every project on it shares the card's settings."""

    result: dict[str, dict[str, object] | None] = {"scheduler": None, "helper": None}
    routes: tuple[ComputeRoute, ...] = ("scheduler", "helper")
    for route in routes:
        for project in projects:
            probe = store.compute_backend_probe(project["project_id"], machine_id, route)
            if probe is not None:
                result[route] = probe.model_dump(mode="json")
                break
    return result


def _machine_view(
    machine: SpaceMachineRecord,
    usage: MachineUsage,
    visible: set[str],
    store: AppStore,
    launcher: AgentLauncher | None,
) -> dict[str, object]:
    projects = usage.projects.get(machine.machine_id, [])
    readiness = {
        provider: launcher.cached_readiness(
            LoginKey(provider, machine.machine_id, MACHINE_LOGIN_ID),
            binary=machine.provider_paths.get(provider),
        )
        for provider in PROVIDER_IDS
        if launcher is not None
    }
    return {
        "machine_id": machine.machine_id,
        "name": machine.name,
        "host": machine.host,
        "os_account": machine.os_account,
        "writable_paths": list(machine.writable_paths),
        "hidden_folders": list(machine.hidden_folders),
        "provider_autocompact": dict(machine.provider_autocompact),
        "provider_shell_timeout": dict(machine.provider_shell_timeout),
        "autocompact_providers": [
            {"provider": profile.id, "label": profile.label, "hint": profile.autocompact_hint}
            for profile in map(profile_for, PROVIDER_IDS)
            if profile.autocompact_hint is not None
        ],
        "shell_timeout_providers": [
            {
                "provider": profile.id,
                "label": profile.label,
                "default_minutes": profile.shell_timeout_default_seconds // 60,
            }
            for profile in map(profile_for, PROVIDER_IDS)
        ],
        "job_manager": machine.job_manager,
        "jobs_root": machine.jobs_root,
        "access_hint": machine.access_hint,
        "provider_paths": dict(machine.provider_paths),
        # Each provider's declared models, so a pick lists them before any probe.
        "providers": [
            {
                "provider": profile.id,
                "label": profile.label,
                "models": [choice.model_dump(mode="json") for choice in profile.declared],
            }
            for profile in map(profile_for, PROVIDER_IDS)
        ],
        "provider_readiness": {
            provider: value.model_dump(mode="json")
            for provider, value in readiness.items()
            if value is not None
        },
        "compute_probes": _compute_probes(store, machine.machine_id, projects),
        # Agents run on each project's base machine; only there does the helper launch.
        "agents_run_here": machine.machine_id in usage.agent_machines,
        "reach": _REACH.get((str(store.path), machine.machine_id), []),
        "hidden_read": _hidden_read_projection(machine, store.path.parent).model_dump(mode="json"),
        # Use counts every project; only the viewer's own projects are named.
        "projects": [project for project in projects if project["project_id"] in visible],
        "in_use": True if projects else (False if usage.complete else None),
        "deletable": usage.deletable(machine),
    }


def _one_machine_view(
    store: AppStore,
    machine: SpaceMachineRecord,
    visible: set[str],
    launcher: AgentLauncher | None = None,
) -> dict[str, object]:
    usage = _machine_usage(store)
    return _machine_view(machine, usage, visible, store, launcher)


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
            if item.machine == machine.machine_id
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
        for _provider, machine_id, path in catalog.provider_targets()
        if machine_id == machine.machine_id and path
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
        if item.machine == machine.machine_id
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
    catalog: CatalogDependency,
) -> dict[str, object]:
    visible = _visible_project_ids(request, identity_access, store)
    usage = _machine_usage(store)
    # The Space thread's picker reads the backend's card, so it exists before any turn.
    store.backend_space_machine()
    return {
        "machines": [
            _machine_view(machine, usage, visible, store, catalog.launcher)
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
    host = body.host.strip()
    if host:
        try:
            validate_ssh_destination(host)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    # SSH picks the account from the host route, so a second account on one host
    # could not be told apart. The local card is the backend's own.
    if not host or any(card.host == host for card in store.space_machines()):
        raise HTTPException(status_code=409, detail="This space already has that machine.")
    os_account = body.os_account
    if store.space_kind == "team" and not os_account:
        # Team projects are backed up, and a backup records each machine's
        # account: a blank one is the account this server's login lands on.
        os_account = _login_account(host)
    try:
        created = store.create_space_machine(name=body.name, host=host, os_account=os_account)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _one_machine_view(store, created, _visible_project_ids(request, identity_access, store))


def _login_account(host: str) -> str:
    """The account this server's login to `host` lands on; failures are the routes' 422."""

    try:
        account = login_account(host)
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"code": "unreachable", "message": "unreachable"}
        ) from None
    except MachineLoginFailure as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.code, "fingerprint": exc.fingerprint},
        ) from None
    try:
        return _validated_os_account(account)
    except ValueError:
        raise HTTPException(
            status_code=422, detail={"code": "unreachable", "message": "unreachable"}
        ) from None


def _checked_route(
    store: AppStore, machine: SpaceMachineRecord, body: UpdateSpaceMachineRequest
) -> tuple[str, str]:
    """The card's route after this edit; a changed one must log in, its one check.

    The backend card is the machine RCP runs on and has no route to edit; every
    other card has a host. The login's account is the card's: a named one must
    match it, and a team space fills a blank one, as create does.
    """

    host = machine.host if body.host is None else body.host.strip()
    os_account = machine.os_account if body.os_account is None else body.os_account
    if (host, os_account) == (machine.host, machine.os_account):
        return host, os_account
    if machine.machine_id == BACKEND_MACHINE_ID:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "backend_has_no_route",
                "message": "RCP runs on this machine, so it has no SSH route to edit.",
            },
        )
    if not host:
        raise HTTPException(
            status_code=422,
            detail={"code": "host_required", "message": "Enter the SSH host for this machine."},
        )
    account = _login_account(host)
    if body.os_account and body.os_account != account:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "account_mismatch",
                "message": f"The SSH login lands on {account!r}, not {body.os_account!r}.",
            },
        )
    if os_account or store.space_kind == "team":
        return host, account
    return host, os_account


@router.post("/api/space/machines/host-key")
def trust_machine_host_key(
    body: TrustHostKeyRequest,
    request: Request,
    *,
    identity_access: IdentityDependency,
) -> dict[str, object]:
    """The member's click is the consent to record exactly the key they compared."""
    identity_access.acting_user(request)
    try:
        trust_host_key(body.host, body.fingerprint)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MachineLoginFailure as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": exc.code}
        ) from None
    return {"trusted": body.host}


def _operator_readiness(
    store: AppStore, machine: SpaceMachineRecord, result: BrowserReadiness
) -> dict[str, object]:
    """Browser readiness with its root fix commands written for the viewer's prompt."""
    route = operator_route(store.space_kind, machine.host)
    return result.model_copy(
        update={
            name: route.command(shlex.split(value), root=True)
            for name in ("apt_command", "admin_command")
            if (value := getattr(result, name))
        }
    ).model_dump()


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
    host, os_account = _checked_route(store, machine, body)
    route_changed = (host, os_account) != (machine.host, machine.os_account)
    with contextlib.ExitStack() as route:
        terminals = _api_services(request).terminals
        if route_changed and not route.enter_context(terminals.route_change(machine_id)):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "machine_has_terminals",
                    "message": "Close this machine's terminals before changing its route.",
                },
            )
        if route_changed and store.space_machine_has_work(machine_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "machine_has_work",
                    "message": "Agent work is still running on this machine. "
                    "Change its route once that work ends.",
                },
            )
        target = machine.model_copy(update={"host": host, "os_account": os_account})
        writable_paths = None
        if body.writable_paths is not None:
            try:
                writable_paths = _validated_writable_paths(target, body.writable_paths, catalog)
            except (MachineBrowseFailure, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        hidden_folders = None
        if body.hidden_folders is not None:
            try:
                hidden_folders = _validated_hidden_folders(target, body.hidden_folders, catalog)
            except (MachineBrowseFailure, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": getattr(exc, "code", "hidden_folder_rejected"),
                        "message": str(exc),
                    },
                ) from exc
        provider_paths = None
        if body.provider_paths is not None:
            try:
                recorded = validate_machine_provider_paths(
                    {
                        provider: path
                        for provider, path in body.provider_paths.items()
                        if path.strip()
                    },
                    host=host,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            provider_paths = {provider: recorded.get(provider) for provider in body.provider_paths}
        compute_changed = (
            body.job_manager is not None and body.job_manager != machine.job_manager
        ) or (body.jobs_root is not None and body.jobs_root != machine.jobs_root)
        usage = _machine_usage(store)
        projects = usage.projects.get(machine_id, [])
        if compute_changed:
            # Fail closed: no project keeps a route check made under the old settings.
            for project in projects:
                store.delete_compute_backend_probe(project["project_id"], machine_id)
        # A human edit makes the card the owner: no later start refills a field
        # this edit cleared from an older manifest.
        store.settle_legacy_machine_settings([machine.host])
        try:
            updated = store.update_space_machine(
                machine_id,
                name=body.name,
                host=host if route_changed else None,
                os_account=os_account if route_changed else None,
                writable_paths=writable_paths,
                hidden_folders=hidden_folders,
                provider_autocompact=body.provider_autocompact,
                provider_shell_timeout=body.provider_shell_timeout,
                job_manager=body.job_manager,
                jobs_root=body.jobs_root,
                access_hint=body.access_hint,
                provider_paths=provider_paths,
            )
        except SpaceMachineHostInUse:
            raise HTTPException(
                status_code=409, detail="This space already has that machine."
            ) from None
        if route_changed:
            invalidate_machine(
                store,
                machine_id,
                project_ids=[project["project_id"] for project in projects],
                forget_display_snapshot=catalog.forget_display_snapshot,
            )
            with _REACH_LOCK:
                _REACH.pop((str(store.path), machine_id), None)
        for provider, path in (provider_paths or {}).items():
            for binary in {machine.provider_paths.get(provider), path} - {None}:
                catalog.launcher.invalidate_readiness(
                    LoginKey(provider, machine_id, MACHINE_LOGIN_ID), binary=binary
                )
        if compute_changed:
            # Checked now, so the card this returns already shows the new routes.
            _refresh_machine_probes(store, catalog, updated)
            updated = _machine_or_404(store, machine_id)
        return _one_machine_view(
            store, updated, _visible_project_ids(request, identity_access, store), catalog.launcher
        )


def _refresh_machine_probes(
    store: AppStore, catalog: ProjectCatalog, machine: SpaceMachineRecord
) -> None:
    """Settle the card's job manager, then check its routes once for every project on it."""

    settle_job_manager(store, machine.machine_id)
    usage = _machine_usage(store)
    for project in usage.projects.get(machine.machine_id, []):
        record = store.project(project["project_id"])
        if record is None:
            continue
        try:
            refresh_compute_probes(
                store,
                load_manifest(record.locator),
                record.project_id,
                data_dir=catalog.data_dir,
                machines=[machine.machine_id],
            )
        except Exception as exc:
            logger.warning("Could not check compute routes on %s: %s", machine.name, exc)


@router.post("/api/space/machines/{machine_id}/compute/check")
def check_space_machine_compute(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    """Re-check the card's compute routes and its reach from every agent machine now."""

    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    _refresh_machine_probes(store, catalog, machine)
    usage = _machine_usage(store)
    sources = [card for card in store.space_machines() if card.machine_id in usage.agent_machines]
    reach = [
        {"from": source.name, "probe": probe.model_dump(mode="json")}
        for source, probe in probe_machine_reach(machine, sources, space_kind=store.space_kind)
    ]
    with _REACH_LOCK:
        _REACH[(str(store.path), machine_id)] = reach
    return _one_machine_view(
        store,
        _machine_or_404(store, machine_id),
        _visible_project_ids(request, identity_access, store),
        catalog.launcher,
    )


@router.post("/api/space/machines/{machine_id}/providers/{provider}/resolve")
def resolve_space_machine_provider(
    machine_id: str,
    provider: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    """Find `provider` on the machine and record that executable on its card."""

    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    try:
        profile_for(provider)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Unknown provider") from exc
    launcher = catalog.launcher
    login = LoginKey(provider, machine.machine_id, MACHINE_LOGIN_ID)
    discovered = launcher.readiness(login, refresh=True)
    if discovered.path_state == "unreachable":
        raise HTTPException(
            status_code=422, detail=discovered.reason or f"{machine.name} is unreachable"
        )
    if not discovered.installed or not discovered.binary_path:
        raise HTTPException(
            status_code=422,
            detail=discovered.reason or f"No {provider} executable was found on {machine.name}.",
        )
    store.settle_legacy_machine_settings([machine.host])
    updated = store.update_space_machine(
        machine_id, provider_paths={provider: discovered.binary_path}
    )
    launcher.readiness(
        login,
        binary=discovered.binary_path,
        refresh=True,
    )
    return _one_machine_view(
        store, updated, _visible_project_ids(request, identity_access, store), launcher
    )


@router.delete("/api/space/machines/{machine_id}")
def delete_space_machine(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    project_display_cache: DisplayCacheDependency,
) -> dict[str, object]:
    from rcp.api.project_state import detach_project_machine

    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    listed = sorted(
        {item["project_id"] for item in _machine_usage(store).projects.get(machine_id, [])}
    )
    # Each listing project's write fence comes first, as its own machine route takes
    # it, so a detach never edits a project mid-transfer or under another writer.
    with ExitStack() as fences:
        for project_id in listed:
            fences.enter_context(project_write_admission(project_id, request))
        with MACHINE_ATTACHMENT_LOCK:
            usage = _machine_usage(store)
            if usage.deletable(machine) and store.space_machine_has_work(machine_id):
                raise HTTPException(
                    status_code=409,
                    detail="Agent work is still running on this machine.",
                )
            if not usage.deletable(machine):
                detail = (
                    "A repository lives on this machine."
                    if machine_id in usage.repository_machines
                    else "RCP could not read every project, so it cannot tell whether this "
                    "machine is used."
                )
                raise HTTPException(status_code=409, detail=detail)
            projects = usage.projects.get(machine_id, [])
            if {item["project_id"] for item in projects} - set(listed):
                raise HTTPException(status_code=409, detail="A project started using this machine.")
            for project in projects:
                detach_project_machine(project_display_cache, project["project_id"], machine_id)
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
    return _operator_readiness(
        store,
        machine,
        readiness(host=machine.host, os_account=machine.os_account, data_dir=catalog.data_dir),
    )


@router.get("/api/space/machines/{machine_id}/dependencies")
def machine_dependencies(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    refresh: bool = False,
) -> dict[str, object]:
    """The machine's programs against `rcp.dependencies`; the card asks per machine."""
    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    checker = request.app.state.dependency_checker
    status = checker.status(machine.host, refresh=refresh)
    return status.model_dump(mode="json")


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
    return _operator_readiness(
        store,
        machine,
        install_browser(
            host=machine.host, os_account=machine.os_account, data_dir=catalog.data_dir
        ),
    )


@router.post("/api/space/machines/{machine_id}/linger")
def enable_machine_linger(
    machine_id: str,
    request: Request,
    *,
    identity_access: IdentityDependency,
    store: StoreDependency,
    catalog: CatalogDependency,
) -> dict[str, object]:
    """The member's explicit consent to keep this account's background processes running."""
    identity_access.acting_user(request)
    machine = _machine_or_404(store, machine_id)
    return _operator_readiness(
        store,
        machine,
        enable_linger(host=machine.host, os_account=machine.os_account, data_dir=catalog.data_dir),
    )
