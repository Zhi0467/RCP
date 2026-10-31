"""Fence a departing member's own logins and delete only their credentials.

Removed rows remain the retry inventory while member removal is pending. Remote
cleanup executes this module's credential-only helper; sessions and provider
configuration are never deleted.
"""

from __future__ import annotations

import importlib.resources
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.storage.models import LoginKey


def remove_remote_credential(login_id: str, relative_paths: tuple[str, ...]) -> None:
    """Delete one added login's credential on the execution account."""
    parsed = UUID(login_id)
    if parsed.version != 4 or str(parsed) != login_id:
        raise ValueError("Login id must be UUID4 text.")
    for relative in relative_paths:
        path = Path.home() / relative.format(login_id=login_id)
        path.unlink(missing_ok=True)


def cleanup_member_provider_logins(store: AppStore, member_id: str) -> tuple[str, ...]:
    """Fence own logins, then delete credentials nothing runs on; return unreached hosts."""
    fence_member_provider_logins(store, member_id)
    return delete_member_provider_credentials(store, member_id)


def fence_member_provider_logins(store: AppStore, member_id: str) -> None:
    """Remove the member's own logins and settings in this database; touch no host."""
    for row, login in _member_logins(store, member_id):
        if row["removed_at"] is None:
            # Draining marks the row for reconcile_login_removals, which retries a
            # pending credential after member removal has completed.
            store.set_provider_login_draining(login)
        store.finish_provider_login_removal(login, member_id=member_id)
    with store.connection() as connection:
        connection.execute("DELETE FROM provider_login_settings WHERE user_id = ?", (member_id,))


def delete_member_provider_credentials(store: AppStore, member_id: str) -> tuple[str, ...]:
    """Delete fenced own credentials nothing runs on; return unreached hosts."""
    from rcp.runs.provider_login import bound_running_count, delete_removed_credential

    # Include earlier passes' rows: their credentials may still be pending.
    unreached: set[str] = set()
    for _row, login in _member_logins(store, member_id):
        # A turn already running on a fenced login keeps its credential until it ends.
        running = bool(bound_running_count(store, login))
        if not delete_removed_credential(store, login, defer=running) and not running:
            unreached.add(login.host)
    return tuple(sorted(unreached))


def _member_logins(store: AppStore, member_id: str) -> list[tuple[sqlite3.Row, LoginKey]]:
    from rcp.storage.models import LoginKey

    with store.connection() as connection:
        rows = connection.execute(
            "SELECT provider, host, login_id, removed_at FROM provider_login_states "
            "WHERE member_id = ?",
            (member_id,),
        ).fetchall()
    return [(row, LoginKey(row["provider"], row["host"], row["login_id"])) for row in rows]


def cleanup_provider_login_credential(store: AppStore, login: LoginKey) -> bool:
    """Delete only this added credential; False means its remote host was unreached.

    The caller owns the durable fence. This also cleans a native credential
    written by a device attempt that completed after its login was removed.
    """
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.limits import PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS
    from rcp.providers import profile_for
    from rcp.transport.ssh import ssh_arguments

    if not login.login_id:
        raise ValueError("The machine sign-in cannot be removed.")
    credentials = ProviderCredentialStore.for_data_dir(store.path.parent)
    profile = profile_for(login.provider)
    if not profile.login_credential_files:
        raise ValueError("Provider has no added credentials.")
    directory = credentials.account_dir(*login)
    for relative in profile.login_credential_files:
        (directory / relative).unlink(missing_ok=True)
    if not login.host:
        for name in profile.login_home_credentials:
            (credentials.login_home(login.provider, login.login_id) / name).unlink(missing_ok=True)
        return True
    source = importlib.resources.files("rcp.server_ops").joinpath("provider_cleanup.py").read_text()
    command = shlex.join(
        ["python3", "-c", source, login.login_id, *profile.remote_login_credential_files]
    )
    try:
        result = subprocess.run(
            ssh_arguments(login.host, command),
            capture_output=True,
            timeout=PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


if __name__ == "__main__":
    import sys

    remove_remote_credential(sys.argv[1], tuple(sys.argv[2:]))
