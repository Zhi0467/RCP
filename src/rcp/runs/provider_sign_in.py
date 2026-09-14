"""Sign a machine account in to a provider, verify it, and sign it out, from the product.

The execution account's provider login is shared by every member, so any
signed-in member may operate it; every change records who did it. Codex signs
in through its device-code flow: RCP starts `codex login --device-auth` as the
execution account, shows the code and URL, and waits for the CLI to finish.
Claude signs in by saving a long-lived setup token that RCP injects into every
Claude process it starts. In both cases the last step is one authenticated
request; only that verified answer marks the account `signed_in`.

Nothing here prints a credential: not the token, not `auth.json`, not the
provider's output beyond a bounded, whitespace-folded detail.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import uuid
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import AgentLauncher
from rcp.agents import launcher as launcher_module
from rcp.agents.provider_environment import (
    ProviderCredentialStore,
    remote_claude_token_placement_command,
    remote_claude_token_removal_command,
    validate_claude_token,
)
from rcp.config import load_manifest
from rcp.limits import (
    PROVIDER_LOGIN_DETAIL_MAX_CHARS,
    PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS,
    PROVIDER_SIGN_IN_TIMEOUT_SECONDS,
    PROVIDER_SIGN_OUT_TIMEOUT_SECONDS,
    PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS,
)
from rcp.providers import profile_for
from rcp.storage import AppStore
from rcp.storage.models import ProviderLoginStateRecord
from rcp.transport.ssh import ssh_arguments

_LOGGER = logging.getLogger(__name__)
_URL = re.compile(r"https?://[^\s'\"<>]+")
# Device codes are short upper-case groups joined by dashes; a lone group on
# its own line after the CLI announces the code is accepted too.
_CODE = re.compile(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{3,})+\b")
_BARE_CODE = re.compile(r"^[A-Z0-9]{6,12}$")
_TRANSCRIPT_LINES = 20


class ProviderLoginRefused(ValueError):
    """The sign-in, verify, or sign-out could not be completed; `detail` is safe to show."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class ProviderSignInStatus(BaseModel):
    """One device-code sign-in as the human sees it while it runs."""

    model_config = ConfigDict(extra="forbid")

    login_id: str
    provider: Literal["codex"]
    host: str
    state: Literal["pending", "succeeded", "failed"]
    user_code: str | None = None
    verification_url: str | None = None
    detail: str | None = None
    started_at: str
    started_by: str
    finished_at: str | None = None
    #: Parked work Verify resumed after success; filled by the route that
    #: observes the success, since resuming needs the request's services.
    resumed: dict[str, int] | None = None


def bounded_detail(text: str) -> str:
    return " ".join(text.split())[:PROVIDER_LOGIN_DETAIL_MAX_CHARS]


def reset_claude_logins_without_tokens(
    store: AppStore, credentials: ProviderCredentialStore
) -> list[ProviderLoginStateRecord]:
    """Sign out every Claude account recorded as signed in whose token is not here.

    The token directory is excluded from protected backups, so a restored
    server carries the login state but not the credential. Marking the account
    signed out before any launch keeps the fence honest; a member signs in again.
    """

    reset: list[ProviderLoginStateRecord] = []
    for state in store.provider_login_states():
        if state.provider != "claude" or state.state != "signed_in":
            continue
        if credentials.claude_token_record(state.host) is not None:
            continue
        reset.append(
            store.mark_provider_login_signed_out(
                "claude",
                state.host,
                member_id=None,
                source="restore",
                detail=(
                    "The Claude setup token is not on this machine, so the restored login "
                    "state was reset. Save a setup token to sign in again."
                ),
            )
        )
    return reset


