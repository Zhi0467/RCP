"""Provider-owned authentication mechanics; no task or episode authority."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents.provider_environment import ProviderProcessEnvironment
from rcp.limits import (
    PROVIDER_CLAUDE_TOKEN_ESTIMATED_LIFETIME_DAYS,
    PROVIDER_TOKEN_MAX_CHARS,
    PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS,
)
from rcp.transport.ssh import ssh_arguments

if TYPE_CHECKING:
    from rcp.agents.provider_environment import ProviderCredentialStore


CLAUDE_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFLICTING_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: Where a remote execution account keeps the token RCP placed for it.
REMOTE_CLAUDE_TOKEN_PATH = "~/.config/rcp/claude-setup-token"


def validate_claude_token(token: str) -> str:
    """Accept only a token-shaped string; the shape is all RCP can check."""

    if not token or token != token.strip() or any(ch.isspace() for ch in token):
        raise ValueError("A setup token has no spaces or line breaks.")
    if len(token) > PROVIDER_TOKEN_MAX_CHARS:
        raise ValueError("The pasted value is longer than any setup token.")
    return token


def remote_claude_token_prefix(path: str = REMOTE_CLAUDE_TOKEN_PATH) -> str:
    """The shell fragment that exports the token from the remote account's file.

    The path is quoted so `~` still expands; the token itself never enters the
    command line. A missing or empty file refuses the process rather than
    falling back to credentials inherited from the remote login shell.
    """

    unset = " ".join(CLAUDE_CONFLICTING_VARIABLES)
    quoted = _quote_home_relative(path)
    return (
        f"unset {unset} {CLAUDE_TOKEN_VARIABLE}; "
        f'if [ -s {quoted} ]; then export {CLAUDE_TOKEN_VARIABLE}="$(cat {quoted})"; '
        "else printf '%s\\n' 'RCP managed credential is missing' >&2; exit 1; fi"
    )


def remote_claude_token_placement_command(path: str = REMOTE_CLAUDE_TOKEN_PATH) -> str:
    """Write the token read from stdin to the remote account, mode 0600 in a 0700 directory."""

    quoted = _quote_home_relative(path)
    directory = _quote_home_relative(str(Path(path).parent))
    temporary = quoted + ".tmp"
    return (
        f"umask 077 && mkdir -p {directory} && cat > {temporary} "
        f"&& chmod 600 {temporary} && mv -f {temporary} {quoted}"
    )


def remote_claude_token_removal_command(path: str = REMOTE_CLAUDE_TOKEN_PATH) -> str:
    return f"rm -f {_quote_home_relative(path)}"


def _quote_home_relative(path: str) -> str:
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


@dataclass(frozen=True)
class DeviceLoginStep:
    """What one line of a provider's device-login protocol means to RCP.

    `fields` carry the code and link a human needs, read from the provider's own
    structured reply rather than from prose it prints. `send` is the next frame
    to write. A step is terminal when `finished` is set; `failure` then holds the
    provider's own explanation, which RCP shows but never interprets.
    """

    fields: dict[str, str] = field(default_factory=dict)
    send: bytes | None = None
    finished: bool = False
    failure: str | None = None


class DeviceLogin:
    """One provider's device sign-in, driven over that provider's structured protocol."""

    def command(self, binary: str) -> list[str]:
        raise NotImplementedError

    def initial_input(self) -> bytes:
        return b""

    def receive_line(self, line: str) -> DeviceLoginStep:
        raise NotImplementedError

    def cancel_input(self) -> bytes:
        return b""


class CodexDeviceLogin(DeviceLogin):
    """Codex device sign-in over the app-server JSON protocol.

    Every fact RCP acts on is a protocol field: `userCode` and `verificationUrl`
    from the `account/login/start` reply, then `success` on the
    `account/login/completed` notification. Codex may reword its console output
    or its error strings without changing what RCP reads.
    """

    INITIALIZE_ID = 1
    START_ID = 2
    CANCEL_ID = 3

    def __init__(self) -> None:
        self._login_id: str | None = None

    def command(self, binary: str) -> list[str]:
        return [binary, "app-server"]

    def initial_input(self) -> bytes:
        return _rpc_bytes(
            {
                "id": self.INITIALIZE_ID,
                "method": "initialize",
                "params": {"clientInfo": {"name": "rcp", "title": "RCP", "version": "1"}},
            }
        )

    def receive_line(self, line: str) -> DeviceLoginStep:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return DeviceLoginStep()
        if not isinstance(value, dict):
            return DeviceLoginStep()
        if value.get("id") == self.INITIALIZE_ID and "error" in value:
            # The provider answered but refused to start. It may hold the
            # connection open, so end here instead of waiting for a reply that
            # will never come.
            return DeviceLoginStep(
                finished=True,
                failure=_protocol_error_text(value.get("error"))
                or "The provider refused to start a sign-in session.",
            )
        if value.get("id") == self.INITIALIZE_ID and "result" in value:
            return DeviceLoginStep(
                send=_rpc_bytes({"method": "initialized", "params": {}})
                + _rpc_bytes(
                    {
                        "id": self.START_ID,
                        "method": "account/login/start",
                        "params": {"type": "chatgptDeviceCode"},
                    }
                )
            )
        if value.get("id") == self.START_ID:
            return self._started(value)
        if value.get("method") == "account/login/completed":
            return self._completed(value.get("params"))
        if "id" in value and "method" in value:
            # A request from the provider has no unattended answer; end the attempt.
            return DeviceLoginStep(
                finished=True, failure="The provider asked for input RCP cannot answer."
            )
        return DeviceLoginStep()

    def _started(self, value: dict[str, object]) -> DeviceLoginStep:
        if "error" in value:
            return DeviceLoginStep(finished=True, failure=_protocol_error_text(value.get("error")))
        result = value.get("result")
        if not isinstance(result, dict):
            return DeviceLoginStep(finished=True, failure="The provider started no device login.")
        self._login_id = str(result.get("loginId") or "") or None
        fields = {}
        code = result.get("userCode")
        url = result.get("verificationUrl")
        if isinstance(code, str) and code:
            fields["user_code"] = code
        if isinstance(url, str) and url:
            fields["verification_url"] = url
        if len(fields) < 2:
            return DeviceLoginStep(
                finished=True, failure="The provider returned no device code to enter."
            )
        return DeviceLoginStep(fields=fields)

    def _completed(self, params: object) -> DeviceLoginStep:
        if not isinstance(params, dict):
            return DeviceLoginStep()
        if self._login_id and params.get("loginId") not in (None, self._login_id):
            return DeviceLoginStep()
        if params.get("success") is True:
            return DeviceLoginStep(finished=True)
        # A refusal the provider did not explain is still a refusal: without a
        # failure here the caller would go on to verify, and a still-valid old
        # credential would report the replacement login as signed in.
        return DeviceLoginStep(
            finished=True,
            failure=_protocol_error_text(params.get("error"))
            or "The provider reported the sign-in as unsuccessful.",
        )

    def cancel_input(self) -> bytes:
        if not self._login_id:
            return b""
        return _rpc_bytes(
            {
                "id": self.CANCEL_ID,
                "method": "account/login/cancel",
                "params": {"loginId": self._login_id},
            }
        )


def _protocol_error_text(value: object) -> str:
    """The provider's own words for a failure; displayed, never matched on."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return message
    return ""


