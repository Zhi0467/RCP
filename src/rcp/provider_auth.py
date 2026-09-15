"""Provider-owned authentication mechanics; no task or episode authority."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
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


class ProviderAuthentication:
    supports_sign_out = False
    methods: tuple[str, ...] = ()
    token_instructions: str | None = None
    missing_credential_detail = "A managed credential is required. Sign in in Settings."

    def credential_available(self, credentials: ProviderCredentialStore, host: str) -> bool:
        return True

    def credential_metadata(self, credentials: ProviderCredentialStore, host: str) -> dict | None:
        return None

    def process_environment(
        self, credentials: ProviderCredentialStore, host: str
    ) -> ProviderProcessEnvironment:
        return ProviderProcessEnvironment()

    def device_command(self, binary: str) -> list[str]:
        raise ValueError("Device sign-in is not supported by this provider.")

    def device_fields(self, line: str) -> dict[str, str]:
        return {}

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

    def device_command(self, binary: str) -> list[str]:
        return [binary, "login", "--device-auth"]

    def device_fields(self, line: str) -> dict[str, str]:
        fields = {}
        url = re.search(r"https?://[^\s'\"<>]+", line)
        code = re.search(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{3,})+\b", line)
        if url:
            fields["verification_url"] = url.group(0).rstrip(".,")
        if code:
            fields["user_code"] = code.group(0)
        elif re.fullmatch(r"[A-Z0-9]{6,12}", line.strip()):
            fields["user_code"] = line.strip()
        return fields

    def sign_out(self, credentials: ProviderCredentialStore, host: str, binary: str) -> list[str]:
        return [binary, "logout"]


class ClaudeAuthentication(ProviderAuthentication):
    supports_sign_out = True
    credential_namespace = "claude"
    methods = ("token_entry",)
    token_instructions = "Run claude setup-token, then paste the setup token here."
    missing_credential_detail = (
        "The Claude setup token is missing. Save a setup token in Settings to sign in."
    )

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
            )
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in CLAUDE_CONFLICTING_VARIABLES
        }
        token = credentials.token(self.credential_namespace, host)
        environment.pop(CLAUDE_TOKEN_VARIABLE, None)
        if token:
            environment[CLAUDE_TOKEN_VARIABLE] = token
        return ProviderProcessEnvironment(local_env=environment)

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
