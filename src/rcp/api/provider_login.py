"""Machine-account sign-in, verification, and sign-out, plus parked-work resumption."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rcp.agents.provider_environment import ProviderCredentialStore
from rcp.api.dependencies import (
    get_background_tasks,
    get_catalog,
    get_episode_reconciliation,
    get_identity_access,
    get_provider_credentials,
    get_provider_sign_ins,
    get_store,
    get_watcher_delivery,
)
from rcp.config import load_manifest
from rcp.limits import PROVIDER_CLAUDE_TOKEN_ESTIMATED_LIFETIME_DAYS, PROVIDER_TOKEN_MAX_CHARS
from rcp.projects import ProjectCatalog
from rcp.providers import ProviderId
from rcp.runs.provider_login import resume_provider_account
from rcp.runs.provider_sign_in import (
    ProviderLoginRefused,
    ProviderSignInRunner,
    ProviderSignInStatus,
)
from rcp.storage import AppStore

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
CredentialsDependency = Annotated[ProviderCredentialStore, Depends(get_provider_credentials)]
SignInsDependency = Annotated[ProviderSignInRunner, Depends(get_provider_sign_ins)]


class ProviderAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""


class ClaudeTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""
    token: str = Field(min_length=1, max_length=PROVIDER_TOKEN_MAX_CHARS)


class ClaudeTokenSummary(BaseModel):
    """What the UI may know about a stored setup token: never the token."""

    model_config = ConfigDict(extra="forbid")

    pasted_at: str
    pasted_by: str
    verified_at: str | None = None
    #: An estimate from the documented lifetime; a classified login failure is the truth.
    estimated_expiry_at: str


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
    token: ClaudeTokenSummary | None = None
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
        state = store.provider_login_state(provider, host)
        token = None
        if provider == "claude" and (record := credentials.claude_token_record(host)) is not None:
            pasted = datetime.fromisoformat(record.pasted_at)
            token = ClaudeTokenSummary(
                pasted_at=record.pasted_at,
                pasted_by=record.pasted_by,
                verified_at=record.verified_at,
                estimated_expiry_at=(
                    pasted + timedelta(days=PROVIDER_CLAUDE_TOKEN_ESTIMATED_LIFETIME_DAYS)
                ).isoformat(),
            )
        accounts.append(
            ProviderLoginAccount(
                **state.model_dump(),
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
    counts = _resume_account(request, provider, body.host)
    return {"state": state.model_dump(mode="json"), "resumed": counts}


@router.post("/api/providers/codex/logins/sign-in")
def start_codex_sign_in(
    body: ProviderAccountRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    member = get_identity_access(request).acting_user(request)
    try:
        return sign_ins.start_codex_sign_in(body.host, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc


@router.get("/api/providers/codex/logins/sign-in/{login_id}")
def codex_sign_in_status(
    login_id: str,
    request: Request,
    sign_ins: SignInsDependency,
) -> ProviderSignInStatus:
    get_identity_access(request).acting_user(request)
    status = sign_ins.sign_in_status(login_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown sign-in.")
    if status.state == "succeeded" and status.resumed is None:
        # The login is verified; parked work resumes exactly as after Verify.
        counts = _resume_account(request, "codex", status.host)
        status = sign_ins.record_resumed(login_id, counts) or status
    return status


@router.post("/api/providers/claude/logins/token")
def save_claude_token(
    body: ClaudeTokenRequest,
    request: Request,
    sign_ins: SignInsDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    try:
        state = sign_ins.save_claude_token(body.host, body.token, member_id=member.user_id)
    except ProviderLoginRefused as exc:
        raise _refused(exc) from exc
    counts = _resume_account(request, "claude", body.host)
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


def _resume_account(request: Request, provider: str, host: str) -> dict[str, int]:
    return resume_provider_account(
        get_background_tasks(request),
        provider,
        host,
        reconcile_episodes=get_episode_reconciliation(request),
        watcher_delivery=get_watcher_delivery(request),
    )


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
