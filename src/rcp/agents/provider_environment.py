"""Private atomic credential persistence and neutral process environments."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from rcp.git_identity import GitIdentity, write_git_identity


class CredentialRecord(BaseModel):
    """Nonsecret facts about a stored credential."""

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

    def with_claude_foreground_tasks(self, *, remote: bool) -> ProviderProcessEnvironment:
        """Keep delegated Claude work inside the invocation that owns it."""
        if remote:
            return ProviderProcessEnvironment(
                local_env=self.local_env,
                remote_prefix="; ".join(
                    part
                    for part in (
                        self.remote_prefix,
                        "export CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1",
                    )
                    if part
                ),
            )
        return ProviderProcessEnvironment(
            local_env={
                **(os.environ if self.local_env is None else self.local_env),
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            },
            remote_prefix=self.remote_prefix,
        )

    def with_git_identity(
        self, identity: GitIdentity, *, data_dir: Path, remote: bool = False
    ) -> ProviderProcessEnvironment:
        """Compose member defaults with the provider's existing credentials."""
        if remote:
            source = importlib.resources.files("rcp").joinpath("git_identity.py").read_text()
            command = shlex.join(
                ["python3", "-c", source, str(data_dir), identity.user_id, identity.display_name]
            )
            prefix = f'GIT_CONFIG_SYSTEM="$({command})" || exit $?; export GIT_CONFIG_SYSTEM'
            return ProviderProcessEnvironment(
                local_env=self.local_env,
                remote_prefix="; ".join(part for part in (self.remote_prefix, prefix) if part),
            )
        path = write_git_identity(data_dir, identity)
        return ProviderProcessEnvironment(
            local_env={
                **(os.environ if self.local_env is None else self.local_env),
                "GIT_CONFIG_SYSTEM": str(path),
            },
            remote_prefix=self.remote_prefix,
        )


def account_directory_name(host: str) -> str:
    """One path-safe directory per execution account; the local account is `local`."""

    if not host:
        return "local"
    return "remote-" + hashlib.sha256(host.encode()).hexdigest()


class ProviderCredentialStore:
    """Secrets RCP keeps for provider logins, under `<data dir>/providers`.

    Provider implementations select their namespace. The directory is excluded
    from protected backups, so a restored server starts without credentials.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> ProviderCredentialStore:
        """The store for one RCP data directory; the only place that names the folder."""

        return cls(data_dir / "providers")

    def account_dir(self, provider: str, host: str) -> Path:
        if provider in {"", ".", ".."}:
            raise ValueError("Invalid credential namespace.")
        return self.root / quote(provider, safe="") / account_directory_name(host)

    def token_path(self, provider: str, host: str) -> Path:
        return self.account_dir(provider, host) / "setup-token"

    def token_record(self, provider: str, host: str) -> CredentialRecord | None:
        path = self.account_dir(provider, host) / "setup-token.json"
        try:
            return CredentialRecord.model_validate(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None

    def token(self, provider: str, host: str) -> str | None:
        try:
            token = self.token_path(provider, host).read_text().strip()
        except OSError:
            return None
        return token or None

    def store_token(
        self, provider: str, host: str, token: str, *, member_id: str, now: str
    ) -> CredentialRecord:
        directory = self.account_dir(provider, host)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        _write_private(self.token_path(provider, host), token + "\n")
        record = CredentialRecord(pasted_at=now, pasted_by=member_id)
        _write_private(directory / "setup-token.json", record.model_dump_json(indent=2) + "\n")
        return record

    def mark_token_verified(self, provider: str, host: str, *, now: str) -> CredentialRecord | None:
        record = self.token_record(provider, host)
        if record is None:
            return None
        record = record.model_copy(update={"verified_at": now})
        _write_private(
            self.account_dir(provider, host) / "setup-token.json",
            record.model_dump_json(indent=2) + "\n",
        )
        return record

    def delete_token(self, provider: str, host: str) -> bool:
        """Remove the token and its record; True when a token was stored."""

        existed = self.token_path(provider, host).exists()
        for name in ("setup-token", "setup-token.json"):
            (self.account_dir(provider, host) / name).unlink(missing_ok=True)
        return existed


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
    "CredentialRecord",
    "ProviderCredentialStore",
    "ProviderProcessEnvironment",
    "account_directory_name",
]
