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
import fcntl
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from rcp.limits import (
    PROVIDER_CREDENTIAL_ACQUIRE_SLICE_SECONDS,
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

    def __init__(
        self,
        lock: threading.Lock | None,
        *,
        minimum: float | None = None,
        across_processes: int | None = None,
    ) -> None:
        self._guard = threading.Lock()
        self._lock = lock
        self._across_processes = across_processes
        self._expiry: threading.Timer | None = None
        self._pending: threading.Timer | None = None
        self._earliest = 0.0
        if lock is not None:
            if minimum is None:
                minimum = PROVIDER_CREDENTIAL_STARTUP_MIN_HOLD_SECONDS
            self._earliest = time.monotonic() + minimum
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
            descriptor, self._across_processes = self._across_processes, None
            for timer in (self._expiry, self._pending):
                if timer is not None:
                    timer.cancel()
            self._expiry = self._pending = None
        if descriptor is not None:
            _drop_account_lock(descriptor)
        if lock is not None:
            lock.release()


class ProviderCredentialGate:
    """Admit one provider startup at a time per credential."""

    def __init__(self, *, account_lock_root: Path | None = None) -> None:
        # One registry lock, because several worker threads reach a shared
        # launcher and an unguarded read-then-create would hand two startups
        # separate locks for one credential.
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        #: Named so a test never takes a lock in the human's own home.
        self._account_lock_root = account_lock_root or _DEFAULT_ACCOUNT_LOCK_ROOT

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

        claim = _Claim(self._lock_for(provider, host), self._account_lock_path(provider, host))
        while True:
            # `to_thread` cannot be cancelled, so each attempt is bounded and
            # shielded, and the claim below decides who owns a win. Short
            # attempts also hand the worker thread back between tries, so a
            # waiting startup cannot pin the loop's executor through shutdown.
            attempt = asyncio.ensure_future(asyncio.to_thread(claim.try_acquire))
            try:
                acquired = await asyncio.shield(attempt)
            except asyncio.CancelledError:
                claim.abandon()
                raise
            if acquired:
                return CredentialStartupHold(claim.lock, across_processes=claim.descriptor)

    @contextmanager
    def hold_blocking(self, provider: str, host: str) -> Iterator[None]:
        """Hold the credential across a synchronous provider probe.

        Readiness and skill inventory run provider executables on worker
        threads, and those load the same login as a turn does. A probe is short
        and runs to completion, so it holds for its whole duration rather than
        until a first line, and needs no minimum: a turn's first line can
        precede its authentication, while an exited probe cannot.
        """

        claim = _Claim(self._lock_for(provider, host), self._account_lock_path(provider, host))
        while not claim.try_acquire():
            pass
        hold = CredentialStartupHold(claim.lock, minimum=0.0, across_processes=claim.descriptor)
        try:
            yield
        finally:
            hold.release()

    def _account_lock_path(self, provider: str, host: str) -> Path | None:
        """Where this OS account's lock for one provider login lives.

        Remote execution has no such file: the credential sits on the other
        machine and only a lock taken there would mean anything, which is an
        SSH round trip per launch. Remote launches keep in-process
        serialization only.
        """

        if host:
            return None
        return self._account_lock_root / f"{provider}.lock"

    def _lock_for(self, provider: str, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault((provider, host.strip().lower()), threading.Lock())


class _Claim:
    """Hand one lock from the acquiring thread to its caller, or give it back.

    Neither side may consult the asyncio wrapper. `to_thread` cannot be
    cancelled, and during loop teardown the runner cancels the wrapper before
    the worker finishes, so a win reported only through that future would be
    lost and the credential locked until RCP restarts. Both sides meet on this
    guard instead, so whichever arrives second cleans up.
    """

    def __init__(self, lock: threading.Lock, account_lock: Path | None) -> None:
        self.lock = lock
        self.descriptor: int | None = None
        self._account_lock = account_lock
        self._guard = threading.Lock()
        self._abandoned = False
        self._won = False

    def try_acquire(self) -> bool:
        """Win both locks for a caller that still wants them, else give back."""

        if not self.lock.acquire(True, PROVIDER_CREDENTIAL_ACQUIRE_SLICE_SECONDS):
            return False
        try:
            descriptor = _take_account_lock(self._account_lock)
        except _AccountLockBusy:
            # Another RCP process holds this account. Step off the in-process
            # lock so a sibling turn is not queued behind our polling.
            self.lock.release()
            time.sleep(PROVIDER_CREDENTIAL_ACQUIRE_SLICE_SECONDS)
            return False
        with self._guard:
            if not self._abandoned:
                self._won = True
                self.descriptor = descriptor
                return True
        self._give_back(descriptor)
        return False

    def abandon(self) -> None:
        """Give up this claim, releasing a win the caller will never receive."""

        with self._guard:
            self._abandoned = True
            if not self._won:
                return
            self._won = False
            descriptor, self.descriptor = self.descriptor, None
        self._give_back(descriptor)

    def _give_back(self, descriptor: int | None) -> None:
        if descriptor is not None:
            _drop_account_lock(descriptor)
        self.lock.release()


#: Alongside the existing ~/.rcp/jobs, so one account keeps one RCP directory.
_DEFAULT_ACCOUNT_LOCK_ROOT = Path.home() / ".rcp" / "credential-locks"


class _AccountLockBusy(Exception):
    """Another RCP process under this account holds the provider login."""


def _take_account_lock(path: Path | None) -> int | None:
    """Claim this account's provider login against other RCP processes.

    Two RCP processes with different data directories share one provider login,
    and the single-instance lock only excludes a second process on the same data
    directory. The OS releases this lock when its holder exits, so a crashed
    holder never strands the login.

    Returning None means there is no such lock to take. Contention raises, so a
    caller retries a busy account instead of spinning on a home it cannot use.
    """

    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        # A home that cannot hold the lock file must not stop a turn; in-process
        # serialization still covers the common single-process case.
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise _AccountLockBusy(str(path)) from exc
    return descriptor


def _drop_account_lock(descriptor: int) -> None:
    with suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    with suppress(OSError):
        os.close(descriptor)


def _start_timer(delay: float, action: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()
    return timer
