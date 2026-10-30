"""Login-keyed admission, parked-work release, limit evidence, and credential drains."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rcp.agents.provider_accounts import ProviderAccounts
from rcp.config import Manifest, load_manifest
from rcp.providers import profile_for
from rcp.server_ops.provider_cleanup import cleanup_provider_login_credential
from rcp.storage.models import LoginKey

if TYPE_CHECKING:
    from rcp.agents.launcher import AgentLauncher
    from rcp.background import BackgroundAgentTasks
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord, ProviderLoginStateRecord, StoredWatcherRecord
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


def task_login_host(task: AgentTaskRecord, manifest: Callable[[], Manifest]) -> str:
    """Prefer a captured stage host; resolve queued tasks from their execution target."""
    if task.stage_host is not None or task.stage_root is not None:
        return task.stage_host or ""
    return provider_login_host(manifest(), str(task.request.get("run_on") or "local"))


@dataclass
class ParkedProviderWork:
    recoveries: list[tuple[str, str]] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    experiments: list[str] = field(default_factory=list)
    watcher_groups: list[list[StoredWatcherRecord]] = field(default_factory=list)


def parked_provider_work(
    store: AppStore, project_id: str, manifest: Callable[[], Manifest]
) -> dict[LoginKey, ParkedProviderWork]:
    """Select the account-bound work a verified sign-in can release."""
    work: dict[LoginKey, ParkedProviderWork] = defaultdict(ParkedProviderWork)
    for recovery in store.blocked_provider_auth_recoveries():
        task = store.agent_task(recovery.operation_id)
        # Only provider-launching work holds an account; other queued work names none.
        if task is not None and task.project_id == project_id and task.request.get("provider"):
            account = resolved_task_login(store, task)
            if account is not None:
                work[account].recoveries.append((recovery.episode_id, recovery.operation_id))
    for operation_id in store.queued_agent_task_ids(project_id):
        task = store.agent_task(operation_id)
        if task is None or not task.request.get("provider"):
            continue
        try:
            host = task_login_host(task, manifest)
        except (OSError, ValueError):
            logging.getLogger(__name__).warning(
                "Provider login skipped an unavailable queued execution target."
            )
            continue
        account = resolved_task_login(store, task, host=host)
        if account is not None:
            work[account].queued.append(operation_id)
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
            account = resolved_task_login(store, current)
            if account is not None:
                work[account].experiments.append(current.operation_id)
    for group in store.completed_watcher_groups():
        first = group[0]
        if first.project_id == project_id:
            origin = store.agent_task(first.origin_operation_id)
            account = (
                resolved_task_login(store, origin, host=first.execution_host or "")
                if origin is not None
                else None
            )
            if account is not None:
                work[account].watcher_groups.append(group)
    return work


def resume_provider_account(
    background: BackgroundAgentTasks,
    key: LoginKey,
    *,
    reconcile_episodes: Callable[[], int] = lambda: 0,
    watcher_delivery: WatcherDelivery | None = None,
    member_id: str | None = None,
) -> dict[str, int]:
    """Release this account's parked work and run the ordinary episode poll once."""

    store = background.store
    accounts = ProviderAccounts.for_store(store)
    if accounts.refusal(key):
        return {"checked": 0}
    verified = store.provider_login_state(key)

    def eligible(operation_id: str) -> bool:
        task = store.agent_task(operation_id)
        if task is None:
            return False
        if (
            task.status == "queued"
            and task.parent_operation_id
            and (
                store.agent_task_has_receipt(task.operation_id, "provider_login_lineage")
                or store.agent_task_has_receipt(task.parent_operation_id, "transport_auto_retry")
            )
        ):
            return member_id is None and resolved_task_login(store, task, host=key.host) == key
        authorizer = task.authorized_by.user_id if task.authorized_by else None
        session = task.native_session_id or str(task.request.get("fork_session_id") or "")
        if profile_for(key.provider).sessions_bound_to_login and session:
            bound = accounts.bound_login(key.provider, key.host, session)
            return (
                member_id is None
                and (bound or resolved_task_login(store, task, host=key.host)) == key
            )
        if member_id is not None and authorizer != member_id:
            return False
        if authorizer is None or not profile_for(key.provider).authentication.manages_login:
            return not key.login_id
        setting = store.provider_login_setting(authorizer, key.provider, key.host).setting
        return (verified.member_id == authorizer and "own" in setting) or (
            verified.member_id is None and "shared" in setting
        )

    selected = []
    for project in store.projects():
        parked = parked_provider_work(
            store, project.project_id, lambda project=project: load_manifest(project.locator)
        )
        for resolved, work in parked.items():
            if (resolved.provider, resolved.host) != (key.provider, key.host):
                continue
            work.recoveries = [pair for pair in work.recoveries if eligible(pair[1])]
            work.queued = [operation_id for operation_id in work.queued if eligible(operation_id)]
            work.experiments = [
                operation_id for operation_id in work.experiments if eligible(operation_id)
            ]
            work.watcher_groups = [
                group for group in work.watcher_groups if eligible(group[0].origin_operation_id)
            ]
            selected.append(work)
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
            if watcher_delivery is not None:
                watcher_delivery.deliver_watcher_group(group)
                checked += 1
    return {"checked": checked}


