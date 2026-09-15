"""Shared account lifecycle: serialize credentials, publish facts, resume existing work."""

from __future__ import annotations

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
from rcp.config import load_manifest
from rcp.limits import (
    PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS,
    PROVIDER_SIGN_IN_CANCEL_GRACE_SECONDS,
    PROVIDER_SIGN_IN_TIMEOUT_SECONDS,
    PROVIDER_SIGN_OUT_TIMEOUT_SECONDS,
)
from rcp.provider_auth import DeviceLogin
from rcp.providers import profile_for
from rcp.storage import AppStore
from rcp.storage.models import ProviderLoginStateRecord
from rcp.transport.ssh import ssh_arguments

_LOGGER = logging.getLogger(__name__)

#: What the account says while a member is completing a device sign-in.
SIGN_IN_IN_PROGRESS_DETAIL = "A sign-in is in progress. Finish it in Settings, Provider logins."
SIGN_IN_CANCELED_DETAIL = "The sign-in was canceled before it completed."


class ProviderLoginRefused(ValueError):
    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class ProviderSignInStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login_id: str
    provider: str
    host: str
    state: Literal["pending", "succeeded", "failed"]
    user_code: str | None = None
    verification_url: str | None = None
    detail: str | None = None
    started_at: str
    started_by: str
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
        self._canceled: set[str] = set()
        self._recovered: dict[tuple[str, str], int] = {}
        self._resume_counts: dict[tuple[str, str], dict[str, int]] = {}
        self.resume_account: Callable[[str, str], dict[str, int]] | None = None

    def refusal(self, provider: str, host: str) -> str | None:
        return self.accounts.refusal(provider, host)

    def account_state(self, provider: str, host: str) -> ProviderLoginStateRecord:
        return self.accounts.account_state(provider, host)

    def observe_failure(
        self,
        provider: str,
        host: str,
        *,
        generation: int,
        evidence: str,
        source: Literal["turn", "report", "probe"],
    ) -> bool:
        if not self.accounts.observe_failure(
            provider, host, generation=generation, evidence=evidence, source=source
        ):
            return False
        if self.store.provider_login_state(provider, host).generation == generation:
            self.launcher.invalidate_readiness(provider, host=host)
        return True

    def provider_binary(self, provider: str, host: str) -> tuple[str, set[str]]:
        """The executable RCP would launch on this account, and every path a manifest saved."""

        machines = []
        for project in self.store.projects():
            try:
                manifest = load_manifest(project.locator)
            except (OSError, ValueError):
                _LOGGER.warning("Provider login skipped an unavailable project manifest.")
                continue
            machines.extend(machine for machine in manifest.machines if machine.host == host)
        if host and not machines:
            raise ProviderLoginRefused("Unknown provider execution host.")
        binaries = {
            machine.provider_paths[provider]
            for machine in machines
            if provider in machine.provider_paths
        }
        if len(binaries) > 1:
            raise ProviderLoginRefused("Provider paths disagree for this account.")
        binary = next(iter(binaries), None)
        if binary is None:
            # Read through the module so the suite's discovery seam covers sign-in too.
            binary = provider if host else launcher_module._discover_local_provider(provider)
        if binary is None:
            raise ProviderLoginRefused(f"{profile_for(provider).label} executable was not found.")
        return binary, binaries

    def sign_in_status(self, login_id: str) -> ProviderSignInStatus | None:
        with self._lock:
            return self._sign_ins.get(login_id)

    def running_sign_in(self, provider: str, host: str) -> ProviderSignInStatus | None:
        with self._lock:
            return self._running(provider, host)

    def _running(self, provider: str, host: str) -> ProviderSignInStatus | None:
        return next(
            (
                s
                for s in self._sign_ins.values()
                if s.provider == provider and s.host == host and s.state == "pending"
            ),
            None,
        )

    def resume_counts(self, provider: str, host: str) -> dict[str, int]:
        return self._resume_counts.get((provider, host), {"checked": 0})

    def reconcile_recovery(self) -> None:
        """Verified durable generations are replayable recovery input after interruption.

        Recovery uses existing idempotent task claims and episode reconciliation.
        The memory cache avoids repeated scans, but is not the recovery authority.
        """
        if self.resume_account is None:
            return
        with self._recovery_lock:
            for state in self.store.provider_login_states():
                key = (state.provider, state.host)
                if (
                    state.state != "signed_in"
                    or state.source != "verify"
                    or self._recovered.get(key) == state.generation
                ):
                    continue
                try:
                    counts = self.resume_account(*key)
                except Exception:
                    _LOGGER.warning(
                        "Verified provider account recovery will retry on the next lifecycle pass."
                    )
                    continue
                self._resume_counts[key] = counts
                self._recovered[key] = state.generation

    def verify(self, provider: str, host: str, *, member_id: str) -> ProviderLoginStateRecord:
        binary, binaries = self.provider_binary(provider, host)
        with self.launcher.credential_gate.hold_blocking(provider, host):
            state = self._verify_locked(provider, host, binary, binaries, member_id)
        self.reconcile_recovery()
        return state

    def _verify_locked(
        self, provider: str, host: str, binary: str, binaries: set[str], member_id: str
    ) -> ProviderLoginStateRecord:
        profile = profile_for(provider)
        auth = profile.authentication
        if not auth.credential_available(self.credentials, host):
            self.store.mark_provider_login_signed_out(
                provider,
                host,
                member_id=member_id,
                source="restore",
                detail=auth.missing_credential_detail,
            )
            raise ProviderLoginRefused(auth.missing_credential_detail)
        command = profile.login_probe_command(binary)
        if command is None:
            raise ProviderLoginRefused("Verification is not supported by this provider.")
        try:
            auth.prepare_verification(self.credentials, host)
        except ValueError as exc:
            raise ProviderLoginRefused(str(exc)) from exc
        environment = self.launcher.process_environment(provider, host)
        current = self.store.provider_login_state(provider, host)
        result = self.launcher._probe(
            host, command, timeout=PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS, environment=environment
        )
        failed_auth = self.observe_failure(
            provider,
            host,
            generation=current.generation,
            evidence=profile.probe_failure_evidence(result),
            source="probe",
        )
        if failed_auth or not auth.verification_succeeded(result):
            detail = (
                "Provider authentication failed. Sign in again in Settings."
                if failed_auth
                else "The provider could not complete the verification request."
            )
            raise ProviderLoginRefused(detail)
        auth.verified(self.credentials, host, now=self.store.now())
        state = self.store.mark_provider_login_verified(
            provider, host, member_id=member_id, detail="Authenticated request succeeded."
        )
        self._forget_readiness(provider, host, binaries)
        return state

    def _forget_readiness(self, provider: str, host: str, binaries: set[str]) -> None:
        self.store.delete_provider_readiness_snapshots(provider, host)
        self.launcher.invalidate_readiness(provider, host=host)
        for binary in binaries:
            self.launcher.invalidate_readiness(provider, host=host, binary=binary)

    def start_sign_in(self, provider: str, host: str, *, member_id: str) -> ProviderSignInStatus:
        if "device_code" not in profile_for(provider).authentication.methods:
            raise ProviderLoginRefused(
                "Device sign-in is not supported by this provider.", status_code=422
            )
        with self._lock:
            running = self._running(provider, host)
            if running is not None:
                return running
            binary, _ = self.provider_binary(provider, host)
            status = ProviderSignInStatus(
                login_id=str(uuid.uuid4()),
                provider=provider,
                host=host,
                state="pending",
                started_at=self.store.now(),
                started_by=member_id,
            )
            self._sign_ins[status.login_id] = status
        threading.Thread(
            target=self._run_sign_in,
            args=(status, binary),
            name="rcp-provider-sign-in",
            daemon=True,
        ).start()
        return status

    def cancel_sign_in(self, login_id: str) -> ProviderSignInStatus:
        """Ask the provider to abandon a running device sign-in, then stop its process."""

        with self._lock:
            status = self._sign_ins.get(login_id)
            if status is None:
                raise ProviderLoginRefused("No such sign-in.", status_code=404)
            if status.state != "pending":
                return status
            entry = self._logins.get(login_id)
            self._canceled.add(login_id)
        if entry is not None:
            process, login = entry
            _write(process, login.cancel_input())
            grace = threading.Timer(PROVIDER_SIGN_IN_CANCEL_GRACE_SECONDS, _kill, args=(process,))
            grace.daemon = True
            grace.start()
        return status

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
            self._logins[status.login_id] = (process, login)
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
                    self._update(status.login_id, **step.fields)
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
                self._logins.pop(status.login_id, None)
                canceled = status.login_id in self._canceled
                self._canceled.discard(status.login_id)
        if canceled:
            raise ProviderLoginRefused(SIGN_IN_CANCELED_DETAIL)
        if not finished:
            # Naming the exit status is what separates "the member walked away"
            # from "this build of the provider has no such command".
            raise ProviderLoginRefused(
                f"The provider ended the sign-in before it completed (exit status {returncode})."
            )
        if failure:
            raise ProviderLoginRefused(failure)

    def _update(self, login_id: str, **changes: object) -> None:
        with self._lock:
            self._sign_ins[login_id] = self._sign_ins[login_id].model_copy(update=changes)

    def _run_sign_in(self, status: ProviderSignInStatus, binary: str) -> None:
        provider, host = status.provider, status.host
        auth = profile_for(provider).authentication
        try:
            with self.launcher.credential_gate.hold_blocking(provider, host):
                # Native login can replace its credential before verification runs.
                # Persist the fence first so interruption cannot retain old eligibility.
                self.store.mark_provider_login_signed_out(
                    provider,
                    host,
                    member_id=status.started_by,
                    source="sign_out",
                    detail=SIGN_IN_IN_PROGRESS_DETAIL,
                )
                self._forget_readiness(provider, host, set())
                environment = self.launcher.process_environment(provider, host)
                try:
                    self._device_login(status, auth.device_login(), binary, environment)
                    state = self._verify_locked(provider, host, binary, set(), status.started_by)
                except ProviderLoginRefused as exc:
                    # The fence above says a sign-in is running; once it is not,
                    # the account must say why, not keep describing the attempt.
                    self.store.mark_provider_login_signed_out(
                        provider,
                        host,
                        member_id=status.started_by,
                        source="sign_out",
                        detail=exc.detail,
                    )
                    raise
            self.reconcile_recovery()
            self._update(
                status.login_id,
                state="succeeded",
                detail=state.detail,
                finished_at=self.store.now(),
                resumed=self.resume_counts(provider, host) if self.resume_account else None,
            )
        except ProviderLoginRefused as exc:
            self._update(
                status.login_id, state="failed", detail=exc.detail, finished_at=self.store.now()
            )
        except Exception:
            _LOGGER.warning("Provider sign-in process could not complete.")
            self._update(
                status.login_id,
                state="failed",
                detail="The provider sign-in process could not complete.",
                finished_at=self.store.now(),
            )

    def save_token(
        self, provider: str, host: str, token: str, *, member_id: str
    ) -> ProviderLoginStateRecord:
        auth = profile_for(provider).authentication
        if "token_entry" not in auth.methods:
            raise ProviderLoginRefused(
                "Token entry is not supported by this provider.", status_code=422
            )
        binary, binaries = self.provider_binary(provider, host)
        with self.launcher.credential_gate.hold_blocking(provider, host):
            try:
                token = auth.validate_token(token)
            except ValueError as exc:
                raise ProviderLoginRefused(str(exc), status_code=422) from exc
            # Publish the fence before touching a file: interruption cannot leave a
            # new, unverified credential attached to the old signed-in generation.
            self.store.mark_provider_login_signed_out(
                provider,
                host,
                member_id=member_id,
                source="sign_out",
                detail="A replacement credential is awaiting verification.",
            )
            self._forget_readiness(provider, host, binaries)
            try:
                auth.save_token(
                    self.credentials, host, token, member_id=member_id, now=self.store.now()
                )
            except (OSError, ValueError) as exc:
                raise ProviderLoginRefused("Could not persist the credential.") from exc
            state = self._verify_locked(provider, host, binary, binaries, member_id)
        self.reconcile_recovery()
        return state

    def sign_out(self, provider: str, host: str, *, member_id: str) -> ProviderLoginStateRecord:
        if not profile_for(provider).authentication.supports_sign_out:
            raise ProviderLoginRefused(
                "Sign-out is not supported by this provider.", status_code=422
            )
        binary, binaries = self.provider_binary(provider, host)
        with self.launcher.credential_gate.hold_blocking(provider, host):
            detail = "Signed out by a member."
            state = self.store.mark_provider_login_signed_out(
                provider, host, member_id=member_id, source="sign_out", detail=detail
            )
            self._forget_readiness(provider, host, binaries)
            try:
                command = profile_for(provider).authentication.sign_out(
                    self.credentials, host, binary
                )
                if command:
                    environment = self.launcher.process_environment(provider, host)
                    result = self.launcher._probe(
                        host,
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
                    provider, host, member_id=member_id, source="sign_out", detail=detail
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
