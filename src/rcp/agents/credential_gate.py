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
running, and a minimum stagger covers the refresh itself, because neither CLI
says when it rotates the token and the first line may well precede that call.
Releasing on the first line alone would assume an ordering we cannot observe
without spending the very token at risk.

This narrows the window rather than closing it: a provider that refreshes again
mid-turn is outside any boundary RCP controls.
"""

from __future__ import annotations

import asyncio

from rcp.limits import (
    PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS,
    PROVIDER_CREDENTIAL_STARTUP_TIMEOUT_SECONDS,
)


class CredentialStartupHold:
    """One admitted startup.

    Every exit path must release. Releasing twice is safe, and the hold expires
    on its own so a provider that never speaks cannot strand the credential for
    the length of its turn.
    """

    def __init__(self, lock: asyncio.Lock | None) -> None:
        self._lock = lock
        self._expiry: asyncio.TimerHandle | None = None
        self._pending: asyncio.TimerHandle | None = None
        self._earliest = 0.0
        if lock is not None:
            loop = asyncio.get_running_loop()
            self._earliest = loop.time() + PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS
            self._expiry = loop.call_later(
                PROVIDER_CREDENTIAL_STARTUP_TIMEOUT_SECONDS, self._release_now
            )

    def release(self) -> None:
        """End this startup, no earlier than the minimum stagger allows."""

        if self._lock is None or self._pending is not None:
            return
        loop = asyncio.get_running_loop()
        if loop.time() < self._earliest:
            self._pending = loop.call_at(self._earliest, self._release_now)
            return
        self._release_now()

    def _release_now(self) -> None:
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
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def hold(self, provider: str, host: str) -> CredentialStartupHold:
        """Wait for exclusive use of this credential's startup window.

        The key is the credential's identity, not the turn's: one provider login
        per execution account, where the host names the account. A blocked
        caller waits rather than failing, because a staggered start is the
        intended behavior and every holder releases within a bounded window.
        """

        lock = self._locks.get((provider, host))
        if lock is None:
            lock = asyncio.Lock()
            self._locks[(provider, host)] = lock
        await lock.acquire()
        return CredentialStartupHold(lock)
