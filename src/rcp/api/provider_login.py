"""Human per-login actions and viewer-filtered Settings rows."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_provider_credentials,
    get_provider_sign_ins,
    get_store,
)
from rcp.config import load_manifest
from rcp.limits import PROVIDER_TOKEN_MAX_CHARS
from rcp.projects import ProjectCatalog
from rcp.provider_login_rows import provider_login_row
from rcp.providers import PROVIDER_IDS, ProviderId, profile_for
from rcp.runs import provider_login
from rcp.runs.provider_sign_in import (
    ProviderLoginRefused,
    ProviderSignInRunner,
    ProviderSignInStatus,
)
from rcp.service import agent_login_accounts
from rcp.storage import AppStore
from rcp.storage.models import LoginKey, ProviderLoginSetting

_LOGIN_STATE_FIELDS = {
    "provider",
    "host",
    "login_id",
    "state",
    "generation",
    "detail",
    "source",
    "changed_at",
    "changed_by",
}


class ProviderAccountRoute(APIRoute):
    """Validation failures must never echo a submitted credential."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "Invalid provider account request.") from None

        return handle


router = APIRouter(route_class=ProviderAccountRoute)
StoreDependency = Annotated[AppStore, Depends(get_store)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
CredentialsDependency = Annotated[ProviderCredentialStore, Depends(get_provider_credentials)]
SignInsDependency = Annotated[ProviderSignInRunner, Depends(get_provider_sign_ins)]


class ProviderAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""
    login_id: str = ""


class ProviderTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""
    login_id: str = ""
    token: str = Field(min_length=1, max_length=PROVIDER_TOKEN_MAX_CHARS)


class ProviderCredentialSummary(BaseModel):
    """What the UI may know about a stored setup token: never the token."""

    model_config = ConfigDict(extra="forbid")

    pasted_at: str
    pasted_by: str
    verified_at: str | None = None
    #: An estimate from the documented lifetime; a classified login failure is the truth.
    estimated_expiry_at: str | None = None


class ProviderLoginAccount(BaseModel):
    """One shared or member-owned provider login on a space machine."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    host: str
    login_id: str
    kind: Literal["shared", "own"]
    owner_user_id: str | None
    owner_display_name: str | None
    added_by_display_name: str | None
    added_at: str | None
    limited_at: str | None
    draining_since: str | None
    bound_running: int
    machines: list[str]
    state: Literal["signed_in", "signed_out"]
    generation: int
    detail: str | None
    source: str | None
    changed_at: str
    changed_by: str | None
    label: str
    sign_in_methods: tuple[str, ...]
    #: False when the CLI's own login applies; the row then only says how to run it.
    managed: bool
    login_command: str
    token_instructions: str | None = None
    token: ProviderCredentialSummary | None = None
    sign_in: ProviderSignInStatus | None = None
    #: The machine card's path sign-in launches; None means the machine resolves it.
    provider_path: str | None = None
    #: Some visible project's agents launch on, or hold work parked on, this account.
    agent_used: bool = False


def _refused(exc: ProviderLoginRefused) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def provider_login_accounts(
    store: AppStore,
    catalog: ProjectCatalog,
    credentials: ProviderCredentialStore,
    sign_ins: ProviderSignInRunner,
    visible: set[str],
    viewer_id: str | None = None,
) -> list[ProviderLoginAccount]:
    machines: dict[str, set[str]] = {}
    for machine in store.space_machines():
        machines.setdefault(machine.host, set()).add(machine.name)
    pairs = {(provider, host) for provider, host, _ in catalog.provider_targets()}
    # Every space machine can be signed in, even one no project uses yet.
    pairs.update((provider, host) for host in machines for provider in PROVIDER_IDS)
    # A stored row for a host the space no longer lists is history, not an
    # account anyone can act on; Verify would refuse it as an unknown host.
    pairs.update(
        (state.provider, state.host)
        for state in store.provider_login_states()
        if not state.host or state.host in machines
    )
    used = _agent_used_accounts(store, visible, viewer_id)
    accounts = []
    for provider, host in sorted(pairs):
        for state in store.provider_logins(provider, host):
            key = LoginKey(provider, host, state.login_id)
            accounts.append(
                ProviderLoginAccount.model_validate(
                    provider_login_row(
                        store,
                        credentials,
                        sign_ins,
                        key,
                        viewer_id=viewer_id,
                        machines=sorted(machines.get(host, set())),
                        provider_path=store.provider_binary(provider, host),
                        agent_used=key in used,
                    )
                )
            )
    return accounts


def _agent_used_accounts(
    store: AppStore, visible: set[str], viewer_id: str | None
) -> set[LoginKey]:
    used: set[LoginKey] = set()
    for record in store.projects():
        if record.project_id not in visible:
            continue
        try:
            manifest = load_manifest(record.locator)
        except (FileNotFoundError, OSError, ValueError):
            continue
        used |= agent_login_accounts(manifest, store, record.project_id, viewer_id=viewer_id)
    return used


@router.get("/api/providers/logins")
def provider_logins(
    request: Request,
    store: StoreDependency,
    catalog: CatalogDependency,
    credentials: CredentialsDependency,
    sign_ins: SignInsDependency,
) -> list[ProviderLoginAccount]:
    user = get_identity_access(request).acting_user(request)
    visible = store.member_project_ids(user.user_id)
    return provider_login_accounts(store, catalog, credentials, sign_ins, visible, user.user_id)


@router.post("/api/providers/{provider}/logins/verify")
def verify_provider_login(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    login = LoginKey(provider, body.host, body.login_id)
    try:
        state = sign_ins.verify(login, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    counts = sign_ins.resume_counts(login)
    return {
        "state": state.model_dump(
            mode="json",
            include=_LOGIN_STATE_FIELDS,
        ),
        "resumed": counts,
    }


@router.post("/api/providers/{provider}/logins/sign-in")
def start_provider_sign_in(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    member = get_identity_access(request).acting_user(request)
    login = LoginKey(provider, body.host, body.login_id)
    try:
        return sign_ins.start_sign_in(login, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc


@router.get("/api/providers/{provider}/logins/sign-in/{attempt_id}")
def provider_sign_in_status(
    provider: ProviderId,
    attempt_id: str,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    member = get_identity_access(request).acting_user(request)
    status = sign_ins.sign_in_status(attempt_id)
    if status is None or status.provider != provider or status.started_by != member.user_id:
        raise HTTPException(status_code=404, detail="Unknown sign-in.")
    return status


@router.post("/api/providers/{provider}/logins/sign-in/{attempt_id}/cancel")
def cancel_provider_sign_in(
    provider: ProviderId,
    attempt_id: str,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    member = get_identity_access(request).acting_user(request)
    status = sign_ins.sign_in_status(attempt_id)
    if status is None or status.provider != provider or status.started_by != member.user_id:
        raise HTTPException(status_code=404, detail="Unknown sign-in.")
    try:
        return sign_ins.cancel_sign_in(attempt_id, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc


@router.post("/api/providers/{provider}/logins/token")
def save_provider_token(
    provider: ProviderId,
    body: ProviderTokenRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    login = LoginKey(provider, body.host, body.login_id)
    try:
        state = sign_ins.save_token(login, body.token, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    counts = sign_ins.resume_counts(login)
    return {
        "state": state.model_dump(
            mode="json",
            include=_LOGIN_STATE_FIELDS,
        ),
        "resumed": counts,
    }


@router.post("/api/providers/{provider}/logins/sign-out")
def sign_out_provider_login(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    login = LoginKey(provider, body.host, body.login_id)
    try:
        state = sign_ins.sign_out(login, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    return {
        "state": state.model_dump(
            mode="json",
            include=_LOGIN_STATE_FIELDS,
        )
    }


class ProviderLoginAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""
    kind: Literal["shared", "own"]


class ProviderLoginSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""
    setting: ProviderLoginSetting


def _managed_host(store: AppStore, provider: str, host: str) -> None:
    if not profile_for(provider).authentication.manages_login:
        raise HTTPException(422, "This provider only supports the machine sign-in.")
    if host == "*" or (host and store.space_machine_for(host) is None):
        raise HTTPException(422, "Unknown provider execution host.")


def _setting_item(store: AppStore, member_id: str, provider: str, host: str) -> dict[str, object]:
    setting = store.provider_login_setting(member_id, provider, host)
    return {
        "provider": provider,
        "host": host,
        "setting": setting.setting,
        "changed_at": setting.changed_at,
        "stored": bool(setting.changed_at),
    }


@router.get("/api/providers/logins/settings")
def provider_login_settings(
    request: Request,
    store: StoreDependency,
    catalog: CatalogDependency,
    credentials: CredentialsDependency,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    rows = provider_login_accounts(
        store,
        catalog,
        credentials,
        sign_ins,
        store.member_project_ids(member.user_id),
        member.user_id,
    )
    pairs = sorted(
        {
            (row.provider, row.host)
            for row in rows
            if profile_for(row.provider).authentication.manages_login
        }
    )
    return {
        "settings": [
            _setting_item(store, member.user_id, provider, host) for provider, host in pairs
        ],
        "other_member_count": sum(
            user.user_id != member.user_id and user.removal_started_at is None
            for user in store.space_users()
        ),
    }


@router.put("/api/providers/{provider}/logins/setting")
def set_provider_login_setting(
    provider: ProviderId,
    body: ProviderLoginSettingRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    store = sign_ins.store
    _managed_host(store, provider, body.host)
    store.set_provider_login_setting(member.user_id, provider, body.host, body.setting)
    try:
        assert sign_ins.resume_setting is not None
        resumed = sign_ins.resume_setting(member.user_id, provider, body.host)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    return {**_setting_item(store, member.user_id, provider, body.host), "resumed": resumed}


@router.post("/api/providers/{provider}/logins/add")
def add_provider_login(
    provider: ProviderId,
    body: ProviderLoginAddRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    store = sign_ins.store
    _managed_host(store, provider, body.host)
    try:
        state = store.add_provider_login(
            provider,
            body.host,
            member_id=member.user_id if body.kind == "own" else None,
            added_by=member.user_id,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    status = None
    if "device_code" in profile_for(provider).authentication.methods:
        try:
            status = sign_ins.start_sign_in(
                LoginKey(provider, body.host, state.login_id), member_id=member.user_id
            )
        except ProviderLoginRefused as exc:
            raise _refused(exc) from exc
    return {
        "login_id": state.login_id,
        "state": state.model_dump(mode="json", include=_LOGIN_STATE_FIELDS),
        "sign_in": status,
    }


@router.post("/api/providers/{provider}/logins/remove")
def remove_provider_login(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    store = sign_ins.store
    key = LoginKey(provider, body.host, body.login_id)
    try:
        state = sign_ins.require_login(key, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    if not body.login_id:
        raise HTTPException(422, "The machine sign-in cannot be removed.")
    if state.member_id:
        raise HTTPException(403, "Only shared sign-ins can be removed.")
    try:
        state = provider_login.remove_shared_login(
            store,
            sign_ins.launcher,
            key,
            cancel_sign_in=sign_ins.cancel_login_sign_in,
            member_id=member.user_id,
        )
        count = (
            provider_login.bound_running_count(store, key)
            if state.draining_since and not state.removed_at
            else 0
        )
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    return {
        "state": state.model_dump(mode="json", include=_LOGIN_STATE_FIELDS),
        "draining_since": state.draining_since,
        "removed_at": state.removed_at,
        "bound_running": count,
    }
