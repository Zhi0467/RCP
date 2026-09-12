"""Serialize provider startup per credential so token rotation cannot race.

A provider login is one rotating credential file owned by one operating-system
account: Codex keeps `auth.json` under `CODEX_HOME`, Claude keeps
`.credentials.json` under its config directory. Both refresh lazily while a
process starts, and neither CLI locks the file while it does so. Two providers
starting together therefore read the same refresh token, and because the token
is single-use the loser's write can clobber the winner's rotated token. The
credential is then dead until a human logs in again, which is worse than the
failed turn that exposed it.

RCP is the component that fans those launches out, so RCP is the component that
has to stagger them. The gate below admits one startup at a time per
credential. Turns still run concurrently once past startup.

A hold ends at the later of two marks. The provider's first line proves it is
running, and a minimum stagger covers the refresh itself, because no CLI
reports when it rotates the token and the first line demonstrably precedes that
call: Codex answers a local `initialize` RPC and Claude emits `system/init`
before either authenticates. Confirming the true ordering would mean forcing a
refresh, which spends the very token at risk.

Every primitive here is a threading primitive, not an asyncio one. Background
tasks each run `asyncio.run` on their own thread, so one launcher's cached lock
is contended from several event loops at once; an `asyncio.Lock` would wake its
waiters on the wrong loop and never release them.

This narrows the window rather than closing it: a provider that refreshes again
mid-turn is outside any boundary RCP controls.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

from rcp.limits import (
    PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS,
    PROVIDER_CREDENTIAL_STARTUP_TIMEOUT_SECONDS,
)


class CredentialStartupHold:
    """One admitted startup.

    Every exit path must release. Releasing twice is safe, releasing from a
    thread other than the acquiring one is safe, and the hold expires on its own
    so a provider that never speaks cannot strand the credential for the length
    of its turn.
    """

    def __init__(self, lock: threading.Lock | None) -> None:
        self._guard = threading.Lock()
        self._lock = lock
        self._expiry: threading.Timer | None = None
        self._pending: threading.Timer | None = None
        self._earliest = 0.0
        if lock is not None:
            self._earliest = time.monotonic() + PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS
            self._expiry = _start_timer(
                PROVIDER_CREDENTIAL_STARTUP_TIMEOUT_SECONDS, self._release_now
            )

    def release(self) -> None:
        """End this startup, no earlier than the minimum stagger allows."""

        with self._guard:
            if self._lock is None or self._pending is not None:
                return
            remaining = self._earliest - time.monotonic()
            if remaining > 0:
                self._pending = _start_timer(remaining, self._release_now)
                return
        self._release_now()

    def _release_now(self) -> None:
        with self._guard:
            lock, self._lock = self._lock, None
            for timer in (self._expiry, self._pending):
                if timer is not None:
                    timer.cancel()
            self._expiry = self._pending = None
        if lock is not None:
            lock.release()


class ProviderCredentialGate:
    """Admit one provider startup at a time per credential."""

    def __init__(self) -> None:
        # One registry lock, because several worker threads reach a shared
        # launcher and an unguarded read-then-create would hand two startups
        # separate locks for one credential.
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}

    async def hold(self, provider: str, host: str) -> CredentialStartupHold:
        """Wait for exclusive use of this credential's startup window.

        The key is the credential's identity, not the turn's: one provider login
        per execution account, where the host names the account. A blocked
        caller waits rather than failing, because a staggered start is the
        intended behavior and every holder releases within a bounded window.

        Two spellings of one destination stay distinct here beyond case and
        surrounding space. Proving them identical needs the remote account
        probe, which is an SSH round trip this path cannot afford on every
        turn, so an aliased duplicate of one machine keeps the old race.
        Folding case is the safe direction: merging two keys only staggers
        startups that did not need it.
        """

        with self._guard:
            lock = self._locks.setdefault((provider, host.strip().lower()), threading.Lock())
        await asyncio.to_thread(lock.acquire)
        return CredentialStartupHold(lock)


def _start_timer(delay: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()
    return timer
