"""Admission and recovery policy for machine-account provider logins."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rcp.config import Manifest, load_manifest
from rcp.providers import profile_for
from rcp.storage import AppStore, ProviderLoginStateRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks


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
        if task.request.get("run_on") in {None, "local"}:
            if host == "":
                matches.append((recovery.episode_id, recovery.operation_id))
            continue
        project = store.project(task.project_id)
        if project is None:
            continue
        try:
            resolved_host = provider_login_host(
                load_manifest(project.locator), task.request.get("run_on")
            )
        except (OSError, ValueError):
            logging.getLogger(__name__).warning(
                "Provider login recovery skipped an unavailable execution target."
            )
            continue
        if resolved_host == host:
            matches.append((recovery.episode_id, recovery.operation_id))
    return store.release_provider_auth_recoveries(matches)
