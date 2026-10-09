"""Member Git access explicitly carried from task admission to provider launch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rcp.agents.checkout_git_access import ensure_team_checkout_access
from rcp.agents.provider_environment import ProviderProcessEnvironment
from rcp.config import Manifest
from rcp.core.models import AuthorizedHuman
from rcp.git_identity import GitIdentity
from rcp.provisioning_repositories import effective_repositories
from rcp.server_ops.layout import ServerLayout, remote_project_deploy_key_relative_path
from rcp.storage import AppStore


@dataclass(frozen=True)
class ProviderGitAccess:
    identity: GitIdentity | None
    data_dir: Path
    host: str
    checkouts: tuple[tuple[str, str], ...] = ()

    async def prepare(
        self, environment: ProviderProcessEnvironment
    ) -> tuple[ProviderProcessEnvironment, list[str]]:
        """Return the environment and one notice per repository Git cannot reach."""
        notices = await ensure_team_checkout_access(list(self.checkouts), host=self.host)
        if self.identity is not None:
            # A task with no authorizing member has no identity to default to.
            environment = environment.with_git_identity(
                self.identity, data_dir=self.data_dir, remote=bool(self.host)
            )
        return environment, notices


def provider_git_access(
    manifest: Manifest,
    *,
    project_id: str,
    run_on: str,
    member: AuthorizedHuman | None,
    store: AppStore,
    data_dir: Path,
    layout: ServerLayout,
) -> ProviderGitAccess:
    host = manifest.machine_map[run_on].host
    sources = {}
    if store.space_kind == "team":
        project = store.project(project_id)
        if project is None:
            raise ValueError("The provider project is not registered.")
        sources = {
            repository.alias: repository
            for repository in effective_repositories(
                project, store.completed_project_provisioning_requests(project_id)
            )
        }
        if any(repository.alias not in sources for repository in manifest.repositories):
            raise ValueError("A provider repository has no completed provisioning proof.")
    checkouts = tuple(
        (
            repository.path,
            str(Path("~") / remote_project_deploy_key_relative_path(project_id, repository.alias))
            if host
            else str(layout.project_deploy_key_path(project_id, repository.alias)),
        )
        for repository in manifest.repositories
        if store.space_kind == "team"
        and sources[repository.alias].repository is not None
        and manifest.machine_map[repository.machine].host == host
    )
    return ProviderGitAccess(
        identity=GitIdentity(member.user_id, member.display_name) if member else None,
        data_dir=Path("~/.local/share/rcp") if host else data_dir,
        host=host,
        checkouts=checkouts,
    )
