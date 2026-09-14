"""Machine-account sign-in, verification, and sign-out, plus parked-work resumption."""

from __future__ import annotations

import logging
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
from rcp.providers import ProviderId, profile_for
from rcp.runs.provider_sign_in import (
    ProviderLoginRefused,
    ProviderSignInRunner,
    ProviderSignInStatus,
)
from rcp.storage import AppStore


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


class ProviderTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""
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
    """One `(provider, execution account)` pair every project on this server may launch on."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    host: str
    machines: list[str]
    state: Literal["signed_in", "signed_out"]
    generation: int
    detail: str | None
    source: str | None
    changed_at: str
    changed_by: str | None
    label: str
    sign_in_methods: tuple[str, ...]
    token_instructions: str | None = None
    token: ProviderCredentialSummary | None = None
    sign_in: ProviderSignInStatus | None = None


def _refused(exc: ProviderLoginRefused) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def provider_login_accounts(
    store: AppStore,
    catalog: ProjectCatalog,
    credentials: ProviderCredentialStore,
    sign_ins: ProviderSignInRunner,
) -> list[ProviderLoginAccount]:
    machines: dict[str, set[str]] = {}
    for project in store.projects():
        manifest = _project_manifest(project)
        if manifest is None:
            continue
        for machine in manifest.machines:
            machines.setdefault(machine.host, set()).add(machine.alias)
    pairs = {(provider, host) for provider, host, _ in catalog.provider_targets()}
    # A stored row for a host no project names any more is history, not an
    # account anyone can act on; Verify would refuse it as an unknown host.
    pairs.update(
        (state.provider, state.host)
        for state in store.provider_login_states()
        if not state.host or state.host in machines
    )
    accounts = []
    for provider, host in sorted(pairs):
        state = sign_ins.account_state(provider, host)
        profile = profile_for(provider)
        metadata = profile.authentication.credential_metadata(credentials, host)
        token = ProviderCredentialSummary(**metadata) if metadata is not None else None
        accounts.append(
            ProviderLoginAccount(
                **state.model_dump(),
                label=profile.label,
                sign_in_methods=profile.authentication.methods,
                token_instructions=profile.authentication.token_instructions,
                machines=sorted(machines.get(host, set())),
                token=token,
                sign_in=sign_ins.running_sign_in(provider, host),
            )
        )
    return accounts


@router.get("/api/providers/logins")
def provider_logins(
    request: Request,
    store: StoreDependency,
    catalog: CatalogDependency,
    credentials: CredentialsDependency,
    sign_ins: SignInsDependency,
) -> list[ProviderLoginAccount]:
    get_identity_access(request).acting_user(request)
    return provider_login_accounts(store, catalog, credentials, sign_ins)


@router.post("/api/providers/{provider}/logins/verify")
def verify_provider_login(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    try:
        state = sign_ins.verify(provider, body.host, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    counts = sign_ins.resume_counts(provider, body.host)
    return {"state": state.model_dump(mode="json"), "resumed": counts}


@router.post("/api/providers/{provider}/logins/sign-in")
def start_provider_sign_in(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    member = get_identity_access(request).acting_user(request)
    try:
        return sign_ins.start_sign_in(provider, body.host, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc


@router.get("/api/providers/{provider}/logins/sign-in/{login_id}")
def provider_sign_in_status(
    provider: ProviderId,
    login_id: str,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    get_identity_access(request).acting_user(request)
    status = sign_ins.sign_in_status(login_id)
    if status is None or status.provider != provider:
        raise HTTPException(status_code=404, detail="Unknown sign-in.")
    return status


@router.post("/api/providers/{provider}/logins/token")
def save_provider_token(
    provider: ProviderId,
    body: ProviderTokenRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    try:
        state = sign_ins.save_token(provider, body.host, body.token, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    counts = sign_ins.resume_counts(provider, body.host)
    return {"state": state.model_dump(mode="json"), "resumed": counts}


@router.post("/api/providers/{provider}/logins/sign-out")
def sign_out_provider_login(
    provider: ProviderId,
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    try:
        state = sign_ins.sign_out(provider, body.host, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    return {"state": state.model_dump(mode="json")}


def _project_manifest(project):
    if project is None:
        return None
    try:
        return load_manifest(project.locator)
    except (OSError, ValueError):
        logging.getLogger(__name__).warning(
            "Provider login resume skipped an unavailable project manifest."
        )
        return None
