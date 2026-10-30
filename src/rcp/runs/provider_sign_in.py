"""Per-login lifecycle: serialize credentials, publish facts, resume existing work."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import uuid
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import AgentLauncher
from rcp.agents import launcher as launcher_module
from rcp.agents.provider_accounts import (
    ProviderAccounts,
)
from rcp.agents.provider_environment import ProviderProcessEnvironment
from rcp.limits import (
    PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS,
    PROVIDER_SIGN_IN_CANCEL_GRACE_SECONDS,
    PROVIDER_SIGN_IN_TIMEOUT_SECONDS,
    PROVIDER_SIGN_OUT_TIMEOUT_SECONDS,
)
from rcp.provider_auth import DeviceLogin
from rcp.providers import profile_for
from rcp.runs.provider_login import delete_removed_credential, reconcile_login_removals
from rcp.storage import AppStore
from rcp.storage.models import MACHINE_LOGIN_ID, LoginKey, ProviderLoginStateRecord
from rcp.transport.ssh import ssh_arguments

_LOGGER = logging.getLogger(__name__)

#: What the account says while a member is completing a device sign-in.
SIGN_IN_IN_PROGRESS_DETAIL = "A sign-in is in progress. Finish it in Settings, Sign-ins."
SIGN_IN_CANCELED_DETAIL = "The sign-in was canceled before it completed."
SIGN_IN_INTERRUPTED_DETAIL = "The last sign-in did not finish. Start it again to get a new code."
#: What releases before this one wrote while a device sign-in was running. An
#: account upgraded mid-attempt still carries it, and would otherwise keep
#: describing a sign-in that no process is running.
LEGACY_SIGN_IN_IN_PROGRESS_DETAIL = "A replacement login is awaiting verification."
INTERRUPTED_SIGN_IN_MARKERS = frozenset(
    {SIGN_IN_IN_PROGRESS_DETAIL, LEGACY_SIGN_IN_IN_PROGRESS_DETAIL}
)


class ProviderLoginRefused(ValueError):
    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class SignInCanceled(ProviderLoginRefused):
    """A member ended a sign-in before it completed; the record names that member."""

    def __init__(self, member_id: str | None) -> None:
        super().__init__(SIGN_IN_CANCELED_DETAIL)
        self.member_id = member_id


class ProviderSignInStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str
    login_id: str = MACHINE_LOGIN_ID
    provider: str
    host: str
    state: Literal["pending", "succeeded", "failed"]
    user_code: str | None = None
    verification_url: str | None = None
    detail: str | None = None
    started_at: str
    started_by: str
    started_by_display_name: str | None = None
    finished_at: str | None = None
    resumed: dict[str, int] | None = None


class ProviderSignInRunner:
    """Sign in, verify, and sign out accounts, then resume the work they parked.

    The runner and the launcher are built on one `ProviderAccounts`; the runner
    never reaches into the launcher to wire it.
    """

    def __init__(
        self,
        store: AppStore,
        launcher: AgentLauncher,
        accounts: ProviderAccounts,
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        if launcher.accounts is not accounts:
            raise ValueError("the launcher and the sign-in runner must share one ProviderAccounts")
        self.store = store
        self.launcher = launcher
        self.accounts = accounts
        self.credentials = accounts.credentials
        self._popen = popen
        self._lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._sign_ins: dict[str, ProviderSignInStatus] = {}
        self._logins: dict[str, tuple[subprocess.Popen[str], DeviceLogin]] = {}
        self._settled: dict[str, threading.Event] = {}
        self._verifying: set[str] = set()
        self._canceled: dict[str, str | None] = {}
        self._recovered: dict[LoginKey, int] = {}
        self._resume_counts: dict[LoginKey, dict[str, int]] = {}
        self.resume_account: Callable[[LoginKey], dict[str, int]] | None = None
        self.resume_setting: Callable[[str, str, str], dict[str, int]] | None = None

    def require_login(self, key: LoginKey, *, member_id: str) -> ProviderLoginStateRecord:
        """Reject unknown, removed, or another member's login before touching credentials."""
        try:
            state = self.store.provider_login_state(key)
        except ValueError as exc:
            raise ProviderLoginRefused("Unknown provider login.", status_code=404) from exc
        if state.member_id is not None and state.member_id != member_id:
            raise ProviderLoginRefused("Only the owner may change this login.", status_code=403)
        if state.removed_at is not None:
            raise ProviderLoginRefused("This provider login has been removed.", status_code=409)
        return state

    def observe_failure(
        self,
        login: LoginKey,
        *,
        generation: int,
        evidence: str,
        source: Literal["turn", "report", "probe"],
    ) -> bool:
        if not self.accounts.observe_failure(
            login,
            generation=generation,
            evidence=evidence,
            source=source,
        ):
            return False
        if self.store.provider_login_state(login).generation == generation:
            self.launcher.invalidate_readiness(login)
        return True

    def provider_binary(self, provider: str, host: str) -> tuple[str, set[str]]:
        """The executable RCP would launch on this account, and the path its card records.

        The account must be a space machine. The card's recorded provider path is
        used when one exists; otherwise the same discovery readiness uses resolves
        the provider on that machine, so a native install off PATH still signs in.
        """

        card = self.store.space_machine_for(host) if host else None
        if host and card is None:
            raise ProviderLoginRefused("Unknown provider execution host.")
        if card is not None and card.os_account:
            # A host naming no user takes its account from SSH configuration;
            # never touch another account's provider state.
            reached = self.launcher.execution_account(host=host)
            if not reached.reachable:
                raise ProviderLoginRefused(reached.reason or "The machine is unreachable.")
            if reached.os_account != card.os_account:
                raise ProviderLoginRefused(
                    f"{host} reached account {reached.os_account}, not {card.os_account}."
                )
        binary = self.store.provider_binary(provider, host)
        binaries = {binary} if binary else set()
        if binary is None:
            # Read through the module so the suite's discovery seam covers sign-in too.
            try:
                binary = (
                    self.launcher.discover_provider(provider, host=host)
                    if host
                    else launcher_module._discover_local_provider(provider)
                )
            except launcher_module.ProviderDiscoveryUnavailable as exc:
                # An unanswered host or a broken discovery program is not a
                # missing executable; say which, as readiness would.
                raise ProviderLoginRefused(str(exc)) from exc
        if binary is None:
            raise ProviderLoginRefused(f"{profile_for(provider).label} executable was not found.")
        return binary, binaries

    def sign_in_status(self, attempt_id: str) -> ProviderSignInStatus | None:
        with self._lock:
            return self._sign_ins.get(attempt_id)

    def running_sign_in(self, login: LoginKey) -> ProviderSignInStatus | None:
        with self._lock:
            return self._running(login)

    def _running(self, login: LoginKey) -> ProviderSignInStatus | None:
        return next(
            (
                s
                for s in self._sign_ins.values()
                if s.provider == login.provider
                and s.host == login.host
                and s.login_id == login.login_id
                and s.state == "pending"
            ),
            None,
        )

    def resume_counts(self, login: LoginKey) -> dict[str, int]:
        return self._resume_counts.get(login, {"checked": 0})

    def reconcile_recovery(self) -> None:
        """Verified durable generations are replayable recovery input after interruption.

        Recovery uses existing idempotent task claims and episode reconciliation.
        The memory cache avoids repeated scans, but is not the recovery authority.
        """
        reconcile_login_removals(self.store, self.launcher)
        if self.resume_account is None:
            return
        with self._recovery_lock:
            for state in self.store.provider_login_states():
                key = LoginKey(state.provider, state.host, state.login_id)
                if (
                    state.state != "signed_in"
                    or state.source != "verify"
                    or self._recovered.get(key) == state.generation
                ):
                    continue
                try:
                    counts = self.resume_account(key)
                except Exception:
                    _LOGGER.warning(
                        "Verified provider account recovery will retry on the next lifecycle pass."
                    )
                    continue
                self._resume_counts[key] = counts
                self._recovered[key] = state.generation

    def verify(self, login: LoginKey, *, member_id: str) -> ProviderLoginStateRecord:
        self.require_login(login, member_id=member_id)
        binary, binaries = self.provider_binary(login.provider, login.host)
        with self.launcher.credential_gate.hold_blocking(login):
            try:
                state = self._verify_locked(login, binary, binaries, member_id)
            finally:
                self._cleanup_removed_login(login)
        self.reconcile_recovery()
        return state

    def _cleanup_removed_login(self, key: LoginKey) -> None:
        """Discard a credential written by a provider after member removal fenced it."""
        if self.store.provider_login_state(key).removed_at is None:
            return
        # A failed delete is recorded as pending, which reconcile_login_removals retries.
        try:
            cleaned = delete_removed_credential(self.store, key)
        except OSError:
            cleaned = False
        if not cleaned:
            _LOGGER.warning("A removed provider login credential could not be deleted.")

    def _verify_locked(
        self,
        login: LoginKey,
        binary: str,
        binaries: set[str],
        member_id: str,
    ) -> ProviderLoginStateRecord:
        current = self.require_login(login, member_id=member_id)
        profile = profile_for(login.provider)
        auth = profile.authentication
        # Device verification must probe even a signed-out row: a newly accepted
        # device credential has not yet earned signed_in in durable storage.
        if "device_code" not in auth.methods and not auth.credential_available(
            self.credentials, login.host, login.login_id, login_state=current
        ):
            self.store.mark_provider_login_signed_out(
                login,
                member_id=member_id,
                source="restore",
                detail=auth.missing_credential_detail,
            )
            raise ProviderLoginRefused(auth.missing_credential_detail)
        command = profile.login_probe_command(binary)
        if command is None:
            raise ProviderLoginRefused("Verification is not supported by this provider.")
        try:
            auth.prepare_verification(self.credentials, login.host, login.login_id)
        except ValueError as exc:
            raise ProviderLoginRefused(str(exc)) from exc
        environment = self.launcher.process_environment(login)
        current = self.store.provider_login_state(login)
        result = self.launcher._probe(
            login.host,
            command,
            timeout=PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS,
            environment=environment,
        )
        failed_auth = self.observe_failure(
            login,
            generation=current.generation,
            evidence=profile.probe_failure_evidence(result),
            source="probe",
        )
        if failed_auth or not auth.verification_succeeded(result):
            detail = (
                # Where to sign in is the surface's own sentence, not this one.
                "Provider authentication failed; the credential was rejected."
                if failed_auth
                else "The provider could not complete the verification request."
            )
            raise ProviderLoginRefused(detail)
        self.require_login(login, member_id=member_id)
        auth.verified(self.credentials, login.host, login.login_id, now=self.store.now())
        try:
            state = self.store.mark_provider_login_verified(
                login,
                member_id=member_id,
                detail="Authenticated request succeeded.",
            )
        except ValueError as exc:
            raise ProviderLoginRefused(str(exc)) from exc
        self._forget_readiness(login, binaries)
        return state

    def _forget_readiness(self, login: LoginKey, binaries: set[str]) -> None:
        self.store.delete_provider_readiness_snapshots(login)
        self.launcher.invalidate_readiness(login)
        for binary in binaries:
            self.launcher.invalidate_readiness(login, binary=binary)

    def start_sign_in(self, login: LoginKey, *, member_id: str) -> ProviderSignInStatus:
        self.require_login(login, member_id=member_id)
        if "device_code" not in profile_for(login.provider).authentication.methods:
            raise ProviderLoginRefused(
                "Device sign-in is not supported by this provider.", status_code=422
            )
        # Resolved before the lock: it may probe the machine over SSH.
        binary, _ = self.provider_binary(login.provider, login.host)
        member = self.store.space_user(member_id)
        with self._lock:
            running = self._running(login)
            if running is not None:
                name = running.started_by_display_name or running.started_by
                raise ProviderLoginRefused(f"{name} already started a sign-in for this login.")
            status = ProviderSignInStatus(
                attempt_id=str(uuid.uuid4()),
                login_id=login.login_id,
                provider=login.provider,
                host=login.host,
                state="pending",
                started_at=self.store.now(),
                started_by=member_id,
                started_by_display_name=member.display_name if member else member_id,
            )
            self._sign_ins[status.attempt_id] = status
            self._settled[status.attempt_id] = threading.Event()
        threading.Thread(
            target=self._run_sign_in,
            args=(status, binary),
            name="rcp-provider-sign-in",
            daemon=True,
        ).start()
        return status

    def settle_interrupted_sign_ins(self) -> list[ProviderLoginStateRecord]:
        """Retire a fence left by a sign-in this process cannot still be running.

        A sign-in lives in one process. After a restart none is running, so an
        account still advertising one would tell a member to finish something
        that no longer exists. The marker compared here is RCP's own, never a
        provider's wording.
        """

        settled = []
        for state in self.store.provider_login_states():
            if state.state == "signed_out" and state.detail in INTERRUPTED_SIGN_IN_MARKERS:
                settled.append(
                    self.store.mark_provider_login_signed_out(
                        LoginKey(state.provider, state.host, state.login_id),
                        member_id=None,
                        source="restore",
                        detail=SIGN_IN_INTERRUPTED_DETAIL,
                    )
                )
        return settled

    def _take_cancellation(self, attempt_id: str) -> SignInCanceled | None:
        """Consume this sign-in's cancellation, once, naming who asked for it."""

        with self._lock:
            if attempt_id not in self._canceled:
                return None
            return SignInCanceled(self._canceled.pop(attempt_id))

    def cancel_sign_in(
        self, attempt_id: str, *, member_id: str | None = None
    ) -> ProviderSignInStatus:
        """Ask the provider to abandon a running device sign-in, then stop its process.

        Once the provider has accepted the login there is nothing left to
        abandon, so a cancellation arriving during verification is refused
        rather than recorded: the account really is signed in.
        """

        with self._lock:
            status = self._sign_ins.get(attempt_id)
            if status is None:
                raise ProviderLoginRefused("No such sign-in.", status_code=404)
            if status.state != "pending":
                return status
            if attempt_id in self._verifying:
                raise ProviderLoginRefused(
                    "The provider already accepted this sign-in; it is being verified."
                )
            entry = self._logins.get(attempt_id)
            self._canceled[attempt_id] = member_id
            # The code dies with the process; showing it would invite a sign-in
            # that can never be saved.
            status = status.model_copy(update={"user_code": None, "verification_url": None})
            self._sign_ins[attempt_id] = status
        if entry is not None:
            process, login = entry
            _write(process, login.cancel_input())
            grace = threading.Timer(PROVIDER_SIGN_IN_CANCEL_GRACE_SECONDS, _kill, args=(process,))
            grace.daemon = True
            grace.start()
        return status

    def cancel_login_sign_in(self, key: LoginKey) -> None:
        """Finish a pending attempt's credential work before removal takes its gate."""
        with self._lock:
            status = self._running(key)
            if status is None:
                return
            settled = self._settled[status.attempt_id]
        # Completion may have claimed verification since the lookup. In that
        # case cancellation is refused, but removal still waits for settlement.
        with contextlib.suppress(ProviderLoginRefused):
            self.cancel_sign_in(status.attempt_id)
        settled.wait()

    def _device_login(
        self,
        status: ProviderSignInStatus,
        login: DeviceLogin,
        binary: str,
        environment: ProviderProcessEnvironment,
    ) -> None:
        """Run one device sign-in to its protocol's own completion.

        Nothing here reads the provider's prose: the code and link are protocol
        fields, and only the protocol's completion ends the wait.
        """

        canceled = self._take_cancellation(status.attempt_id)
        if canceled:
            raise canceled
        argv = login.command(binary)
        arguments = (
            ssh_arguments(
                status.host,
                AgentLauncher._remote_login_command(argv, prefix=environment.remote_prefix),
            )
            if status.host
            else argv
        )
        process = self._popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=environment.local_env if not status.host else None,
        )
        with self._lock:
            # Registering and consuming settle under one lock: a cancellation
            # that lands between the check above and this line would otherwise
            # find no process to stop and leave one running to the watchdog.
            canceled = (
                SignInCanceled(self._canceled.pop(status.attempt_id))
                if status.attempt_id in self._canceled
                else None
            )
            if canceled is None:
                self._logins[status.attempt_id] = (process, login)
        if canceled:
            _kill(process)
            process.wait()
            raise canceled
        watchdog = threading.Timer(PROVIDER_SIGN_IN_TIMEOUT_SECONDS, _kill, args=(process,))
        watchdog.daemon = True
        watchdog.start()
        failure: str | None = None
        finished = False
        try:
            _write(process, login.initial_input())
            assert process.stdout is not None
            for line in process.stdout:
                step = login.receive_line(line)
                if step.fields:
                    self._update(status.attempt_id, **step.fields)
                if step.send:
                    _write(process, step.send)
                if step.finished:
                    failure, finished = step.failure, True
                    break
        finally:
            watchdog.cancel()
            # The protocol is over; never leave the provider process behind.
            _kill(process)
            returncode = process.wait()
            with self._lock:
                # Retiring the process, consuming a cancellation, and claiming
                # the verification window happen together. A cancellation that
                # landed between them would otherwise be acknowledged to the
                # member and then contradicted by a successful sign-in.
                self._logins.pop(status.attempt_id, None)
                canceled = (
                    SignInCanceled(self._canceled.pop(status.attempt_id))
                    if status.attempt_id in self._canceled
                    else None
                )
                if canceled is None and finished and not failure:
                    self._verifying.add(status.attempt_id)
        if canceled:
            raise canceled
        if not finished:
            # Naming the exit status is what separates "the member walked away"
            # from "this build of the provider has no such command".
            raise ProviderLoginRefused(
                f"The provider ended the sign-in before it completed (exit status {returncode})."
            )
        if failure:
            raise ProviderLoginRefused(failure)

    def _update(self, attempt_id: str, **changes: object) -> None:
        with self._lock:
            self._sign_ins[attempt_id] = self._sign_ins[attempt_id].model_copy(update=changes)

    def _run_sign_in(self, status: ProviderSignInStatus, binary: str) -> None:
        login = LoginKey(status.provider, status.host, status.login_id)
        auth = profile_for(login.provider).authentication
        try:
            with self.launcher.credential_gate.hold_blocking(login):
                # A member who cancels while this thread waits for the gate must
                # stop the attempt, not start one: nothing has been fenced or
                # launched yet, so the account keeps the state it already had.
                canceled = self._take_cancellation(status.attempt_id)
                if canceled:
                    raise canceled
                self.require_login(login, member_id=status.started_by)
                # Native login can replace its credential before verification runs.
                # Persist the fence first so interruption cannot retain old eligibility.
                self.store.mark_provider_login_signed_out(
                    login,
                    member_id=status.started_by,
                    source="sign_out",
                    detail=SIGN_IN_IN_PROGRESS_DETAIL,
                )
                self._forget_readiness(login, set())
                try:
                    environment = self.launcher.process_environment(login)
                    self._device_login(status, auth.device_login(), binary, environment)
                    state = self._verify_locked(login, binary, set(), status.started_by)
                except Exception as exc:
                    # The fence above says a sign-in is running. However this
                    # attempt ended, the account must say so rather than keep
                    # asking a member to finish it. A cancellation names whoever
                    # asked for it, not whoever started the sign-in.
                    detail = (
                        exc.detail
                        if isinstance(exc, ProviderLoginRefused)
                        else "The sign-in could not complete."
                    )
                    # Publishing must not replace the failure being reported.
                    with contextlib.suppress(Exception):
                        self.store.mark_provider_login_signed_out(
                            login,
                            member_id=getattr(exc, "member_id", None) or status.started_by,
                            source="sign_out",
                            detail=detail,
                        )
                    raise
                finally:
                    self._cleanup_removed_login(login)
            self._settled[status.attempt_id].set()
            self.reconcile_recovery()
            self._update(
                status.attempt_id,
                state="succeeded",
                detail=state.detail,
                finished_at=self.store.now(),
                resumed=self.resume_counts(login) if self.resume_account else None,
            )
        except ProviderLoginRefused as exc:
            self._update(
                status.attempt_id, state="failed", detail=exc.detail, finished_at=self.store.now()
            )
        except Exception:
            _LOGGER.warning("Provider sign-in process could not complete.")
            self._update(
                status.attempt_id,
                state="failed",
                detail="The provider sign-in process could not complete.",
                finished_at=self.store.now(),
            )
        finally:
            self._settled[status.attempt_id].set()
            # Nothing outlives the attempt: a stale marker would refuse the next
            # cancellation, and a stale cancellation would end an unrelated one.
            with self._lock:
                self._verifying.discard(status.attempt_id)
                self._canceled.pop(status.attempt_id, None)

    def save_token(
        self, login: LoginKey, token: str, *, member_id: str
    ) -> ProviderLoginStateRecord:
        self.require_login(login, member_id=member_id)
        auth = profile_for(login.provider).authentication
        if "token_entry" not in auth.methods:
            raise ProviderLoginRefused(
                "Token entry is not supported by this provider.", status_code=422
            )
        binary, binaries = self.provider_binary(login.provider, login.host)
        with self.launcher.credential_gate.hold_blocking(login):
            self.require_login(login, member_id=member_id)
            try:
                token = auth.validate_token(token)
            except ValueError as exc:
                raise ProviderLoginRefused(str(exc), status_code=422) from exc
            # Publish the fence before touching a file: interruption cannot leave a
            # new, unverified credential attached to the old signed-in generation.
            self.store.mark_provider_login_signed_out(
                login,
                member_id=member_id,
                source="sign_out",
                detail="A replacement credential is awaiting verification.",
            )
            self._forget_readiness(login, binaries)
            try:
                try:
                    auth.save_token(
                        self.credentials,
                        login.host,
                        login.login_id,
                        token,
                        member_id=member_id,
                        now=self.store.now(),
                    )
                except (OSError, ValueError) as exc:
                    raise ProviderLoginRefused("Could not persist the credential.") from exc
                state = self._verify_locked(login, binary, binaries, member_id)
            except ProviderLoginRefused as exc:
                # The fence above says a credential is awaiting verification;
                # once it is not, the account must say why rather than keep
                # describing an attempt that is over.
                self.store.mark_provider_login_signed_out(
                    login,
                    member_id=member_id,
                    source="sign_out",
                    detail=exc.detail,
                )
                raise
            finally:
                self._cleanup_removed_login(login)
        self.reconcile_recovery()
        return state

    def sign_out(self, login: LoginKey, *, member_id: str) -> ProviderLoginStateRecord:
        self.require_login(login, member_id=member_id)
        if not profile_for(login.provider).authentication.supports_sign_out:
            raise ProviderLoginRefused(
                "Sign-out is not supported by this provider.", status_code=422
            )
        binary, binaries = self.provider_binary(login.provider, login.host)
        with self.launcher.credential_gate.hold_blocking(login):
            self.require_login(login, member_id=member_id)
            detail = "Signed out by a member."
            state = self.store.mark_provider_login_signed_out(
                login,
                member_id=member_id,
                source="sign_out",
                detail=detail,
            )
            self._forget_readiness(login, binaries)
            try:
                command = profile_for(login.provider).authentication.sign_out(
                    self.credentials, login.host, login.login_id, binary
                )
                if command:
                    environment = self.launcher.process_environment(login)
                    result = self.launcher._probe(
                        login.host,
                        command,
                        timeout=PROVIDER_SIGN_OUT_TIMEOUT_SECONDS,
                        environment=environment,
                    )
                    if result.returncode:
                        detail += " The provider could not confirm credential removal."
            except ValueError:
                detail += " The remote credential could not be removed."
            if detail != state.detail:
                state = self.store.mark_provider_login_signed_out(
                    login,
                    member_id=member_id,
                    source="sign_out",
                    detail=detail,
                )
        return state


def _write(process: subprocess.Popen[str], data: bytes) -> None:
    if not data or process.stdin is None:
        return
    try:
        process.stdin.write(data.decode("utf-8"))
        process.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        _LOGGER.warning("A provider sign-in process closed its input early.")


def _kill(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