def _rpc_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")


class ProviderAuthentication:
    supports_sign_out = False
    methods: tuple[str, ...] = ()
    token_instructions: str | None = None
    missing_credential_detail = "No managed credential is saved."

    def credential_available(self, credentials: ProviderCredentialStore, host: str) -> bool:
        return True

    def credential_metadata(self, credentials: ProviderCredentialStore, host: str) -> dict | None:
        return None

    def process_environment(
        self, credentials: ProviderCredentialStore, host: str
    ) -> ProviderProcessEnvironment:
        return ProviderProcessEnvironment()

    def device_login(self) -> DeviceLogin:
        raise ValueError("Device sign-in is not supported by this provider.")

    def validate_token(self, token: str) -> str:
        raise ValueError("Token entry is not supported by this provider.")

    def save_token(
        self,
        credentials: ProviderCredentialStore,
        host: str,
        token: str,
        *,
        member_id: str,
        now: str,
    ) -> None:
        raise ValueError("Token entry is not supported by this provider.")

    def prepare_verification(self, credentials: ProviderCredentialStore, host: str) -> None:
        pass

    def verified(self, credentials: ProviderCredentialStore, host: str, *, now: str) -> None:
        pass

    def sign_out(
        self, credentials: ProviderCredentialStore, host: str, binary: str
    ) -> list[str] | None:
        raise ValueError("Sign-out is not supported by this provider.")

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        return result.returncode == 0 and bool(result.stdout.strip())