def resolved_task_login(
    store: AppStore, task: AgentTaskRecord, *, host: str | None = None
) -> LoginKey | None:
    """Dry-run a turn boundary, retaining Codex's session binding."""
    provider = str(task.request.get("provider") or "")
    if not provider:
        return None
    host = task.stage_host or "" if host is None else host
    accounts = ProviderAccounts.for_store(store)
    result, _ = accounts.resolve_launch_login(
        provider,
        host,
        task.authorized_by.user_id if task.authorized_by else None,
        session_id=task.native_session_id,
        fork_session_id=str(task.request.get("fork_session_id") or ""),
        task=task,
        inherit_parent=task.status == "queued",
    )
    if isinstance(result, LoginKey):
        return result
    # Keep a parked binding discoverable while it is unhealthy. Release still
    # checks usability and re-resolves unbound work before dispatch.
    return result.login or (
        LoginKey(provider, host, task.login_id) if task.login_id is not None else None
    )


def provider_login_changed(
    background: BackgroundAgentTasks,
    *,
    key: LoginKey | None = None,
    member_id: str | None = None,
    provider: str = "",
    host: str = "",
    reconcile_episodes: Callable[[], int],
    watcher_delivery: WatcherDelivery,
) -> dict[str, int]:
    """Release hook for a verified login, or one member's changed setting."""
    if key is not None:
        return resume_provider_account(
            background,
            key,
            reconcile_episodes=reconcile_episodes,
            watcher_delivery=watcher_delivery,
        )
    if member_id is None or not provider:
        raise ValueError("A setting change requires its member and provider.")
    checked = 0
    for row in background.store.provider_logins(provider, host):
        checked += resume_provider_account(
            background,
            LoginKey(provider, host, row.login_id),
            member_id=member_id,
            watcher_delivery=watcher_delivery,
        )["checked"]
    return {"checked": checked + reconcile_episodes()}


def settle_provider_login(background: BackgroundAgentTasks, record: AgentTaskRecord) -> None:
    """Apply evidence and release only after the owner recorded its durable park."""
    store = background.store
    record = store.agent_task(record.operation_id) or record
    provider = str(record.request.get("provider") or "")
    if not provider or record.login_id is None:
        return
    key = LoginKey(provider, record.stage_host or "", record.login_id)
    starts = [
        receipt.created_at
        for receipt in store.agent_task_receipts(record.operation_id)
        if receipt.category == "provider_runtime_selected"
    ]
    limited = background._failure_classified_as(record, "session_limit")
    if starts and (limited or record.status == "succeeded"):
        store.set_provider_login_limit(key, provider_started_at=min(starts), limited=limited)
    # A Codex turn without a named session is still free to choose another login.
    if profile_for(provider).sessions_bound_to_login and record.native_session_id:
        return
    chosen = resolved_task_login(store, record)
    if chosen is None:
        return
    if record.failure_kind == "provider_auth":
        resume_provider_account(background, chosen)
        return
    if not limited or not record.episode_id or not record.native_session_id or chosen == key:
        return
    if store.provider_login_limited(chosen, continued=True):
        return
    episode = store.episode(record.episode_id)
    if episode is None or episode.status != "running" or episode.stop_requested_at:
        return
    # The claim is durable and serialized with all competing settlement passes.
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT 1 FROM graph_run_receipts WHERE category='provider_login_limit_retry' "
            "AND json_extract(payload_json, '$.provider')=? "
            "AND json_extract(payload_json, '$.host')=? "
            "AND json_extract(payload_json, '$.session')=? "
            "AND json_extract(payload_json, '$.login_id')=? LIMIT 1",
            (provider, key.host, record.native_session_id, key.login_id),
        ).fetchone()
        if prior is not None:
            return
        store._insert_agent_task_receipt(
            connection,
            record.operation_id,
            "provider_login_limit_retry",
            json.dumps(
                {
                    "provider": provider,
                    "host": key.host,
                    "session": record.native_session_id,
                    "login_id": key.login_id,
                    "to": chosen.login_id,
                }
            ),
            tier="summary",
            created_at=store.now(),
        )
    if episode.mode == "experiment_loop" and record.can_retry:
        background.retry(record.operation_id)


