from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.agents.context import RepositoryPointer
from rcp.config import Manifest, RepositoryConfig
from rcp.core.models import ConversationWorktreeBinding
from rcp.providers import AgentCapability
from rcp.rcp_home import command_socket_directory, short_socket_root
from rcp.transport.run_stage import RemoteRunStage


class WritableRepositoryRoot(BaseModel):
    """One exact project repository admitted on the execution machine."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    alias: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    path: str = Field(min_length=1)


class RegisteredRepositoryRoot(BaseModel):
    """One repository root owned by a project in the application catalog."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    project_id: str = Field(min_length=1)
    alias: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    execution_host: str
    path: str = Field(min_length=1)


class ProjectWriteScope(BaseModel):
    """Provider-neutral, canonical filesystem scope for one Work-like launch."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_generation: Literal[1] = 1
    project_id: str = Field(min_length=1)
    execution_machine: str = Field(min_length=1)
    execution_host: str
    capability: Literal["work_auto", "orchestrate"]
    stage_root: str = Field(min_length=1)
    workspace_root: str = Field(min_length=1)
    repositories: list[WritableRepositoryRoot] = Field(default_factory=list)
    git_metadata_roots: list[str] = Field(default_factory=list)
    # Space-machine writable paths plus the default temporary roots. Present
    # only on launches that also write repositories.
    granted_roots: list[str] = Field(default_factory=list)
    protected_write_paths: list[str] = Field(default_factory=list)
    # The subset of protected paths present only because a grant covers RCP's
    # own storage; kept apart so a pre-grant fingerprint can be derived.
    granted_protected_paths: list[str] = Field(default_factory=list)
    # Other tasks' legacy /tmp stages: enforced, but they come and go as stages
    # are swept, so they stay out of the fingerprint.
    transient_protected_paths: list[str] = Field(default_factory=list)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_canonical_scope(self) -> ProjectWriteScope:
        for value in (self.stage_root, self.workspace_root):
            if not PurePosixPath(value).is_absolute():
                raise ValueError("write-scope stage paths must be absolute")
        stage = PurePosixPath(self.stage_root)
        workspace = PurePosixPath(self.workspace_root)
        if workspace != stage and stage not in workspace.parents:
            raise ValueError("write-scope workspace must be inside its exact task stage")
        repository_keys = [(item.alias, item.machine, item.path) for item in self.repositories]
        if repository_keys != sorted(set(repository_keys)):
            raise ValueError("write-scope repositories must be sorted and unique")
        paths = [item.path for item in self.repositories]
        if any(not PurePosixPath(item).is_absolute() for item in paths):
            raise ValueError("write-scope repository roots must be absolute")
        if len(paths) != len(set(paths)):
            raise ValueError("write-scope repository roots must be exact and unique")
        if self.git_metadata_roots != sorted(set(self.git_metadata_roots)):
            raise ValueError("Git metadata roots must be sorted and unique")
        if any(not PurePosixPath(item).is_absolute() for item in self.git_metadata_roots):
            raise ValueError("Git metadata roots must be absolute")
        if self.granted_roots != sorted(set(self.granted_roots)):
            raise ValueError("granted roots must be sorted and unique")
        if any(not PurePosixPath(item).is_absolute() for item in self.granted_roots):
            raise ValueError("granted roots must be absolute")
        if self.granted_protected_paths != sorted(set(self.granted_protected_paths)):
            raise ValueError("granted protected paths must be sorted and unique")
        if not set(self.granted_protected_paths).issubset(self.protected_write_paths):
            raise ValueError("granted protected paths must be protected write paths")
        if self.transient_protected_paths != sorted(set(self.transient_protected_paths)):
            raise ValueError("transient protected paths must be sorted and unique")
        if not set(self.transient_protected_paths).issubset(self.granted_protected_paths):
            raise ValueError("transient protected paths must be granted protected paths")
        if self.protected_write_paths != sorted(set(self.protected_write_paths)):
            raise ValueError("protected write paths must be sorted and unique")
        if any(not PurePosixPath(item).is_absolute() for item in self.protected_write_paths):
            raise ValueError("protected write paths must be absolute")
        expected = _scope_fingerprint(self._fingerprint_payload())
        if self.fingerprint != expected:
            raise ValueError("write-scope fingerprint does not match its canonical roots")
        return self

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        execution_machine: str,
        execution_host: str,
        capability: Literal["work_auto", "orchestrate"],
        stage_root: str,
        workspace_root: str,
        repositories: list[WritableRepositoryRoot],
        protected_write_paths: list[str],
        git_metadata_roots: list[str] | None = None,
        granted_roots: list[str] | None = None,
        granted_protected_paths: list[str] | None = None,
        transient_protected_paths: list[str] | None = None,
    ) -> ProjectWriteScope:
        payload: dict[str, object] = {
            "schema_generation": 1,
            "project_id": project_id,
            "execution_machine": execution_machine,
            "execution_host": execution_host,
            "capability": capability,
            "stage_root": stage_root,
            "workspace_root": workspace_root,
            "repositories": [
                item.model_dump(mode="json")
                for item in sorted(
                    repositories,
                    key=lambda item: (item.alias, item.machine, item.path),
                )
            ],
            "protected_write_paths": sorted(set(protected_write_paths)),
        }
        if git_metadata_roots:
            payload["git_metadata_roots"] = sorted(set(git_metadata_roots))
        if granted_roots:
            payload["granted_roots"] = sorted(set(granted_roots))
        if granted_protected_paths:
            payload["granted_protected_paths"] = sorted(set(granted_protected_paths))
        transient = sorted(set(transient_protected_paths or []))
        fingerprinted = _without_transient(payload, transient)
        if transient:
            payload["transient_protected_paths"] = transient
        return cls.model_validate({**payload, "fingerprint": _scope_fingerprint(fingerprinted)})

    def without_grants(self) -> ProjectWriteScope:
        """This scope as a build without machine grants would have resolved it."""

        granted_protected = set(self.granted_protected_paths)
        return ProjectWriteScope.create(
            project_id=self.project_id,
            execution_machine=self.execution_machine,
            execution_host=self.execution_host,
            capability=self.capability,
            stage_root=self.stage_root,
            workspace_root=self.workspace_root,
            repositories=self.repositories,
            git_metadata_roots=self.git_metadata_roots,
            protected_write_paths=[
                path for path in self.protected_write_paths if path not in granted_protected
            ],
        )

    @property
    def repository_roots(self) -> list[str]:
        return [item.path for item in self.repositories]

    @property
    def writable_roots(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    self.workspace_root,
                    *self.repository_roots,
                    *self.git_metadata_roots,
                    *self.granted_roots,
                ]
            )
        )

    def _fingerprint_payload(self) -> dict[str, object]:
        # Retain every existing unbound scope fingerprint across this additive
        # field: only worktree scopes carry Git metadata outside their checkout.
        excluded = {"fingerprint"}
        if not self.git_metadata_roots:
            excluded.add("git_metadata_roots")
        if not self.granted_roots:
            excluded.add("granted_roots")
        if not self.granted_protected_paths:
            excluded.add("granted_protected_paths")
        excluded.add("transient_protected_paths")
        return _without_transient(
            self.model_dump(mode="json", exclude=excluded), self.transient_protected_paths
        )


def resolve_project_write_scope(
    *,
    manifest: Manifest,
    project_id: str,
    execution_machine: str,
    capability: AgentCapability,
    stage_root: str,
    workspace_root: str,
    admitted_aliases: list[str],
    repository_pointers: list[RepositoryPointer],
    remote_stage: RemoteRunStage | None,
    app_data_dir: Path | None,
    repository_inventory: list[RegisteredRepositoryRoot],
    additional_protected_write_paths: list[str] | None = None,
    conversation_worktree: ConversationWorktreeBinding | None = None,
    include_shared_checkout: bool = False,
    machine_writable_paths: list[str] | None = None,
) -> ProjectWriteScope:
    """Resolve and verify one exact Work-like scope on its execution machine.

    A scope that writes repositories also receives the execution machine's
    space-level writable paths and the default temporary roots. RCP's own
    storage inside any of them stays read-only.
    """

    if capability not in {"work_auto", "orchestrate"}:
        raise ValueError(f"capability {capability!r} has no project write scope")
    if not project_id:
        raise ValueError("project write scope requires a durable project id")
    machine = manifest.machine_map.get(execution_machine)
    if machine is None:
        raise ValueError(f"unknown write-scope execution machine: {execution_machine}")
    if bool(machine.host) != (remote_stage is not None):
        raise ValueError("write-scope execution host does not match its task stage")
    if remote_stage is not None and remote_stage.host != machine.host:
        raise ValueError("write-scope remote stage belongs to a different execution host")
    path_semantics = _ExecutionPathSemantics.for_execution(remote=remote_stage is not None)

    aliases = sorted(set(admitted_aliases))
    if aliases != sorted(admitted_aliases):
        raise ValueError("write-scope repository aliases must be sorted and unique")
    project_aliases = set(manifest.project.truth_scope)
    if not set(aliases).issubset(project_aliases):
        unknown = sorted(set(aliases) - project_aliases)
        raise ValueError(f"write scope names repositories outside this project: {unknown}")

    pointers: dict[str, RepositoryPointer] = {}
    for pointer in repository_pointers:
        if pointer.alias in pointers:
            raise ValueError(f"duplicate repository pointer in write scope: {pointer.alias}")
        pointers[pointer.alias] = pointer

    eligible = [
        manifest.repository_map[alias]
        for alias in aliases
        if manifest.repository_map[alias].machine == execution_machine
    ]
    binding = conversation_worktree
    if include_shared_checkout and binding is None:
        raise ValueError("shared checkout integration requires a conversation worktree binding")
    if binding is not None:
        if capability != "work_auto":
            raise ValueError("conversation worktrees require ordinary Work capability")
        if binding.status != "ready":
            raise ValueError("conversation worktree is not ready")
        if (
            binding.project_id != project_id
            or binding.machine != execution_machine
            or binding.execution_host != machine.host
        ):
            raise ValueError(
                "conversation worktree belongs to a different project or execution host"
            )
        if aliases != [binding.repository_alias] or len(eligible) != 1:
            raise ValueError("conversation worktree requires its exact single repository run scope")
    explicit_protected = list(additional_protected_write_paths or [])
    declared_paths: list[str] = [stage_root, workspace_root, *explicit_protected]
    for repository in eligible:
        pointer = pointers.get(repository.alias)
        if pointer is None:
            raise ValueError(f"write scope is missing repository pointer {repository.alias!r}")
        # Only the machine is compared. A pointer's host says how the *agent*
        # reaches the repository -- `_stage_context_paths` blanks it for a
        # repository that lives on the execution machine, because the agent
        # opens it as a local path there -- while `machine.host` says where the
        # machine is. Comparing them refused every remote run and passed every
        # local one by comparing a value to itself. The pointer's path is what
        # this scope must agree with, and `registered_root != pointer_root`
        # below checks it against the execution host's own filesystem.
        if pointer.machine != repository.machine:
            raise ValueError(
                f"repository {repository.alias!r} does not match its project execution machine"
            )
        declared_paths.extend([repository.path, pointer.path])
    if binding is not None:
        declared_paths.extend([binding.shared_path, binding.worktree_path, binding.git_common_dir])
    # Grants join the one canonicalization round trip. Only a scope that writes
    # repositories receives them.
    declared_grants = (
        [
            *(machine_writable_paths or []),
            *default_temporary_roots(remote=remote_stage is not None),
        ]
        if eligible
        else []
    )
    declared_paths.extend(declared_grants)

    canonical, account_home = _canonical_directories(
        declared_paths,
        remote_stage=remote_stage,
        require_writable=True,
    )
    canonical_stage = canonical[stage_root]
    canonical_workspace = canonical[workspace_root]
    if remote_stage is not None:
        assert remote_stage.root is not None
        if canonical_stage != str(remote_stage.root) or canonical_workspace != str(
            remote_stage.workspace
        ):
            raise ValueError("remote write scope does not match its exact RCP task stage")

    execution_inventory = [
        item for item in repository_inventory if item.execution_host == machine.host
    ]
    inventory_paths = [item.path for item in execution_inventory]
    canonical_inventory, _inventory_home = _canonical_directories(
        inventory_paths,
        remote_stage=remote_stage,
        require_writable=False,
    )

    repository_roots: list[WritableRepositoryRoot] = []
    for repository in eligible:
        pointer = pointers[repository.alias]
        registered_root = canonical[repository.path]
        pointer_root = canonical[pointer.path]
        if binding is not None:
            if registered_root != binding.shared_path:
                raise ValueError(
                    "conversation worktree no longer matches its registered shared root"
                )
            if canonical[binding.worktree_path] != binding.worktree_path:
                raise ValueError("conversation worktree moved or its path was retargeted")
            if canonical[binding.git_common_dir] != binding.git_common_dir:
                raise ValueError("conversation Git metadata moved or its path was retargeted")
            if pointer_root != binding.worktree_path:
                raise ValueError("repository pointer does not match its conversation worktree")
            if path_semantics.overlaps(registered_root, pointer_root):
                raise ValueError("conversation worktree must be outside the shared checkout")
        elif registered_root != pointer_root:
            raise ValueError(
                f"repository {repository.alias!r} no longer matches its registered project root"
            )
        ownership = [
            item
            for item in execution_inventory
            if item.project_id == project_id
            and item.alias == repository.alias
            and item.machine == repository.machine
            and item.path == repository.path
        ]
        if len(ownership) != 1:
            raise ValueError(
                f"repository {repository.alias!r} is missing from the canonical project inventory"
            )
        if canonical_inventory[ownership[0].path] != registered_root:
            raise ValueError(
                f"repository {repository.alias!r} changed while its ownership was verified"
            )
        roots = [pointer_root]
        if include_shared_checkout:
            roots.append(registered_root)
        # Keep the registered checkout's catalog protections even when only the
        # conversation's worktree is admitted for writes.
        for root in dict.fromkeys(
            [registered_root, *roots, *([binding.git_common_dir] if binding else [])]
        ):
            _reject_broad_repository_root(
                root,
                account_home=account_home,
                app_data_dir=app_data_dir if remote_stage is None else None,
                path_semantics=path_semantics,
            )
            _reject_repository_ownership_overlap(
                repository=repository,
                project_id=project_id,
                admitted_owners={
                    (project_id, item.alias, item.machine, item.path) for item in eligible
                },
                registered_root=root,
                inventory=execution_inventory,
                canonical_inventory=canonical_inventory,
                path_semantics=path_semantics,
            )
        repository_roots.extend(
            WritableRepositoryRoot(alias=repository.alias, machine=repository.machine, path=root)
            for root in roots
        )

    granted_input = sorted({canonical[path] for path in declared_grants})
    legacy_stages = [
        root
        for root in (
            remote_stage.legacy_stage_roots() if remote_stage is not None and granted_input else []
        )
        if root != canonical_stage
    ]
    granted, rcp_protected = _granted_roots(
        granted_input,
        owned=[
            *rcp_owned_paths(
                account_home=account_home,
                app_data_dir=app_data_dir,
                remote=remote_stage is not None,
            ),
            # Canonical state of every repository on this host, admitted or not,
            # declared and resolved on the execution host (it may be a symlink).
            *(
                protected_repository_paths(
                    manifest=manifest,
                    repository_roots=sorted(
                        {
                            path
                            for item in execution_inventory
                            for path in (item.path, canonical_inventory[item.path])
                        }
                    ),
                    remote_stage=remote_stage,
                )
                if granted_input
                else []
            ),
            # This launch's own immutable inputs, and other tasks' legacy stages.
            str(PurePosixPath(canonical_stage) / "inputs"),
            *legacy_stages,
        ],
        path_semantics=path_semantics,
    )

    base_protected = protected_repository_paths(
        manifest=manifest,
        repository_roots=[item.path for item in repository_roots],
        remote_stage=remote_stage,
        additional_paths=[*explicit_protected, *(canonical[path] for path in explicit_protected)],
    )
    granted_protected = sorted(set(rcp_protected) - set(base_protected))
    protected = sorted({*base_protected, *granted_protected})

    return ProjectWriteScope.create(
        project_id=project_id,
        execution_machine=execution_machine,
        execution_host=machine.host,
        capability=capability,
        stage_root=canonical_stage,
        workspace_root=canonical_workspace,
        repositories=repository_roots,
        git_metadata_roots=[binding.git_common_dir] if binding else [],
        granted_roots=granted,
        protected_write_paths=protected,
        granted_protected_paths=granted_protected,
        transient_protected_paths=sorted(set(granted_protected) & set(legacy_stages)),
    )


def protected_repository_paths(
    *,
    manifest: Manifest,
    repository_roots: list[str],
    remote_stage: RemoteRunStage | None = None,
    additional_paths: list[str] | None = None,
) -> list[str]:
    """Canonical-state write denies shared by Work and member terminals."""
    state_repository = manifest.repository_map[manifest.state.repository]
    declared = [str(PurePosixPath(root) / ".research") for root in repository_roots]
    declared.append(str(PurePosixPath(state_repository.path) / ".research"))
    protected = [*declared, *(additional_paths or [])]
    unique = list(dict.fromkeys(declared))
    # One call, not one per repository: with a remote stage each call is an SSH
    # exec, so a project with several repositories otherwise pays a round trip
    # per repository on every launch.
    try:
        canonical, _home = _canonical_directories(
            unique, remote_stage=remote_stage, require_writable=False
        )
        protected.extend(canonical[path] for path in unique)
    except (OSError, ValueError):
        # One unavailable state directory must not discard the others, so fall
        # back to resolving each on its own. An unresolved path keeps its
        # lexical write deny.
        for path in unique:
            try:
                resolved, _home = _canonical_directories(
                    [path], remote_stage=remote_stage, require_writable=False
                )
            except (OSError, ValueError):
                continue
            protected.append(resolved[path])
    return sorted(set(protected))


def default_temporary_roots(*, remote: bool) -> list[str]:
    """Temporary roots every repository-writing launch may write.

    RCP keeps none of its own state in them.
    """

    roots = ["/tmp"]
    if not remote:
        roots.append(tempfile.gettempdir())
    return roots


def rcp_owned_paths(
    *,
    account_home: str,
    app_data_dir: Path | None,
    remote: bool,
) -> list[str]:
    """RCP's own storage on one execution machine; never writable by a grant.

    Each path comes from the owner that creates it: the running data directory,
    the installed server layout, and the per-account `~/.rcp` and
    `~/.local/share/rcp` roots.
    """

    home = PurePosixPath(account_home)
    paths = [str(home / ".rcp")]
    if command_socket_directory(account_home) != str(home / ".rcp" / "sockets"):
        paths.append(short_socket_root(account_home))
    if remote:
        paths.append(str(home / ".local" / "share" / "rcp"))
        return paths
    from rcp.transport.ssh import control_directory_candidate

    paths.append(str(control_directory_candidate()))
    if app_data_dir is not None:
        data_dir = app_data_dir.expanduser().resolve()
        paths.append(str(data_dir))
        paths.extend(_installed_server_paths(data_dir))
    return paths


def _installed_server_paths(data_dir: Path) -> list[str]:
    from rcp.server_ops.config import load_installed_server_config
    from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT

    config_path = DEFAULT_SERVER_LAYOUT.config_path
    if not os.path.lexists(config_path):
        return []
    config = load_installed_server_config(config_path)
    paths = config.paths
    if Path(paths.data_dir).resolve() != data_dir:
        return []
    return [
        paths.releases_root,
        paths.source_checkout,
        paths.credentials_root,
        paths.update_checkpoints_root,
        paths.restore_operations_root,
        str(PurePosixPath(paths.config_path).parent),
        paths.runtime_dir,
    ]


def _granted_roots(
    grants: list[str],
    *,
    owned: list[str],
    path_semantics: _ExecutionPathSemantics,
) -> tuple[list[str], list[str]]:
    """Refuse grants inside protected storage; return the protected paths they cover.

    Locally, identity is filesystem identity: resolved forms are compared too,
    and `samefile` catches case-insensitive and symlinked aliases.
    """

    if not grants:
        return [], []
    candidates = list(dict.fromkeys(owned))
    if not path_semantics.remote:
        candidates = list(
            dict.fromkeys([*candidates, *(str(Path(path).resolve()) for path in candidates)])
        )
    for grant in grants:
        for path in candidates:
            if path_semantics.equal(grant, path) or any(
                path_semantics.equal(parent, path) for parent in PurePosixPath(grant).parents
            ):
                raise ValueError(f"writable path {grant} is inside protected storage at {path}")
    covered = sorted(
        {
            path
            for path in candidates
            if any(
                path_semantics.equal(parent, grant)
                for grant in grants
                for parent in PurePosixPath(path).parents
            )
        }
    )
    return grants, covered


def registered_repository_roots(
    manifest: Manifest,
    *,
    project_id: str,
) -> list[RegisteredRepositoryRoot]:
    """Project-owned repository roots in deterministic catalog-inventory form."""

    roots = [
        RegisteredRepositoryRoot(
            project_id=project_id,
            alias=repository.alias,
            machine=repository.machine,
            execution_host=manifest.machine_map[repository.machine].host,
            path=repository.path,
        )
        for repository in manifest.repositories
    ]
    return sorted(
        roots,
        key=lambda item: (
            item.execution_host,
            item.project_id,
            item.alias,
            item.machine,
            item.path,
        ),
    )


def _canonical_directories(
    paths: list[str],
    *,
    remote_stage: RemoteRunStage | None,
    require_writable: bool,
) -> tuple[dict[str, str], str]:
    declared = list(dict.fromkeys(paths))
    if remote_stage is not None:
        return remote_stage.canonical_directories(declared, require_writable=require_writable)
    canonical: dict[str, str] = {}
    for raw in declared:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"project repository root must be absolute: {raw}")
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ValueError(f"project repository root is unavailable: {raw}") from exc
        if not resolved.is_dir():
            raise ValueError(f"project repository root is not a directory: {raw}")
        if require_writable and not os.access(resolved, os.W_OK):
            raise ValueError(f"project repository root is not writable: {raw}")
        canonical[raw] = str(resolved)
    return canonical, str(Path.home().resolve())


@dataclass(frozen=True)
class _ExecutionPathSemantics:
    """One answer for path identity on the filesystem doing the execution."""

    remote: bool

    @classmethod
    def for_execution(cls, *, remote: bool) -> _ExecutionPathSemantics:
        return cls(remote=remote)

    def normalized(self, value: str | Path) -> PurePosixPath:
        return PurePosixPath(os.path.normpath(os.fspath(value)))

    def equal(self, left: str | Path, right: str | Path) -> bool:
        if not self.remote:
            try:
                return os.path.samefile(left, right)
            except OSError:
                pass
        return self.normalized(left) == self.normalized(right)

    def overlaps(self, left: str | Path, right: str | Path) -> bool:
        if not self.remote:
            left_path = Path(left)
            right_path = Path(right)
            if self.equal(left_path, right_path):
                return True
            return any(self.equal(parent, right_path) for parent in left_path.parents) or any(
                self.equal(left_path, parent) for parent in right_path.parents
            )
        left_path = self.normalized(left)
        right_path = self.normalized(right)
        return (
            left_path == right_path
            or left_path in right_path.parents
            or right_path in left_path.parents
        )


def _reject_broad_repository_root(
    root: str,
    *,
    account_home: str,
    app_data_dir: Path | None,
    path_semantics: _ExecutionPathSemantics,
) -> None:
    broad_temporary_roots = {
        PurePosixPath("/tmp"),
        PurePosixPath("/private/tmp"),
        PurePosixPath(str(Path(tempfile.gettempdir()).resolve())),
    }
    if path_semantics.equal(root, "/"):
        raise ValueError("the filesystem root cannot be a project repository write root")
    if path_semantics.equal(root, account_home):
        raise ValueError("the execution account home cannot be a project repository write root")
    if any(path_semantics.equal(root, candidate) for candidate in broad_temporary_roots):
        raise ValueError("a broad temporary directory cannot be a project repository write root")
    if app_data_dir is None:
        return
    data_root = app_data_dir.expanduser().resolve()
    if path_semantics.overlaps(root, data_root):
        raise ValueError("the RCP application data directory cannot be a repository write root")


def _reject_repository_ownership_overlap(
    *,
    repository: RepositoryConfig,
    project_id: str,
    admitted_owners: set[tuple[str, str, str, str]],
    registered_root: str,
    inventory: list[RegisteredRepositoryRoot],
    canonical_inventory: dict[str, str],
    path_semantics: _ExecutionPathSemantics,
) -> None:
    alias = repository.alias
    declared_root = repository.path
    for owner in inventory:
        is_admitted_owner = (
            owner.project_id,
            owner.alias,
            owner.machine,
            owner.path,
        ) in admitted_owners
        if is_admitted_owner:
            continue
        if not (
            path_semantics.overlaps(declared_root, owner.path)
            or path_semantics.overlaps(registered_root, canonical_inventory[owner.path])
        ):
            continue
        relation = (
            "another project" if owner.project_id != project_id else "an unadmitted repository"
        )
        raise ValueError(
            f"repository {alias!r} overlaps {relation} on this execution host: "
            f"{owner.project_id}/{owner.alias}"
        )


def _without_transient(payload: dict[str, object], transient: list[str]) -> dict[str, object]:
    if not transient:
        return payload
    dropped = set(transient)
    result = dict(payload)
    for key in ("protected_write_paths", "granted_protected_paths"):
        if key in result:
            kept = [path for path in result[key] if path not in dropped]  # type: ignore[union-attr]
            if kept or key == "protected_write_paths":
                result[key] = kept
            else:
                del result[key]
    return result


def _scope_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