class ProviderSignInRunner:
    """Sign-in, verify, and sign-out for one RCP process; one sign-in per account at a time."""

    def __init__(
        self,
        store: AppStore,
        launcher: AgentLauncher,
        credentials: ProviderCredentialStore,
        *,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.store = store
        self.launcher = launcher
        self.credentials = credentials
        self._popen = popen
        self._lock = threading.Lock()
        self._sign_ins: dict[str, ProviderSignInStatus] = {}

    # -- lookup -------------------------------------------------------------

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
            for status in self._sign_ins.values():
                if (
                    status.provider == provider
                    and status.host == host
                    and status.state == "pending"
                ):
                    return status
        return None

    def record_resumed(self, login_id: str, counts: dict[str, int]) -> ProviderSignInStatus | None:
        with self._lock:
            status = self._sign_ins.get(login_id)
            if status is not None and status.resumed is None:
                status = status.model_copy(update={"resumed": counts})
                self._sign_ins[login_id] = status
            return status

    # -- verify -------------------------------------------------------------

    def verify(self, provider: str, host: str, *, member_id: str) -> ProviderLoginStateRecord:
        """One real authenticated request; its answer, not a status command, is the proof."""

        profile = profile_for(provider)
        binary, binaries = self.provider_binary(provider, host)
        command = profile.login_probe_command(binary)
        if command is None:
            raise ProviderLoginRefused("no probe defined")
        environment = self.launcher.process_environment(provider, host)
        with self.launcher.credential_gate.hold_blocking(provider, host):
            current = self.store.provider_login_state(provider, host)
            result = self.launcher._probe(
                host,
                command,
                timeout=PROVIDER_LOGIN_VERIFY_TIMEOUT_SECONDS,
                environment=environment,
            )
            if result.returncode != 0 or not result.stdout.strip():
                detail = bounded_detail(
                    result.stderr or "The provider returned no authenticated answer."
                )
                if current.state == "signed_out" or profile.credential_failure(
                    result.stderr + result.stdout
                ):
                    current = self.store.mark_provider_login_failed(
                        provider,
                        host,
                        generation=current.generation,
                        detail=detail,
                        source="probe",
                    )
                    detail = current.detail or detail
                raise ProviderLoginRefused(detail)
            state = self.store.mark_provider_login_verified(
                provider, host, member_id=member_id, detail="Authenticated request succeeded."
            )
            if provider == "claude":
                self.credentials.mark_claude_token_verified(host, now=self.store.now())
            self._forget_readiness(provider, host, binaries)
        return state

    def _forget_readiness(self, provider: str, host: str, binaries: set[str]) -> None:
        # The stored probe answers described the account before this change;
        # the next readiness read probes again under the gate.
        self.store.delete_provider_readiness_snapshots(provider, host)
        self.launcher.invalidate_readiness(provider, host=host)
        for configured in binaries:
            self.launcher.invalidate_readiness(provider, host=host, binary=configured)

    # -- Codex device-code sign-in -------------------------------------------

    def start_codex_sign_in(self, host: str, *, member_id: str) -> ProviderSignInStatus:
        running = self.running_sign_in("codex", host)
        if running is not None:
            return running
        binary, _ = self.provider_binary("codex", host)
        status = ProviderSignInStatus(
            login_id=str(uuid.uuid4()),
            provider="codex",
            host=host,
            state="pending",
            started_at=self.store.now(),
            started_by=member_id,
        )
        with self._lock:
            self._sign_ins[status.login_id] = status
        threading.Thread(
            target=self._run_codex_sign_in,
            args=(status.login_id, host, binary, member_id),
            name="rcp-codex-sign-in",
            daemon=True,
        ).start()
        return status

    def _update(self, login_id: str, **changes: object) -> None:
        with self._lock:
            self._sign_ins[login_id] = self._sign_ins[login_id].model_copy(update=changes)

    def _run_codex_sign_in(self, login_id: str, host: str, binary: str, member_id: str) -> None:
        argv = [binary, "login", "--device-auth"]
        environment = self.launcher.process_environment("codex", host)
        arguments = argv
        if host:
            arguments = ssh_arguments(
                host, AgentLauncher._remote_login_command(argv, prefix=environment.remote_prefix)
            )
        transcript: list[str] = []
        try:
            # The login rewrites the credential file when it completes; holding the
            # gate keeps a turn from starting on the file while that happens.
            with self.launcher.credential_gate.hold_blocking("codex", host):
                process = self._popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=environment.local_env if not host else None,
                )
                # A human who never finishes must not hold the credential forever.
                # By the time this fires the process is far past the startup hold.
                watchdog = threading.Timer(PROVIDER_SIGN_IN_TIMEOUT_SECONDS, _kill, args=(process,))
                watchdog.daemon = True
                watchdog.start()
                try:
                    assert process.stdout is not None
                    for line in process.stdout:
                        line = line.rstrip()
                        if not line:
                            continue
                        transcript.append(line)
                        del transcript[:-_TRANSCRIPT_LINES]
                        self._absorb_sign_in_line(login_id, line)
                    returncode = process.wait()
                finally:
                    watchdog.cancel()
            if returncode != 0:
                raise ProviderLoginRefused(
                    bounded_detail(" ".join(transcript[-3:]) or f"codex login exited {returncode}.")
                )
            state = self.verify("codex", host, member_id=member_id)
            self._update(
                login_id,
                state="succeeded",
                detail=state.detail,
                finished_at=self.store.now(),
            )
        except ProviderLoginRefused as exc:
            self._update(login_id, state="failed", detail=exc.detail, finished_at=self.store.now())
        except Exception as exc:  # a transport or process error; never a credential
            _LOGGER.warning("Codex sign-in for %s failed: %s", host or "local", exc)
            self._update(
                login_id,
                state="failed",
                detail=bounded_detail(str(exc)),
                finished_at=self.store.now(),
            )

    def _absorb_sign_in_line(self, login_id: str, line: str) -> None:
        with self._lock:
            status = self._sign_ins[login_id]
        changes: dict[str, object] = {}
        if status.verification_url is None and (match := _URL.search(line)):
            changes["verification_url"] = match.group(0).rstrip(".,")
        if status.user_code is None:
            code = _CODE.search(line)
            if code is not None:
                changes["user_code"] = code.group(0)
            elif _BARE_CODE.fullmatch(line.strip()):
                changes["user_code"] = line.strip()
        if changes:
            self._update(login_id, **changes)

    # -- Claude setup token ---------------------------------------------------

    def save_claude_token(
        self, host: str, token: str, *, member_id: str
    ) -> ProviderLoginStateRecord:
        try:
            token = validate_claude_token(token)
        except ValueError as exc:
            raise ProviderLoginRefused(str(exc), status_code=422) from exc
        # Refuse an unknown host before anything is written anywhere.
        _, binaries = self.provider_binary("claude", host)
        self.credentials.store_claude_token(host, token, member_id=member_id, now=self.store.now())
        if host:
            self._place_remote_claude_token(host, token)
        self._forget_readiness("claude", host, binaries)
        return self.verify("claude", host, member_id=member_id)

    def _place_remote_claude_token(self, host: str, token: str) -> None:
        # The token travels on stdin of the SSH session, never in an argument.
        try:
            result = subprocess.run(
                ssh_arguments(host, remote_claude_token_placement_command()),
                input=token + "\n",
                capture_output=True,
                text=True,
                timeout=PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderLoginRefused(
                bounded_detail(f"Could not place the token on {host}: {exc}")
            ) from exc
        if result.returncode != 0:
            raise ProviderLoginRefused(
                bounded_detail(
                    f"Could not place the token on {host}: "
                    + (result.stderr.strip() or f"exit {result.returncode}")
                )
            )

    # -- sign-out -------------------------------------------------------------

    def sign_out(self, provider: str, host: str, *, member_id: str) -> ProviderLoginStateRecord:
        """Revoke the account's login and fence launches exactly as a failed login does."""

        binary, binaries = self.provider_binary(provider, host)
        notes: list[str] = []
        if provider == "codex":
            environment = self.launcher.process_environment(provider, host)
            with self.launcher.credential_gate.hold_blocking(provider, host):
                result = self.launcher._probe(
                    host,
                    [binary, "logout"],
                    timeout=PROVIDER_SIGN_OUT_TIMEOUT_SECONDS,
                    environment=environment,
                )
            if result.returncode != 0:
                notes.append(
                    "codex logout reported: "
                    + bounded_detail(result.stderr or result.stdout or f"exit {result.returncode}")
                )
        else:
            self.credentials.delete_claude_token(host)
            if host:
                try:
                    removed = subprocess.run(
                        ssh_arguments(host, remote_claude_token_removal_command()),
                        capture_output=True,
                        text=True,
                        timeout=PROVIDER_SIGN_OUT_TIMEOUT_SECONDS,
                        check=False,
                    )
                    if removed.returncode != 0:
                        notes.append(
                            f"The token file on {host} could not be removed: "
                            + bounded_detail(removed.stderr or f"exit {removed.returncode}")
                        )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    notes.append(f"The token file on {host} could not be removed: {exc}")
        detail = "Signed out by a member." + (" " + " ".join(notes) if notes else "")
        state = self.store.mark_provider_login_signed_out(
            provider, host, member_id=member_id, source="sign_out", detail=detail
        )
        self._forget_readiness(provider, host, binaries)
        return state


def _kill(process: subprocess.Popen[str]) -> None:
    # The device code has expired on the provider's side by now as well.
    if process.poll() is None:
        process.kill()


__all__ = [
    "ProviderLoginRefused",
    "ProviderSignInRunner",
    "ProviderSignInStatus",
    "bounded_detail",
    "reset_claude_logins_without_tokens",
]