def bound_running_count(store: AppStore, key: LoginKey) -> int:
    """Count running turns and running unpaused Codex episodes retaining this login."""
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT operation_id, episode_id, native_session_id, status FROM graph_runs "
            "WHERE json_extract(request_json, '$.provider')=? AND COALESCE(stage_host,'')=? "
            "AND login_id=? ORDER BY rowid DESC",
            key,
        ).fetchall()
    running = sum(row["status"] in {"running", "pausing"} for row in rows)
    if not profile_for(key.provider).sessions_bound_to_login:
        return running
    episodes: set[str] = set()
    seen_sessions: set[str] = set()
    for row in rows:
        session = row["native_session_id"]
        if not session or session in seen_sessions:
            continue
        seen_sessions.add(session)
        if row["status"] == "paused" or not row["episode_id"]:
            continue
        episode = store.episode(row["episode_id"])
        if episode is not None and episode.status == "running" and not episode.stop_requested_at:
            episodes.add(episode.episode_id)
    return running + len(episodes)


def remove_shared_login(
    store: AppStore,
    launcher: AgentLauncher,
    key: LoginKey,
    *,
    cancel_sign_in: Callable[[LoginKey], None],
    member_id: str | None = None,
) -> ProviderLoginStateRecord:
    """Cancel sign-in before taking its gate; drain before deleting credentials."""
    state = store.provider_login_state(key)
    if not key.login_id or state.member_id is not None:
        raise ValueError("Only an added shared sign-in can be removed.")
    if state.removed_at:
        return state
    cancel_sign_in(key)
    store.set_provider_login_draining(key)
    with launcher.credential_gate.hold_blocking(key):
        if not bound_running_count(store, key):
            store.finish_provider_login_removal(key, member_id=member_id)
            delete_removed_credential(store, key)
    return store.provider_login_state(key)


def reconcile_login_removals(store: AppStore, launcher: AgentLauncher) -> None:
    """Finish drains and retry unreachable credential deletion after restart."""
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT provider, host, login_id FROM provider_login_states "
            "WHERE draining_since IS NOT NULL AND "
            "(removed_at IS NULL OR detail = ?)",
            (_CLEANUP_PENDING,),
        ).fetchall()
    for row in rows:
        key = LoginKey(row["provider"], row["host"], row["login_id"])
        with launcher.credential_gate.hold_blocking(key):
            if bound_running_count(store, key):
                continue
            store.finish_provider_login_removal(key)
            delete_removed_credential(store, key)


_CLEANUP_PENDING = "Sign-in removed. Remote credential deletion is pending; RCP will retry."


def delete_removed_credential(store: AppStore, key: LoginKey, *, defer: bool = False) -> bool:
    """Delete only the credential, or with `defer` leave it pending; False if not deleted.

    A pending row with `draining_since` set is what `reconcile_login_removals`
    retries. Session folders and provider state survive.
    """
    deleted = not defer and cleanup_provider_login_credential(store, key)
    with store.connection() as connection:
        connection.execute(
            "UPDATE provider_login_states SET detail=? WHERE provider=? AND host=? AND login_id=?",
            ("Sign-in removed." if deleted else _CLEANUP_PENDING, *key),
        )
    return deleted
