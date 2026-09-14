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
from rcp.runs.auto_research_delivery import (
    deliver_pending_auto_research_mail,
    pending_auto_research_mail_recipients,
    reconcile_pending_auto_research_lifecycle,
)
from rcp.runs.auto_research_recovery import reconcile_due_auto_research_recoveries
from rcp.runs.episodes.reconcile import EpisodeReconciler
from rcp.runs.episodes.report import start_episode_report
from rcp.runs.provider_login import provider_login_host, release_provider_auth_recoveries
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
    pairs.update((state.provider, state.host) for state in store.provider_login_states())
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
    background = get_background_tasks(request)
    store = background.store
    counts = dict.fromkeys(
        ("recoveries", "lifecycle", "mail", "reports", "experiments", "watchers", "queued"), 0
    )
    contexts = []
    queued = []
    operation_ids: set[str] = set()
    for project in store.projects():
        for operation_id in store.queued_agent_task_ids(project.project_id):
            task = store.agent_task(operation_id)
            if task is None or task.request.get("provider") != provider:
                continue
            if task.stage_host is not None or task.stage_root is not None:
                task_host = task.stage_host or ""
            else:
                manifest = _project_manifest(project)
                if manifest is None:
                    continue
                try:
                    task_host = provider_login_host(manifest, task.request.get("run_on"))
                except (OSError, ValueError):
                    logging.getLogger(__name__).warning(
                        "Provider login resume skipped an unavailable queued execution target."
                    )
                    continue
            if task_host == host:
                queued.append(task)
        for episode in store.episodes(project.project_id, limit=None):
            matching = [
                task
                for task in store.episode_tasks(episode.episode_id, include_hidden=True)
                if task.request.get("provider") == provider and (task.stage_host or "") == host
            ]
            wrapup = store.episode_wrapup(episode.episode_id)
            if not matching and not (
                wrapup is not None and wrapup.provider == provider and wrapup.execution_host == host
            ):
                continue
            contexts.append((project, episode, matching))
            operation_ids.update(task.operation_id for task in matching)
    release_provider_auth_recoveries(background, provider, host)
    awaiting_recovery = {
        recovery.operation_id
        for recovery in store.due_auto_research_recoveries()
        if recovery.operation_id in operation_ids
        and store.auto_research_task_recovery_child(recovery.operation_id) is None
    }
    reconcile_due_auto_research_recoveries(background, operation_ids=operation_ids)
    counts["recoveries"] = sum(
        store.auto_research_task_recovery_child(operation_id) is not None
        for operation_id in awaiting_recovery
    )
    reconciler = EpisodeReconciler(store, background, logger=logging.getLogger(__name__))
    for project, episode, matching in contexts:
        if episode.mode == "auto_research" and episode.root_operation_id:
            root = store.auto_research_actor_binding(episode.root_operation_id)
            if any(task.operation_id == root.current_operation_id for task in matching):
                started = reconcile_pending_auto_research_lifecycle(
                    background, episode_id=episode.episode_id
                )
                counts["lifecycle"] += sum(
                    store.agent_task_has_receipt(operation_id, "operation_dispatch_started")
                    for operation_id in started
                )
            for _, recipient in pending_auto_research_mail_recipients(
                store, episode_id=episode.episode_id
            ):
                child = store.auto_research_child_work(recipient)
                current_id = (
                    child.current_operation_id
                    if child is not None
                    else store.auto_research_actor_binding(recipient).current_operation_id
                )
                if any(task.operation_id == current_id for task in matching):
                    counts["mail"] += int(
                        deliver_pending_auto_research_mail(
                            background, episode_id=episode.episode_id, recipient_task_id=recipient
                        )
                        is not None
                    )
        elif episode.status == "running" and episode.control_node_id:
            runtime = store.experiment_loop_runtime_for_target(
                project.project_id, episode.control_node_id, episode.graph_target
            )
            current = store.agent_task(runtime.current_operation_id or "")
            if (
                current is not None
                and current in matching
                and current.failure_kind == "provider_auth"
                and current.status == "failed"
                and current.can_retry
            ):
                background.retry(current.operation_id)
                counts["experiments"] += 1
        wrapup = store.episode_wrapup(episode.episode_id)
        if (
            wrapup is not None
            and wrapup.provider == provider
            and wrapup.execution_host == host
            and episode.wrapup_state in {"pending", "running"}
        ):
            with background._controls_lock:
                already_running = wrapup.allocation_operation_id in background._workers
            if not already_running:
                counts["reports"] += int(
                    start_episode_report(background, episode.episode_id) is not None
                )
        elif (
            episode.wrapup_state == "not_started"
            and episode.status == "wrapping_up"
            and matching
            and matching[-1].operation_id
            == store.episode_tasks(episode.episode_id)[-1].operation_id
        ):
            if episode.mode == "auto_research":
                reconciler.reconcile_auto_research_episode(
                    episode.episode_id, source="login_verify"
                )
            else:
                reconciler.reconcile_experiment_episode(episode.episode_id, source="login_verify")
    for task in queued:
        if store.agent_task_has_receipt(task.operation_id, "operation_dispatch_started"):
            continue
        background.launch_admitted(task.operation_id)
        counts["queued"] += int(
            store.agent_task_has_receipt(task.operation_id, "operation_dispatch_started")
        )
    for group in store.completed_watcher_groups():
        first = group[0]
        # The watcher froze its account when it was armed; the manifest alias it
        # carries may since have been removed or repointed.
        if first.continuation.provider != provider or (first.execution_host or "") != host:
            continue
        get_watcher_delivery(request).deliver_watcher_group(group)
        counts["watchers"] += int(all(store.watcher(item.watcher_id).notified for item in group))
    return counts


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
