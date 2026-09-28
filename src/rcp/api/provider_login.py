"""Machine-account sign-in, verification, and sign-out, plus parked-work resumption."""

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
from rcp.limits import PROVIDER_TOKEN_MAX_CHARS
from rcp.projects import ProjectCatalog
from rcp.providers import PROVIDER_IDS, ProviderId, profile_for
from rcp.runs.provider_sign_in import (
    ProviderLoginRefused,
    ProviderPathSource,
    ProviderSignInRunner,
    ProviderSignInStatus,
    provider_path_sources,
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
    """One `(provider, execution account)` pair on a space machine."""

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
    #: The project path sign-in launches; None means the machine's PATH resolves it.
    provider_path: ProviderPathSource | None = None


def _refused(exc: ProviderLoginRefused) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _visible_path(
    sources: list[ProviderPathSource], visible: set[str]
) -> ProviderPathSource | None:
    """The path sign-in uses, shown only when the viewer is on its project."""

    first = next(iter(sources), None)
    return first if first is not None and first.project_id in visible else None


def provider_login_accounts(
    store: AppStore,
    catalog: ProjectCatalog,
    credentials: ProviderCredentialStore,
    sign_ins: ProviderSignInRunner,
    visible: set[str],
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
    paths = provider_path_sources(store)
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
                provider_path=_visible_path(paths.get((provider, host), []), visible),
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
    user = get_identity_access(request).acting_user(request)
    visible = store.member_project_ids(user.user_id)
    return provider_login_accounts(store, catalog, credentials, sign_ins, visible)


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


@router.post("/api/providers/{provider}/logins/sign-in/{login_id}/cancel")
def cancel_provider_sign_in(
    provider: ProviderId,
    login_id: str,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    member = get_identity_access(request).acting_user(request)
    status = sign_ins.sign_in_status(login_id)
    if status is None or status.provider != provider:
        raise HTTPException(status_code=404, detail="Unknown sign-in.")
    try:
        return sign_ins.cancel_sign_in(login_id, member_id=member.user_id)
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
