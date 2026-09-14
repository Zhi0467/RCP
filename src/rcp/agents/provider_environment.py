"""The one environment every RCP-started Claude process receives.

Claude Code rotates the same kind of single-use refresh token that killed the
shared Codex login, and every process that reads its credentials file can spend
it. RCP therefore runs Claude on a long-lived `setup-token` instead: the token
is kept in the execution account's credential store and injected as
`CLAUDE_CODE_OAUTH_TOKEN`, which Claude uses without refreshing anything. The
two variables that would make Claude bill an API key instead are removed, so a
member's shell cannot silently change what the service runs on.

The token never appears in a command line. A local process receives it in its
environment; a remote process reads it from a file on the remote account inside
the same login shell that starts the provider.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rcp.limits import PROVIDER_TOKEN_MAX_CHARS

CLAUDE_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_CONFLICTING_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
#: Where a remote execution account keeps the token RCP placed for it.
REMOTE_CLAUDE_TOKEN_PATH = "~/.config/rcp/claude-setup-token"

_UNSAFE_ACCOUNT_CHARS = re.compile(r"[^A-Za-z0-9._@-]+")


class ClaudeTokenRecord(BaseModel):
    """Nonsecret facts about a stored Claude setup token."""

    model_config = ConfigDict(extra="forbid")

    pasted_at: str
    pasted_by: str
    verified_at: str | None = None


@dataclass(frozen=True)
class ProviderProcessEnvironment:
    """What a provider process must be started with.

    `local_env` is the complete environment for a local process, or None to
    inherit RCP's own. `remote_prefix` is a shell fragment the remote login shell
    runs before the provider command, or None when nothing is needed.
    """

    local_env: dict[str, str] | None = None
    remote_prefix: str | None = None


def account_directory_name(host: str) -> str:
    """One path-safe directory per execution account; the local account is `local`."""

    if not host:
        return "local"
    return _UNSAFE_ACCOUNT_CHARS.sub("_", host) or "remote"


def validate_claude_token(token: str) -> str:
    """Accept only a token-shaped string; the shape is all RCP can check."""

    if not token or token != token.strip() or any(ch.isspace() for ch in token):
        raise ValueError("A setup token has no spaces or line breaks.")
    if len(token) > PROVIDER_TOKEN_MAX_CHARS:
        raise ValueError("The pasted value is longer than any setup token.")
    return token


class ProviderCredentialStore:
    """Secrets RCP keeps for provider logins, under `<data dir>/providers`.

    Only Claude has one today: `claude/<account>/setup-token` (mode 0600) beside
    a nonsecret `setup-token.json`. The whole directory is excluded from
    protected backups, so a restored server starts without any token.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def claude_account_dir(self, host: str) -> Path:
        return self.root / "claude" / account_directory_name(host)

    def claude_token_path(self, host: str) -> Path:
        return self.claude_account_dir(host) / "setup-token"

    def claude_token_record(self, host: str) -> ClaudeTokenRecord | None:
        path = self.claude_account_dir(host) / "setup-token.json"
        try:
            return ClaudeTokenRecord.model_validate(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None

    def claude_token(self, host: str) -> str | None:
        try:
            token = self.claude_token_path(host).read_text().strip()
        except OSError:
            return None
        return token or None

    def store_claude_token(
        self, host: str, token: str, *, member_id: str, now: str
    ) -> ClaudeTokenRecord:
        token = validate_claude_token(token)
        directory = self.claude_account_dir(host)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        _write_private(self.claude_token_path(host), token + "\n")
        record = ClaudeTokenRecord(pasted_at=now, pasted_by=member_id)
        _write_private(directory / "setup-token.json", record.model_dump_json(indent=2) + "\n")
        return record

    def mark_claude_token_verified(self, host: str, *, now: str) -> ClaudeTokenRecord | None:
        record = self.claude_token_record(host)
        if record is None:
            return None
        record = record.model_copy(update={"verified_at": now})
        _write_private(
            self.claude_account_dir(host) / "setup-token.json",
            record.model_dump_json(indent=2) + "\n",
        )
        return record

    def delete_claude_token(self, host: str) -> bool:
        """Remove the token and its record; True when a token was stored."""

        existed = self.claude_token_path(host).exists()
        for name in ("setup-token", "setup-token.json"):
            (self.claude_account_dir(host) / name).unlink(missing_ok=True)
        return existed

    def process_environment(self, provider: str, host: str) -> ProviderProcessEnvironment:
        """The environment for one provider process on one account."""

        if provider != "claude":
            return ProviderProcessEnvironment()
        if host:
            if self.claude_token_record(host) is None:
                return ProviderProcessEnvironment()
            return ProviderProcessEnvironment(remote_prefix=remote_claude_token_prefix())
        environment = {
            name: value
            for name, value in os.environ.items()
            if name not in CLAUDE_CONFLICTING_VARIABLES
        }
        token = self.claude_token("")
        if token is not None:
            environment[CLAUDE_TOKEN_VARIABLE] = token
        else:
            environment.pop(CLAUDE_TOKEN_VARIABLE, None)
        return ProviderProcessEnvironment(local_env=environment)


def remote_claude_token_prefix(path: str = REMOTE_CLAUDE_TOKEN_PATH) -> str:
    """The shell fragment that exports the token from the remote account's file.

    The path is quoted so `~` still expands; the token itself never enters the
    command line. A missing file leaves the variable unset rather than exporting
    an empty value that Claude would treat as a bad credential.
    """

    unset = " ".join(CLAUDE_CONFLICTING_VARIABLES)
    quoted = _quote_home_relative(path)
    return (
        f"unset {unset}; "
        f'if [ -r {quoted} ]; then export {CLAUDE_TOKEN_VARIABLE}="$(cat {quoted})"; fi'
    )


def remote_claude_token_placement_command(path: str = REMOTE_CLAUDE_TOKEN_PATH) -> str:
    """Write the token read from stdin to the remote account, mode 0600 in a 0700 directory."""

    quoted = _quote_home_relative(path)
    directory = _quote_home_relative(str(Path(path).parent))
    return f"umask 077 && mkdir -p {directory} && cat > {quoted}"


def remote_claude_token_removal_command(path: str = REMOTE_CLAUDE_TOKEN_PATH) -> str:
    return f"rm -f {_quote_home_relative(path)}"


def _quote_home_relative(path: str) -> str:
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _write_private(path: Path, text: str) -> None:
    """Atomic private write: a reader sees the old file or the new one, never a partial."""

    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


__all__ = [
    "CLAUDE_CONFLICTING_VARIABLES",
    "CLAUDE_TOKEN_VARIABLE",
    "ClaudeTokenRecord",
    "ProviderCredentialStore",
    "ProviderProcessEnvironment",
    "REMOTE_CLAUDE_TOKEN_PATH",
    "account_directory_name",
    "remote_claude_token_placement_command",
    "remote_claude_token_prefix",
    "remote_claude_token_removal_command",
    "validate_claude_token",
]
