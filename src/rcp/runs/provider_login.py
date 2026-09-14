"""Admission and recovery policy for machine-account provider logins."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from rcp.config import Manifest, load_manifest
from rcp.providers import profile_for
from rcp.storage import AppStore, ProviderLoginStateRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.watchers import WatcherDelivery


class ProviderSignedOut(ValueError):
    """The execution account needs a verified provider sign-in."""


def provider_login_host(manifest: Manifest, run_on: str | None) -> str:
    if run_on in {None, "local"}:
        return ""
    machine = manifest.machine_map.get(run_on)
    if machine is None:
        raise ValueError(f"unknown execution machine: {run_on}")
    return machine.host or ""


def provider_login_refusal(state: ProviderLoginStateRecord) -> str:
    return (
        f"{profile_for(state.provider).label} was signed out at {state.changed_at}: "
        f"{state.detail or 'The provider rejected its login'}. "
        "Sign in on the machine, then use Verify sign-in."
    )


def provider_login_block(
    store: AppStore,
    manifest: Manifest,
    provider: str,
    run_on: str | None,
) -> str | None:
    state = store.provider_login_state(provider, provider_login_host(manifest, run_on))
    return provider_login_refusal(state) if state.state == "signed_out" else None


def require_provider_login(
    store: AppStore,
    manifest: Manifest,
    provider: str,
    run_on: str | None,
) -> None:
    if problem := provider_login_block(store, manifest, provider, run_on):
        raise ProviderSignedOut(problem)


def project_provider_login_block(
    store: AppStore,
    project_id: str,
    provider: str | None,
    run_on: str | None,
) -> str | None:
    # No manifest read is necessary if this provider has no blocked account.
    if not any(
        s.provider == provider and s.state == "signed_out" for s in store.provider_login_states()
    ):
        return None
    if run_on in {None, "local"}:
        state = store.provider_login_state(provider, "")
        return provider_login_refusal(state) if state.state == "signed_out" else None
    project = store.project(project_id)
    if project is None:
        raise KeyError(project_id)
    return provider_login_block(store, load_manifest(project.locator), provider, run_on)


def require_project_provider_login(
    store: AppStore,
    project_id: str,
    provider: str | None,
    run_on: str | None,
) -> None:
    if problem := project_provider_login_block(store, project_id, provider, run_on):
        raise ProviderSignedOut(problem)


def release_provider_auth_recoveries(
    background: BackgroundAgentTasks,
    provider: str,
    host: str,
) -> list[str]:
    store = background.store
    matches = []
    for recovery in store.blocked_provider_auth_recoveries():
        task = store.agent_task(recovery.operation_id)
        if task is None or task.request.get("provider") != provider:
            continue
        if (task.stage_host or "") == host:
            matches.append((recovery.episode_id, recovery.operation_id))
    return store.release_provider_auth_recoveries(matches)


def resume_provider_account(
    background: BackgroundAgentTasks,
    provider: str,
    host: str,
    *,
    reconcile_episodes: Callable[[], int],
    watcher_delivery: WatcherDelivery,
) -> dict[str, int]:
    """Release this account's parked work and run the ordinary episode poll once."""

    store = background.store
    queued = []
    release_provider_auth_recoveries(background, provider, host)
    for project in store.projects():
        for operation_id in store.queued_agent_task_ids(project.project_id):
            task = store.agent_task(operation_id)
            if task is None or task.request.get("provider") != provider:
                continue
            if task.stage_host is not None or task.stage_root is not None:
                task_host = task.stage_host or ""
            else:
                try:
                    manifest = load_manifest(project.locator)
                    task_host = provider_login_host(manifest, task.request.get("run_on"))
                except (OSError, ValueError):
                    logging.getLogger(__name__).warning(
                        "Provider login resume skipped an unavailable queued execution target."
                    )
                    continue
            if task_host == host:
                queued.append(task)
        # A failed Experiment turn is retried only on verified sign-in, never
        # by the periodic reconciler. Match its frozen execution account.
        for episode in store.episodes(project.project_id, limit=None):
            if (
                episode.mode != "experiment_loop"
                or episode.status != "running"
                or not episode.control_node_id
            ):
                continue
            runtime = store.experiment_loop_runtime_for_target(
                project.project_id, episode.control_node_id, episode.graph_target
            )
            current = store.agent_task(runtime.current_operation_id or "")
            if (
                current is not None
                and current.request.get("provider") == provider
                and (current.stage_host or "") == host
                and current.failure_kind == "provider_auth"
                and current.status == "failed"
                and current.can_retry
            ):
                background.retry(current.operation_id)
    # This is the timer's existing pass, with its process-owned reconciler and
    # login gates. It has no account filter; still-blocked accounts stay parked.
    checked = reconcile_episodes()
    for task in queued:
        if not store.agent_task_has_receipt(task.operation_id, "operation_dispatch_started"):
            background.launch_admitted(task.operation_id)
            checked += 1
    for group in store.completed_watcher_groups():
        first = group[0]
        if first.continuation.provider == provider and (first.execution_host or "") == host:
            watcher_delivery.deliver_watcher_group(group)
            checked += 1
    return {"checked": checked}
