"""One pass that keeps the backend account's own provider CLIs current.

The backend runs as the account that installed the CLIs, so each native update
runs without root. The executable is the backend machine's configured one, else
the account's discovered one. A provider is updated only when that executable
lives in the account's home and no backend turn using it is running or queued;
otherwise it waits for the next pass. While it updates, the launcher holds back
new local launches of that provider, so no turn starts between the check and the
update. A failed update leaves the installed version in use
and is recorded as that provider's update warning on the backend machine card.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.machine_cards import BACKEND_MACHINE_ID, LoginKey
from rcp.providers import PROVIDER_IDS
from rcp.server_ops.provider_update import (
    ProviderUpdateRefused,
    backend_turns_using,
    installed_provider,
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


def auto_update_providers(
    store: AppStore,
    launcher: AgentLauncher,
    *,
    account: pwd.struct_passwd,
    runner: ProviderProcessRunner,
    stop: threading.Event,
    guard: Guard = nullcontext,
) -> None:
    """Update each idle, home-installed provider CLI once; log one line each.

    `guard` wraps each provider's update, and `stop` ends the pass between providers.
    """

    home = Path(account.pw_dir)
    for provider in PROVIDER_IDS:
        if stop.is_set():
            return
        configured = launcher.provider_binary(provider, BACKEND_MACHINE_ID)
        binary = Path(configured) if configured else installed_provider(account, provider)
        if binary is None or not binary.is_relative_to(home):
            continue
        with guard(), launcher.provider_update(provider):
            if backend_turns_using(store.path, provider, queued=True):
                logger.info("Provider update deferred, %s has backend turns", provider)
                continue
            try:
                change = update_installed_provider(account, provider, binary, runner)
            except (OSError, ProviderUpdateRefused) as exc:
                logger.warning("Provider update failed for %s: %s", provider, exc)
                launcher.set_update_warning(provider, BACKEND_MACHINE_ID, str(exc))
                continue
        launcher.set_update_warning(provider, BACKEND_MACHINE_ID, None)
        if change.after == change.before:
            continue
        logger.info("Provider %s updated from %s to %s", provider, change.before, change.after)
        launcher.readiness(
            LoginKey(provider, BACKEND_MACHINE_ID, MACHINE_LOGIN_ID),
            binary=configured,
            refresh=True,
        )
