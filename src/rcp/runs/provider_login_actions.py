"""Integration boundary for the independently implemented login lifecycle slice.

The legacy machine release remains usable before integration. Added-login
release, setting release, drain counts and removal require the keyed lifecycle
implementation; missing actions fail explicitly rather than claim completion.
Merge wiring replaces these adapters with the lifecycle owner's keyed calls.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rcp.runs.provider_sign_in import ProviderLoginRefused
from rcp.storage.models import LoginKey, ProviderLoginStateRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks
    from rcp.runs.provider_sign_in import ProviderSignInRunner
    from rcp.storage import AppStore
    from rcp.watchers import WatcherDelivery


def resume_provider_account(
    background: BackgroundAgentTasks,
    login: LoginKey,
    *,
    reconcile_episodes: Callable[[], int],
    watcher_delivery: WatcherDelivery,
) -> dict[str, int]:
    from rcp.runs.provider_login import resume_provider_account as legacy_resume

    if login.login_id:
        raise ProviderLoginRefused("Login lifecycle integration is unavailable.", status_code=503)
    return legacy_resume(
        background,
        login.provider,
        login.host,
        reconcile_episodes=reconcile_episodes,
        watcher_delivery=watcher_delivery,
    )


def resume_provider_setting(
    sign_ins: ProviderSignInRunner, member_id: str, provider: str, host: str
) -> dict[str, int]:
    raise ProviderLoginRefused("Setting release integration is unavailable.", status_code=503)


def bound_running_count(store: AppStore, login: LoginKey) -> int:
    raise ProviderLoginRefused("Login drain integration is unavailable.", status_code=503)


def remove_shared_login(
    sign_ins: ProviderSignInRunner, login: LoginKey, *, member_id: str
) -> ProviderLoginStateRecord:
    raise ProviderLoginRefused("Login removal integration is unavailable.", status_code=503)
