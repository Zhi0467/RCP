"""How a member signs Claude Code in with a setup token, verifies it, and signs it out."""

from __future__ import annotations

import os
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
from rcp.provider_auth import ProviderAuthentication
from rcp.transport.ssh import ssh_arguments

if TYPE_CHECKING:
    from rcp.agents.provider_environment import ProviderCredentialStore
    from rcp.storage.models import ProviderLoginStateRecord


class ClaudeAuthentication(ProviderAuthentication):
    supports_sign_out = True
    credential_namespace = "claude"
    methods = ("token_entry",)
    token_variable = "CLAUDE_CODE_OAUTH_TOKEN"
    #: Variables that would make Claude bill an API key instead of the token.
    conflicting_variables = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    #: A hosting Claude Code session's identity and channels back to it, inherited
    #: when RCP itself runs inside one. `CLAUDE_CODE_ENTRYPOINT=claude-desktop`
    #: alone swaps in the desktop system prompt and adds ten desktop tools
    #: (probed on 2.1.312); the rest would bind a headless turn to that session or
    #: account. Member configuration such as Bedrock or Vertex settings stays.
    host_session_variables = (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_HOST_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
        "CLAUDE_CODE_ACCOUNT_UUID",
        "CLAUDE_CODE_ORGANIZATION_UUID",
        "CLAUDE_CODE_USER_EMAIL",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "CLAUDE_CODE_DESKTOP_APP_VERSION",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_PID",
    )
    #: Where a remote execution account keeps the token RCP placed for it.
    remote_token_path = "~/.config/rcp/claude-setup-token"
    # Keep delegated Claude work inside the invocation that owns it, and keep
    # auto-memory from carrying one run's notes into another's context.
    process_variables = {
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
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

    def token_path(self, login_id: str) -> str:
        from rcp.agents.provider_environment import validate_login_id

        validate_login_id(login_id)
        return (
            f"~/.config/rcp/logins/{login_id}/claude-setup-token"
            if login_id
            else self.remote_token_path
        )

    def verification_succeeded(self, result: subprocess.CompletedProcess[str]) -> bool:
        from rcp.providers.claude.profile import ClaudeProfile

        output = subprocess.CompletedProcess(result.args, result.returncode, result.stdout, "")
        return (
            super().verification_succeeded(result)
            and not ClaudeProfile().probe_failure_evidence(output).strip()
        )

    def credential_available(
        self,
        credentials: ProviderCredentialStore,
        host: str,
        login_id: str,
        *,
        login_state: ProviderLoginStateRecord | None = None,
    ) -> bool:
        return credentials.token(self.credential_namespace, host, login_id) is not None

    def credential_metadata(
        self, credentials: ProviderCredentialStore, host: str, login_id: str
    ) -> dict | None:
        record = credentials.token_record(self.credential_namespace, host, login_id)
        if record is None or not self.credential_available(credentials, host, login_id):
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
        self, credentials: ProviderCredentialStore, host: str, login_id: str
    ) -> ProviderProcessEnvironment:
        if host:
            return ProviderProcessEnvironment(
                remote_prefix=self.remote_token_prefix(self.token_path(login_id))
                if self.credential_available(credentials, host, login_id)
                else None
            ).with_variables(self.process_variables, remote=True)
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in self.conflicting_variables and name not in self.host_session_variables
        }
        token = credentials.token(self.credential_namespace, host, login_id)
        environment.pop(self.token_variable, None)
        if token:
            environment[self.token_variable] = token
        return ProviderProcessEnvironment(local_env=environment).with_variables(
            self.process_variables, remote=False
        )

    def validate_token(self, token: str) -> str:
        """Accept only a token-shaped string; the shape is all RCP can check."""
        if not token or token != token.strip() or any(ch.isspace() for ch in token):
            raise ValueError("A setup token has no spaces or line breaks.")
        if len(token) > PROVIDER_TOKEN_MAX_CHARS:
            raise ValueError("The pasted value is longer than any setup token.")
        return token

    def remote_token_prefix(self, path: str | None = None) -> str:
        """The shell fragment that exports the token from the remote account's file.

        The path is quoted so `~` still expands; the token itself never enters the
        command line. A missing or empty file refuses the process rather than
        falling back to credentials inherited from the remote login shell.
        """
        unset = " ".join(self.conflicting_variables)
        quoted = _quote_home_relative(path or self.remote_token_path)
        return (
            f"unset {unset} {self.token_variable}; "
            f'if [ -s {quoted} ]; then export {self.token_variable}="$(cat {quoted})"; '
            "else printf '%s\\n' 'RCP managed credential is missing' >&2; exit 1; fi"
        )

    def remote_token_placement_command(self, path: str | None = None) -> str:
        """Write the token read from stdin to the remote account, mode 0600 in a 0700 directory."""
        path = path or self.remote_token_path
        quoted = _quote_home_relative(path)
        directory = _quote_home_relative(str(Path(path).parent))
        temporary = quoted + ".tmp"
        return (
            f"umask 077 && mkdir -p {directory} && cat > {temporary} "
            f"&& chmod 600 {temporary} && mv -f {temporary} {quoted}"
        )

    def save_token(
        self,
        credentials: ProviderCredentialStore,
        host: str,
        login_id: str,
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
            login_id=login_id,
        )

    def prepare_verification(
        self, credentials: ProviderCredentialStore, host: str, login_id: str
    ) -> None:
        if host:
            self._remote(
                host,
                self.remote_token_placement_command(self.token_path(login_id)),
                token=credentials.token(self.credential_namespace, host, login_id),
            )

    def verified(
        self, credentials: ProviderCredentialStore, host: str, login_id: str, *, now: str
    ) -> None:
        credentials.mark_token_verified(self.credential_namespace, host, login_id, now=now)

    def sign_out(
        self, credentials: ProviderCredentialStore, host: str, login_id: str, binary: str
    ) -> None:
        credentials.delete_token(self.credential_namespace, host, login_id)
        if host:
            self._remote(host, f"rm -f {_quote_home_relative(self.token_path(login_id))}")

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


def _quote_home_relative(path: str) -> str:
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)
