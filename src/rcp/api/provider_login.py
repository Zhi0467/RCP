"""Machine-account sign-in verification and narrowly scoped parked-work resumption."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from rcp.agents import AgentLauncher
from rcp.agents.launcher import _discover_local_provider
from rcp.api.dependencies import (
    get_background_tasks,
    get_identity_access,
    get_launcher,
    get_store,
    get_watcher_delivery,
)
from rcp.config import load_manifest
from rcp.limits import PROVIDER_LOGIN_DETAIL_MAX_CHARS, PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS
from rcp.providers import ProviderId, profile_for
from rcp.runs.auto_research_delivery import (
    deliver_pending_auto_research_lifecycle,
    deliver_pending_auto_research_mail,
    pending_auto_research_mail_recipients,
)
from rcp.runs.auto_research_recovery import reconcile_due_auto_research_recoveries
from rcp.runs.episodes.reconcile import EpisodeReconciler
from rcp.runs.episodes.report import start_episode_report
from rcp.runs.provider_login import provider_login_host, release_provider_auth_recoveries
from rcp.storage import AppStore
from rcp.storage.models import ProviderLoginStateRecord

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]
LauncherDependency = Annotated[AgentLauncher, Depends(get_launcher)]


class VerifyProviderLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    host: str = ""


@router.get("/api/providers/logins")
def provider_logins(request: Request, store: StoreDependency) -> list[ProviderLoginStateRecord]:
    get_identity_access(request).acting_user(request)
    return store.provider_login_states()


@router.post("/api/providers/{provider}/logins/verify")
def verify_provider_login(
    provider: ProviderId,
    body: VerifyProviderLoginRequest,
    request: Request,
    store: StoreDependency,
    launcher: LauncherDependency,
) -> dict[str, object]:
    member = get_identity_access(request).acting_user(request)
    profile = profile_for(provider)
    # Only configured execution destinations may be invoked through this API.
    machines = [
        machine
        for project in store.projects()
        for manifest in [_project_manifest(project)]
        if manifest is not None
        for machine in manifest.machines
        if machine.host == body.host
    ]
    if body.host and not machines:
        raise HTTPException(status_code=409, detail="Unknown provider execution host.")
    binaries = {
        machine.provider_paths[provider]
        for machine in machines
        if provider in machine.provider_paths
    }
    if len(binaries) > 1:
        raise HTTPException(status_code=409, detail="Provider paths disagree for this account.")
    binary = next(iter(binaries), None)
    if binary is None:
        binary = provider if body.host else _discover_local_provider(provider)
    if binary is None:
        raise HTTPException(status_code=409, detail=f"{profile.label} executable was not found.")
    command = profile.login_probe_command(binary)
    if command is None:
        raise HTTPException(status_code=409, detail="no probe defined")
    with launcher.credential_gate.hold_blocking(provider, body.host):
        current = store.provider_login_state(provider, body.host)
        result = launcher._probe(body.host, command, timeout=PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS)
        if result.returncode != 0 or not result.stdout.strip():
            detail = " ".join(
                (result.stderr or "The provider returned no authenticated answer.").split()
            )[:PROVIDER_LOGIN_DETAIL_MAX_CHARS]
            if current.state == "signed_out" or profile.credential_failure(
                result.stderr + result.stdout
            ):
                current = store.mark_provider_login_failed(
                    provider,
                    body.host,
                    generation=current.generation,
                    detail=detail,
                    source="probe",
                )
                detail = current.detail
            raise HTTPException(status_code=409, detail=detail)
        state = store.mark_provider_login_verified(
            provider, body.host, member_id=member.user_id, detail="Authenticated request succeeded."
        )
        launcher.invalidate_readiness(provider, host=body.host)
        for configured in binaries:
            launcher.invalidate_readiness(provider, host=body.host, binary=configured)
    counts = _resume_account(request, provider, body.host)
    return {"state": state.model_dump(mode="json"), "resumed": counts}


def _resume_account(request: Request, provider: str, host: str) -> dict[str, int]:
    background = get_background_tasks(request)
    store = background.store
    counts = dict.fromkeys(
        ("recoveries", "lifecycle", "mail", "reports", "experiments", "watchers"), 0
    )
    contexts = []
    operation_ids: set[str] = set()
    for project in store.projects():
        manifest = _project_manifest(project)
        if manifest is None:
            continue
        for episode in store.episodes(project.project_id, limit=None):
            matching = [
                task
                for task in store.episode_tasks(episode.episode_id, include_hidden=True)
                if task.request.get("provider") == provider
                and provider_login_host(manifest, task.request.get("run_on")) == host
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
                counts["lifecycle"] += int(
                    deliver_pending_auto_research_lifecycle(
                        background, episode_id=episode.episode_id
                    )
                    is not None
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
    for group in store.completed_watcher_groups():
        first = group[0]
        manifest = _project_manifest(store.project(first.project_id))
        if (
            manifest is not None
            and first.continuation.provider == provider
            and provider_login_host(manifest, first.continuation.run_on) == host
        ):
            get_watcher_delivery(request).deliver_watcher_group(group)
            counts["watchers"] += int(
                all(store.watcher(item.watcher_id).notified for item in group)
            )
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
