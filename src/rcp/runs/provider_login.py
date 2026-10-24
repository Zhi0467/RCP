"""Admission and recovery policy for machine-account provider logins."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rcp.config import Manifest, load_manifest

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.storage import AppStore
    from rcp.storage.models import StoredWatcherRecord
    from rcp.watchers import WatcherDelivery


class AdmissionRefused(ValueError):
    """An admission refusal that defers work without creating a row or debiting.

    Admission callers catch this base to keep pending wakes and reserved
    allocations for a later attempt.
    """


class ProviderSignedOut(AdmissionRefused):
    """The execution account needs a verified provider sign-in."""


class MachineDependenciesMissing(AdmissionRefused):
    """The execution machine lacks a required program or is unsupported."""


def provider_login_host(manifest: Manifest, run_on: str | None) -> str:
    if run_on in {None, "local"}:
        return ""
    machine = manifest.machine_map.get(run_on)
    if machine is None:
        raise ValueError(f"unknown execution machine: {run_on}")
    return machine.host or ""


@dataclass
class ParkedProviderWork:
    recoveries: list[tuple[str, str]] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    experiments: list[str] = field(default_factory=list)
    watcher_groups: list[list[StoredWatcherRecord]] = field(default_factory=list)


def parked_provider_work(
    store: AppStore, project_id: str, manifest: Callable[[], Manifest]
) -> dict[tuple[str, str], ParkedProviderWork]:
    """Select the account-bound work a verified sign-in can release."""
    work: dict[tuple[str, str], ParkedProviderWork] = defaultdict(ParkedProviderWork)
    for recovery in store.blocked_provider_auth_recoveries():
        task = store.agent_task(recovery.operation_id)
        # Only provider-launching work holds an account; other queued work names none.
        if task is not None and task.project_id == project_id and task.request.get("provider"):
            account = (str(task.request["provider"]), task.stage_host or "")
            work[account].recoveries.append((recovery.episode_id, recovery.operation_id))
    for operation_id in store.queued_agent_task_ids(project_id):
        task = store.agent_task(operation_id)
        if task is None or not task.request.get("provider"):
            continue
        if task.stage_host is not None or task.stage_root is not None:
            host = task.stage_host or ""
        else:
            try:
                host = provider_login_host(manifest(), str(task.request.get("run_on") or "local"))
            except (OSError, ValueError):
                logging.getLogger(__name__).warning(
                    "Provider login skipped an unavailable queued execution target."
                )
                continue
        work[(str(task.request["provider"]), host)].queued.append(operation_id)
    for episode in store.episodes(project_id, limit=None):
        if (
            episode.mode != "experiment_loop"
            or episode.status != "running"
            or not episode.control_node_id
        ):
            continue
        runtime = store.experiment_loop_runtime(
            project_id, episode.control_node_id, graph_target=episode.graph_target
        )
        current = store.agent_task(runtime.current_operation_id or "")
        if (
            current is not None
            and current.request.get("provider")
            and current.failure_kind == "provider_auth"
            and current.status == "failed"
            and current.can_retry
        ):
            account = (str(current.request["provider"]), current.stage_host or "")
            work[account].experiments.append(current.operation_id)
    for group in store.completed_watcher_groups():
        first = group[0]
        if first.project_id == project_id:
            work[(first.continuation.provider, first.execution_host or "")].watcher_groups.append(
                group
            )
    return work


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
    selected = [
        parked_provider_work(
            store, project.project_id, lambda project=project: load_manifest(project.locator)
        ).get((provider, host), ParkedProviderWork())
        for project in store.projects()
    ]
    for work in selected:
        store.release_provider_auth_recoveries(work.recoveries)
        for operation_id in work.experiments:
            background.retry(operation_id)
    # This is the timer's existing pass, with its process-owned reconciler and
    # login gates. It has no account filter; still-blocked accounts stay parked.
    checked = reconcile_episodes()
    for work in selected:
        for operation_id in work.queued:
            if not store.agent_task_has_receipt(operation_id, "operation_dispatch_started"):
                background.launch_admitted(operation_id)
                checked += 1
        for group in work.watcher_groups:
            watcher_delivery.deliver_watcher_group(group)
            checked += 1
    return {"checked": checked}
