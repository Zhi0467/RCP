"""Private atomic credential persistence and neutral process environments."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import shlex
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from rcp.git_identity import GitIdentity, write_git_identity

if TYPE_CHECKING:
    from rcp.agents.git_access import ProviderGitAccess
    from rcp.agents.write_scope import RegisteredRepositoryRoot
    from rcp.config import Manifest
    from rcp.core.models import HiddenReadScope
    from rcp.providers import AgentCapability, ProviderId
    from rcp.transport.run_stage import RemoteRunStage


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

    def with_variables(
        self, variables: dict[str, str], *, remote: bool
    ) -> ProviderProcessEnvironment:
        """Add fixed variables a provider's processes always need."""
        if not variables:
            return self
        if remote:
            return ProviderProcessEnvironment(
                local_env=self.local_env,
                remote_prefix="; ".join(
                    part
                    for part in (
                        self.remote_prefix,
                        *(
                            f"export {name}={shlex.quote(value)}"
                            for name, value in variables.items()
                        ),
                    )
                    if part
                ),
            )
        return ProviderProcessEnvironment(
            local_env={
                **(os.environ if self.local_env is None else self.local_env),
                **variables,
            },
            remote_prefix=self.remote_prefix,
        )

    def with_path_prefix(self, directory: str, *, remote: bool) -> ProviderProcessEnvironment:
        """Prefix this host's PATH after its login shell has initialized."""
        if remote:
            prefix = f'export PATH={shlex.quote(directory)}:"$PATH"'
            return ProviderProcessEnvironment(
                local_env=self.local_env,
                remote_prefix="; ".join(part for part in (self.remote_prefix, prefix) if part),
            )
        inherited = os.environ if self.local_env is None else self.local_env
        return self.with_variables(
            {"PATH": directory + os.pathsep + inherited.get("PATH", os.defpath)}, remote=False
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


def unhidden_read_scope(
    *, execution_machine: str, host: str, os_account: str = "unknown"
) -> HiddenReadScope:
    """An explicit launch fallback when execution-host policy cannot be prepared."""
    from rcp.core.models import HiddenReadScope, HiddenReadStatus

    return HiddenReadScope(
        execution_machine=execution_machine,
        execution_host=host,
        os_account=os_account or "unknown",
        enforcement=HiddenReadStatus(status="unhidden", reasons=("wrapper_unavailable",)),
    )


def prepare_hidden_read_launch(
    *,
    manifest: Manifest,
    execution_machine: str,
    provider: ProviderId,
    capability: AgentCapability,
    stage_root: str,
    workspace_root: str,
    app_data_dir: Path | None,
    remote_stage: RemoteRunStage | None,
    repository_inventory: Sequence[RegisteredRepositoryRoot] = (),
    machine_hidden_folders: Sequence[str] = (),
    git_access: ProviderGitAccess | None = None,
    browser_enabled: bool = False,
) -> HiddenReadScope:
    """Confirm keys, resolve once, then stage the exact policy used by tools."""
    import logging

    from rcp.agents.hidden_read import resolve_hidden_read_scope, staged_hidden_read_source
    from rcp.agents.staged_hidden_read import install as install_hidden_read_files
    from rcp.agents.write_scope import installed_server_storage
    from rcp.core.models import HiddenReadScope, HiddenReadStatus
    from rcp.limits import HIDDEN_READ_STAGE_TIMEOUT_SECONDS
    from rcp.ssh_agent import (
        confirm_key_evidence,
        confirm_remote_key_evidence,
        running_agent_socket,
        user_ssh_identity_candidates,
    )

    machine = manifest.machine_map[execution_machine]
    try:
        if remote_stage is None:
            deploy_keys = (
                {key for _, key in git_access.checkouts} if git_access is not None else set()
            )
            storage = installed_server_storage(app_data_dir) if app_data_dir is not None else None
            if storage is not None:
                deploy_keys.update(
                    str(path)
                    for path in Path(storage.credentials_root).glob("projects/*/*/id_ed25519")
                )
            evidence = confirm_key_evidence(
                private_key_paths=user_ssh_identity_candidates(Path.home()),
                kind="ssh_identity",
                agent_socket=os.environ.get("SSH_AUTH_SOCK"),
            ) + confirm_key_evidence(
                private_key_paths=tuple(sorted(deploy_keys)),
                kind="deploy_key",
                agent_socket=running_agent_socket(),
            )
        else:
            # Confirm on the execution account, never against the local agents.
            # A transport failure before inventory raises into the unhidden fallback.
            home_result = remote_stage._ssh(["printenv", "HOME"])
            home = home_result.stdout.strip()
            if home_result.returncode or not home.startswith("/"):
                raise OSError("execution-host home could not be resolved")
            evidence = confirm_remote_key_evidence(remote_stage=remote_stage, home=home)
        scope = resolve_hidden_read_scope(
            manifest=manifest,
            execution_machine=execution_machine,
            provider=provider,
            capability=capability,
            stage_root=stage_root,
            workspace_root=workspace_root,
            app_data_dir=app_data_dir,
            remote_stage=remote_stage,
            repository_inventory=list(repository_inventory),
            machine_hidden_folders=list(machine_hidden_folders),
            key_evidence=evidence,
            browser_enabled=browser_enabled,
        )
        reasons = set(scope.enforcement.reasons)
        if provider == "opencode":
            # grep/glob permissions match search patterns, not filesystem paths.
            reasons.add("provider_native_tools_uncovered")
        if reasons:
            scope = HiddenReadScope.model_validate(
                {
                    **scope.model_dump(exclude={"fingerprint"}),
                    "enforcement": HiddenReadStatus(
                        status="unhidden", reasons=tuple(sorted(reasons))
                    ),
                }
            )
        # Staged outside every write root and hidden by the scope itself, so a
        # tool call can neither read nor rewrite the policy it runs under.
        wrapper = PurePosixPath(scope.wrapper_path())
        files = {
            wrapper.name: staged_hidden_read_source(),
            wrapper.name + ".policy.json": json.dumps(
                scope.model_dump(mode="json"), sort_keys=True
            ),
        }
        if remote_stage is not None:
            result = remote_stage._ssh_bytes(
                ["python3", "-c", staged_hidden_read_source(), "--install", str(wrapper.parent)],
                input_data=json.dumps(files).encode(),
                timeout_seconds=HIDDEN_READ_STAGE_TIMEOUT_SECONDS,
            )
            if result.returncode:
                raise OSError("hidden-read wrapper could not be staged")
        else:
            install_hidden_read_files(str(wrapper.parent), files)
        return scope
    except Exception:
        logging.getLogger(__name__).exception(
            "Hidden-read launch preparation failed; running unhidden"
        )
        return unhidden_read_scope(
            execution_machine=execution_machine, host=machine.host, os_account=machine.os_account
        )
