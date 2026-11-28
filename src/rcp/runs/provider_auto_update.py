"""Shared maintenance for scheduled and member-requested provider CLI updates."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.providers import PROVIDER_IDS, ProviderId, profile_for
from rcp.runs.provider_login import run_on_machine
from rcp.server_ops.provider_update import (
    LocalUpdateTarget,
    ProviderUpdateRefused,
    ProviderVersionChange,
    UpdateTarget,
    update_installed_provider,
)
from rcp.storage.models import MACHINE_LOGIN_ID

if TYPE_CHECKING:
    import pwd
    import threading
    from collections.abc import Callable

    from rcp.agents.launcher import AgentLauncher
    from rcp.server_ops.provider_update import ProviderProcessRunner
    from rcp.storage import AppStore

    Guard = Callable[[], AbstractContextManager[object]]

logger = logging.getLogger(__name__)


def turns_using(store: AppStore, provider: ProviderId, machine_id: str) -> int:
    """Count active and admitted turns on the exact provider and machine.

    The CLI counterpart is server_ops.provider_update._refuse_while_turns_run.
    """
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT project_id, stage_machine_id, json_extract(request_json, '$.run_on') AS run_on "
            "FROM graph_runs WHERE json_extract(request_json, '$.provider')=? "
            "AND status IN ('queued','running','pausing')",
            (provider,),
        ).fetchall()
    return sum(
        (row["stage_machine_id"] or run_on_machine(store, row["project_id"], row["run_on"]))
        == machine_id
        for row in rows
    )


def update_provider(
    store: AppStore,
    launcher: AgentLauncher,
    provider: ProviderId,
    machine_id: str,
    *,
    target: UpdateTarget,
    stop: threading.Event,
    guard: Guard = nullcontext,
) -> ProviderVersionChange:
    """Admit one update, then run and verify it under the caller's mutation guard."""
    if stop.is_set():
        raise ProviderUpdateRefused("Provider updates are stopping.")
    with launcher.provider_update(provider, machine_id):
        count = turns_using(store, provider, machine_id)
        if count:
            raise ProviderUpdateRefused(f"{count} turns are using {profile_for(provider).label}.")
        with guard():
            try:
                configured = launcher.provider_binary(provider, machine_id)
                binary = configured or target.discover(provider)
                if binary is None:
                    raise ProviderUpdateRefused(f"{profile_for(provider).label} is not installed.")
                change = update_installed_provider(target, provider, binary)
            except (OSError, ProviderUpdateRefused) as exc:
                launcher.set_update_warning(provider, machine_id, str(exc))
                raise
            launcher.set_update_warning(provider, machine_id, None)
            login = LoginKey(provider, machine_id, MACHINE_LOGIN_ID)
            launcher.invalidate_provider_readiness(provider, machine_id)
    launcher.readiness(login, binary=configured, refresh=True)
    return change


def auto_update_providers(
    store: AppStore,
    launcher: AgentLauncher,
    *,
    account: pwd.struct_passwd,
    runner: ProviderProcessRunner,
    stop: threading.Event,
    guard: Guard = nullcontext,
) -> None:
    """Update idle backend installations once, skipping missing or external binaries."""
    target = LocalUpdateTarget(account, runner)
    for provider in PROVIDER_IDS:
        if stop.is_set():
            return
        binary = launcher.provider_binary(provider, BACKEND_MACHINE_ID) or target.discover(provider)
        if binary is None or not PurePosixPath(binary).is_relative_to(PurePosixPath(target.home)):
            continue
        try:
            change = update_provider(
                store,
                launcher,
                provider,
                BACKEND_MACHINE_ID,
                target=target,
                stop=stop,
                guard=guard,
            )
        except (OSError, ProviderUpdateRefused) as exc:
            logger.info("Provider update did not complete for %s: %s", provider, exc)
            continue
        if change.after != change.before:
            logger.info("Provider %s updated from %s to %s", provider, change.before, change.after)