class CodexAuthentication(ProviderAuthentication):
    supports_sign_out = True
    methods = ("device_code",)

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        from rcp.providers import CodexProfile

        output = subprocess.CompletedProcess(result.args, result.returncode, result.stdout, "")
        return (
            super().verification_succeeded(result)
            and not CodexProfile().probe_failure_evidence(output).strip()
        )

    def device_login(self) -> DeviceLogin:
        return CodexDeviceLogin()

    def sign_out(self, credentials: ProviderCredentialStore, host: str, binary: str) -> list[str]:
        return [binary, "logout"]


class ClaudeAuthentication(ProviderAuthentication):
    supports_sign_out = True
    credential_namespace = "claude"
    methods = ("token_entry",)
    # A member cannot act on "run claude setup-token" alone: the command says
    # nothing about which machine to run it on, which account it mints for, or
    # that Claude's other sign-in is the one that breaks a shared account.
    token_instructions = (
        "Run claude setup-token on any machine with a browser, signed in as the Claude "
        "account this login should use. The token is not tied to the machine that made "
        "it, so paste it here. Do not use claude auth login for an account other people "
        "share: that credential rotates on every process start, and one lost write signs "
        "out every member."
    )
    # Where to go is the surface's sentence, not this one; saying it twice made
    # the signed-out notice read as two instructions.
    missing_credential_detail = "No Claude setup token is saved."

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        from rcp.providers import ClaudeProfile

        output = subprocess.CompletedProcess(result.args, result.returncode, result.stdout, "")
        return (
            super().verification_succeeded(result)
            and not ClaudeProfile().probe_failure_evidence(output).strip()
        )

    def credential_available(self, credentials: ProviderCredentialStore, host: str) -> bool:
        return credentials.token(self.credential_namespace, host) is not None

    def credential_metadata(self, credentials: ProviderCredentialStore, host: str) -> dict | None:
        record = credentials.token_record(self.credential_namespace, host)
        if record is None or not self.credential_available(credentials, host):
            return None
        metadata = record.model_dump()
        try:
            pasted = datetime.fromisoformat(record.pasted_at)
            metadata["estimated_expiry_at"] = (
                pasted + timedelta(days=PROVIDER_CLAUDE_TOKEN_ESTIMATED_LIFETIME_DAYS)
            ).isoformat()
        except ValueError:
            metadata["estimated_expiry_at"] = None
        return metadata

    def process_environment(
        self, credentials: ProviderCredentialStore, host: str
    ) -> ProviderProcessEnvironment:
        if host:
            return ProviderProcessEnvironment(
                remote_prefix=remote_claude_token_prefix()
                if self.credential_available(credentials, host)
                else None
            ).with_claude_foreground_tasks(remote=True)
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in CLAUDE_CONFLICTING_VARIABLES
        }
        token = credentials.token(self.credential_namespace, host)
        environment.pop(CLAUDE_TOKEN_VARIABLE, None)
        if token:
            environment[CLAUDE_TOKEN_VARIABLE] = token
        return ProviderProcessEnvironment(local_env=environment).with_claude_foreground_tasks(
            remote=False
        )

    def validate_token(self, token: str) -> str:
        return validate_claude_token(token)

    def save_token(
        self,
        credentials: ProviderCredentialStore,
        host: str,
        token: str,
        *,
        member_id: str,
        now: str,
    ) -> None:
        credentials.store_token(
            self.credential_namespace,
            host,
            token,
            member_id=member_id,
            now=now,
        )

    def prepare_verification(self, credentials: ProviderCredentialStore, host: str) -> None:
        if host:
            self._remote(
                host,
                remote_claude_token_placement_command(),
                token=credentials.token(self.credential_namespace, host),
            )

    def verified(self, credentials: ProviderCredentialStore, host: str, *, now: str) -> None:
        credentials.mark_token_verified(self.credential_namespace, host, now=now)

    def sign_out(self, credentials: ProviderCredentialStore, host: str, binary: str) -> None:
        credentials.delete_token(self.credential_namespace, host)
        if host:
            self._remote(host, remote_claude_token_removal_command())

    @staticmethod
    def _remote(host: str, command: str, *, token: str | None = None) -> None:
        try:
            result = subprocess.run(
                ssh_arguments(host, command),
                input=token + "\n" if token else None,
                capture_output=True,
                text=True,
                timeout=PROVIDER_TOKEN_PLACEMENT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Could not update the remote credential.") from exc
        if result.returncode:
            raise ValueError("Could not update the remote credential.")
