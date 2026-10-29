from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from rcp.compute_jobs.text import COMPUTE_CREDENTIAL_PATH, validate_compute_metadata
from rcp.providers import (
    DEFAULT_PROVIDER,
    AgentCapability,
    ProviderId,
    configured_runtime,
)
from rcp.server_ops.models import redact_server_text
from rcp.skill_registry import SkillDefaults
from rcp.ssh_validation import validate_ssh_destination

DEFAULT_AUTO_RESEARCH_INVOCATION_CEILING = 10
#: A retired top-level manifest table. Loads ignore it; every manifest write drops it.
LEGACY_COMPUTE_CONNECTIONS_KEY = "compute_connections"


def require_base_machine(run_on: str, base_machine: str, *, label: str = "agent.run_on") -> str:
    """Refuse an agent machine other than the base; every agent runs beside .research/."""
    if run_on != base_machine:
        raise ValueError(
            f"{label} must be the canonical state machine {base_machine!r}; "
            "every agent runs beside .research/"
        )
    return base_machine


class MachineComputeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_manager: Literal["slurm"] | None = None
    jobs_root: str = ""

    @model_validator(mode="after")
    def validate_compute(self) -> MachineComputeConfig:
        validate_compute_metadata(self.jobs_root)
        if self.jobs_root and not PurePosixPath(self.jobs_root).is_absolute():
            raise ValueError("compute jobs_root must be absolute when configured")
        return self


class MachineConfig(BaseModel):
    """One space machine a project uses, named by the project's alias.

    The machine's settings live on its space card (`rcp.storage.space_machines`).
    The `legacy_*` fields read what older builds stored here; they are validated
    and copied into the card once (`rcp.projects.record_space_machines`), and
    nothing else reads them.
    """

    alias: str
    host: str = ""
    os_account: str = ""
    legacy_provider_paths: dict[ProviderId, str] = Field(
        default_factory=dict, alias="provider_paths"
    )
    legacy_compute: MachineComputeConfig | None = Field(default=None, alias="compute")
    legacy_access_hint: str = Field(default="", max_length=512, alias="access_hint")

    @model_validator(mode="after")
    def validate_host(self) -> MachineConfig:
        self.host = self.host.strip()
        if self.host:
            validate_ssh_destination(self.host)
        return self

    @model_validator(mode="after")
    def validate_access_hint(self) -> MachineConfig:
        self.legacy_access_hint = validate_machine_access_hint(self.legacy_access_hint)
        return self

    @model_validator(mode="after")
    def validate_provider_paths(self) -> MachineConfig:
        self.legacy_provider_paths = validate_machine_provider_paths(
            self.legacy_provider_paths, host=self.host
        )
        return self

    @model_validator(mode="after")
    def validate_os_account(self) -> MachineConfig:
        if (
            self.os_account
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", self.os_account) is None
        ):
            raise ValueError("machine operating-system account is invalid")
        return self


def validate_machine_access_hint(value: str) -> str:
    """One line of metadata every agent reads, never a credential channel."""

    hint = value.strip()
    if len(hint) > 512:
        raise ValueError("machine access hint must be at most 512 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in hint):
        raise ValueError("machine access hint must be one line")
    if redact_server_text(hint) != hint:
        raise ValueError("machine access hint cannot contain credential-shaped text")
    if COMPUTE_CREDENTIAL_PATH.search(hint):
        raise ValueError("machine access hint cannot contain SSH credential paths")
    return hint


def validate_machine_provider_paths(
    paths: dict[ProviderId, str], *, host: str
) -> dict[ProviderId, str]:
    """Strip each recorded executable, drop empty ones, and require absolute paths."""

    normalized: dict[ProviderId, str] = {}
    for provider, raw_path in paths.items():
        path = raw_path.strip()
        if not path:
            continue
        absolute = PurePosixPath(path).is_absolute() if host else Path(path).is_absolute()
        if not absolute:
            target = host or "the local machine"
            raise ValueError(f"provider path for {provider!r} on {target} must be absolute")
        normalized[provider] = path
    return normalized


class ResolvedComputeProfile(BaseModel):
    """One immutable, non-secret project machine as seen from an agent's machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # A machine's alias; any alias and host the manifest accepts resolves.
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: Literal["local", "ssh"]
    ssh_target: str = ""
    access_hint: str = Field(default="", max_length=512)
    job_manager: Literal["", "slurm"] = ""

    @model_validator(mode="before")
    @classmethod
    def strip_metadata(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: item.strip() if key in _PROFILE_TEXT_FIELDS and isinstance(item, str) else item
                for key, item in value.items()
            }
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> ResolvedComputeProfile:
        values = (self.name, self.ssh_target, self.access_hint)
        if any(ord(character) < 32 or ord(character) == 127 for v in values for character in v):
            raise ValueError("compute metadata must be one line")
        # These fields are metadata, not credential channels. Reuse the server's
        # bounded redactor as a validator so credential-shaped text is refused
        # before it reaches a provider prompt.
        if any(redact_server_text(value) != value for value in values):
            raise ValueError("compute metadata cannot contain credential-shaped text")
        if any(COMPUTE_CREDENTIAL_PATH.search(value) for value in values):
            raise ValueError("compute metadata cannot contain SSH credential paths")
        if not self.name:
            raise ValueError("compute profile name must not be blank")
        if self.kind == "local":
            if self.ssh_target:
                raise ValueError("local compute cannot define an SSH target")
        elif not self.ssh_target:
            raise ValueError("remote compute requires an SSH target")
        else:
            validate_ssh_destination(self.ssh_target)
        return self


_PROFILE_TEXT_FIELDS = frozenset({"name", "ssh_target", "access_hint"})


class ResolvedComputeContext(BaseModel):
    """The bounded active compute set frozen into one admitted task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active: tuple[ResolvedComputeProfile, ...] = ()

    @model_validator(mode="after")
    def active_ids_are_unique(self) -> ResolvedComputeContext:
        ids = [profile.id for profile in self.active]
        if len(ids) != len(set(ids)):
            raise ValueError("resolved compute context cannot repeat a connection")
        return self


class RepositoryConfig(BaseModel):
    alias: str
    machine: str
    path: str


class ProjectConfig(BaseModel):
    truth_scope: list[str]


class StateConfig(BaseModel):
    repository: str


AgentSurface = Literal["seed", "refresh", "node_chat", "project_chat", "paper_coach"]
AgentExecutionProfile = AgentSurface | Literal["orchestrator"]


class AgentPermissions(BaseModel):
    read_graph: bool
    read_research_md: bool
    read_introduction: bool
    read_repositories: Literal["none", "run_scope", "project_scope"]
    read_conversations: Literal["none", "run_scope"]
    write_graph_patch: bool
    write_project_files: bool = False
    write_paper: bool = False


class AgentSurfaceConfig(BaseModel):
    provider: ProviderId = DEFAULT_PROVIDER
    runtime: str = ""
    model: str = ""
    reasoning: str = "medium"
    run_on: str
    permissions: AgentPermissions | None = None

    @model_validator(mode="after")
    def validate_provider_runtime(self) -> AgentSurfaceConfig:
        self.runtime = configured_runtime(self.provider, self.runtime)
        return self


class AgentConfig(BaseModel):
    default_run_truth_scope: list[str]
    default_auto_research_invocation_ceiling: int = Field(
        default=DEFAULT_AUTO_RESEARCH_INVOCATION_CEILING,
        ge=1,
        description="Operational invocations per newly authorized episode.",
    )
    skill_defaults: SkillDefaults = Field(default_factory=SkillDefaults)
    seed: AgentSurfaceConfig | None = None
    refresh: AgentSurfaceConfig | None = None
    node_chat: AgentSurfaceConfig | None = None
    project_chat: AgentSurfaceConfig | None = None
    paper_coach: AgentSurfaceConfig | None = None
    orchestrator: AgentSurfaceConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_auto_research_ceiling(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_key = "default_campaign_invocation_ceiling"
        current_key = "default_auto_research_invocation_ceiling"
        if legacy_key not in migrated:
            return migrated
        if current_key in migrated and migrated[current_key] != migrated[legacy_key]:
            raise ValueError(f"agent.{legacy_key} conflicts with agent.{current_key}")
        migrated.setdefault(current_key, migrated[legacy_key])
        del migrated[legacy_key]
        return migrated


class SourcesConfig(BaseModel):
    claude_roots: list[str] = Field(default_factory=lambda: ["~/.claude/projects"])
    codex_roots: list[str] = Field(default_factory=lambda: ["~/.codex/sessions"])
    remote_claude_roots: list[str] = Field(default_factory=lambda: ["~/.claude/projects"])
    remote_codex_roots: list[str] = Field(default_factory=lambda: ["~/.codex/sessions"])


class ExecutionConfig(BaseModel):
    run_on: str


class PaperCoachConfig(BaseModel):
    default_provider: ProviderId = DEFAULT_PROVIDER
    default_model: str = ""
    default_reasoning: str = "medium"


class Manifest(BaseModel):
    name: str
    machines: list[MachineConfig]
    repositories: list[RepositoryConfig]
    project: ProjectConfig
    state: StateConfig
    agent: AgentConfig
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    execution: ExecutionConfig | None = None
    paper: dict[str, Any] = Field(default_factory=dict)
    _path: Path = PrivateAttr()

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_compute_connections(cls, value: Any) -> Any:
        # Compute connections were retired when every project machine became a
        # compute resource. An older manifest still loads; the next write drops it.
        if isinstance(value, dict) and LEGACY_COMPUTE_CONNECTIONS_KEY in value:
            value = {k: v for k, v in value.items() if k != LEGACY_COMPUTE_CONNECTIONS_KEY}
        return value

    @model_validator(mode="after")
    def validate_references(self) -> Manifest:
        machine_aliases = [item.alias for item in self.machines]
        repository_aliases = [item.alias for item in self.repositories]
        if len(machine_aliases) != len(set(machine_aliases)):
            raise ValueError("machine aliases must be unique")
        if len(repository_aliases) != len(set(repository_aliases)):
            raise ValueError("repository aliases must be unique")
        machines = set(machine_aliases)
        repositories = set(repository_aliases)
        missing_machines = {item.machine for item in self.repositories} - machines
        if missing_machines:
            raise ValueError(f"repositories use unknown machines: {sorted(missing_machines)}")
        unknown_scope = set(self.project.truth_scope) - repositories
        if unknown_scope:
            raise ValueError(
                f"project truth scope uses unknown repositories: {sorted(unknown_scope)}"
            )
        if self.state.repository not in repositories:
            raise ValueError("state.repository must name a registered repository")
        if self.state.repository not in self.project.truth_scope:
            raise ValueError("state.repository must remain in project.truth_scope in v1")
        default_outside = set(self.agent.default_run_truth_scope) - set(self.project.truth_scope)
        if default_outside or not self.agent.default_run_truth_scope:
            raise ValueError("agent.default_run_truth_scope must be a non-empty project subset")
        legacy_execution = self.execution
        if legacy_execution is None and any(
            getattr(self.agent, surface) is None for surface in _AGENT_SURFACES
        ):
            raise ValueError(
                "agent profiles must all be configured when legacy [execution] is absent"
            )
        legacy_coach = PaperCoachConfig.model_validate(self.paper.get("coach", {}))
        state_machine = self.base_machine
        for surface in _AGENT_SURFACES:
            profile = getattr(self.agent, surface)
            if profile is None:
                assert legacy_execution is not None
                profile = AgentSurfaceConfig(
                    provider=(
                        legacy_coach.default_provider if surface == "paper_coach" else "codex"
                    ),
                    model=legacy_coach.default_model if surface == "paper_coach" else "",
                    reasoning=(
                        legacy_coach.default_reasoning if surface == "paper_coach" else "medium"
                    ),
                    run_on=legacy_execution.run_on,
                )
                setattr(self.agent, surface, profile)
        if self.agent.orchestrator is None:
            refresh = self.agent.refresh
            assert refresh is not None
            self.agent.orchestrator = refresh.model_copy(
                deep=True,
                update={"permissions": permissions_for("orchestrate")},
            )
        coach = self.agent.paper_coach
        assert coach is not None
        # The coach once chose its own machine. Every agent now runs on the base,
        # the canonical state machine, so an older choice is dropped on load.
        coach.run_on = state_machine
        for surface in AGENT_EXECUTION_PROFILES:
            profile = getattr(self.agent, surface)
            assert profile is not None
            if profile.run_on not in machines:
                raise ValueError(f"agent.{surface}.run_on must name a registered machine")
            profile.run_on = require_base_machine(
                profile.run_on, state_machine, label=f"agent.{surface}.run_on"
            )
            expected = permissions_for(surface)
            if profile.permissions is None:
                profile.permissions = expected
            elif surface in {
                "node_chat",
                "project_chat",
            } and profile.permissions == permissions_for("scratch_patch"):
                # Before conversation modes, chat surfaces carried the same
                # patch permission as ingestion. Accept that one exact legacy
                # contract, but normalize it to Discuss in memory so a saved
                # manifest cannot silently authorize Work.
                profile.permissions = expected
            elif profile.permissions != expected:
                raise ValueError(
                    f"agent.{surface}.permissions cannot widen or narrow the "
                    f"{surface.replace('_', ' ')} safety contract"
                )
        return self

    @property
    def path(self) -> Path:
        return self._path

    @property
    def research_dir(self) -> Path:
        return self._path.parent

    @property
    def repository_map(self) -> dict[str, RepositoryConfig]:
        return {item.alias: item for item in self.repositories}

    @property
    def base_machine(self) -> str:
        return self.repository_map[self.state.repository].machine

    @property
    def machine_map(self) -> dict[str, MachineConfig]:
        return {item.alias: item for item in self.machines}

    @property
    def repository_paths(self) -> dict[str, str]:
        """Registered alias to its path, for reading a declared path back to its alias."""

        return {item.alias: item.path for item in self.repositories}

    @property
    def coach(self) -> PaperCoachConfig:
        profile = self.agent_profile("paper_coach")
        return PaperCoachConfig(
            default_provider=profile.provider,
            default_model=profile.model,
            default_reasoning=profile.reasoning,
        )

    def agent_profile(self, surface: AgentExecutionProfile) -> AgentSurfaceConfig:
        profile = getattr(self.agent, surface)
        assert profile is not None
        return profile


_AGENT_SURFACES: tuple[AgentSurface, ...] = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
)
AGENT_EXECUTION_PROFILES: tuple[AgentExecutionProfile, ...] = (
    *_AGENT_SURFACES,
    "orchestrator",
)


#: Surfaces whose turns can launch a Work-like capability. The map below gives a
#: chat surface's *default* capability, `discuss`, but a Work turn on that same
#: profile launches `work_auto`, so the default cannot answer this on its own.
#: Only these profiles are subject to a Work-like launch precondition.
_WORK_LIKE_SURFACES: frozenset[str] = frozenset({"node_chat", "project_chat", "orchestrator"})


def launches_work_like(surface: AgentExecutionProfile) -> bool:
    """Whether any turn on this profile can ask for `work_auto` or `orchestrate`."""

    return surface in _WORK_LIKE_SURFACES


def permissions_for(target: AgentExecutionProfile | AgentCapability) -> AgentPermissions:
    """Return the immutable authority envelope for one launch capability."""

    capability: AgentCapability
    if target in {"seed", "refresh"}:
        capability = "scratch_patch"
    elif target in {"node_chat", "project_chat"}:
        capability = "discuss"
    elif target == "paper_coach":
        capability = "paper_suggest"
    elif target == "discuss":
        capability = "discuss"
    elif target == "work_auto":
        capability = "work_auto"
    elif target in {"orchestrator", "orchestrate"}:
        capability = "orchestrate"
    elif target == "scratch_patch":
        capability = "scratch_patch"
    elif target == "paper_suggest":
        capability = "paper_suggest"
    else:
        raise ValueError(f"Unknown agent surface or capability: {target!r}")

    if capability == "paper_suggest":
        return AgentPermissions(
            read_graph=True,
            read_research_md=True,
            read_introduction=True,
            read_repositories="project_scope",
            read_conversations="none",
            write_graph_patch=False,
            write_project_files=False,
            write_paper=False,
        )
    if capability == "discuss":
        return AgentPermissions(
            read_graph=True,
            read_research_md=True,
            read_introduction=True,
            read_repositories="run_scope",
            read_conversations="run_scope",
            write_graph_patch=False,
            write_project_files=False,
            write_paper=False,
        )
    if capability in {"work_auto", "orchestrate"}:
        return AgentPermissions(
            read_graph=True,
            read_research_md=True,
            read_introduction=True,
            read_repositories="run_scope",
            read_conversations="run_scope",
            write_graph_patch=True,
            write_project_files=True,
            write_paper=False,
        )
    if capability == "scratch_patch":
        return AgentPermissions(
            read_graph=True,
            read_research_md=True,
            read_introduction=True,
            read_repositories="run_scope",
            read_conversations="run_scope",
            write_graph_patch=True,
            write_project_files=False,
            write_paper=False,
        )
    raise ValueError(f"Unknown agent capability: {capability!r}")


def resolve_manifest_path(
    value: str | os.PathLike[str],
    *,
    local_home: Path | None = None,
) -> Path:
    path = _expand_local_user_path(str(value), local_home=local_home).resolve()
    if path.is_file():
        return path
    direct = path / "manifest.toml"
    if direct.is_file():
        return direct
    nested = path / ".research" / "manifest.toml"
    if nested.is_file():
        return nested
    raise FileNotFoundError(f"No manifest.toml found at {path}")


def load_manifest(
    value: str | os.PathLike[str],
    *,
    local_home: Path | None = None,
    project_root: Path | None = None,
) -> Manifest:
    path = resolve_manifest_path(value, local_home=local_home)
    data = tomlkit.parse(path.read_text(encoding="utf-8"))
    manifest = Manifest.model_validate(data.unwrap())
    manifest._path = path
    project_root = path.parent.parent if project_root is None else project_root
    for repository in manifest.repositories:
        repository_path = _expand_local_user_path(repository.path, local_home=local_home)
        if not repository_path.is_absolute() and not manifest.machine_map[repository.machine].host:
            repository.path = str((project_root / repository_path).resolve())
    manifest.sources.claude_roots = [
        _resolve_local_source_root(project_root, root, local_home=local_home)
        for root in manifest.sources.claude_roots
    ]
    manifest.sources.codex_roots = [
        _resolve_local_source_root(project_root, root, local_home=local_home)
        for root in manifest.sources.codex_roots
    ]
    return manifest


def write_project_scope(
    manifest: Manifest,
    truth_scope: list[str],
    repository_descriptor: dict[str, str] | None = None,
) -> Manifest:
    content = _project_scope_content(manifest, truth_scope, repository_descriptor)
    _atomic_write(manifest.path, content)
    return load_manifest(manifest.path)


def validate_project_scope_update(
    manifest: Manifest,
    truth_scope: list[str],
    repository_descriptor: dict[str, str] | None = None,
) -> None:
    _project_scope_content(manifest, truth_scope, repository_descriptor)


def _project_scope_content(
    manifest: Manifest,
    truth_scope: list[str],
    repository_descriptor: dict[str, str] | None,
) -> str:
    if repository_descriptor and repository_descriptor.get("machine") not in manifest.machine_map:
        raise ValueError(
            f"repository uses unknown machine: {repository_descriptor.get('machine')!r}"
        )
    document = tomlkit.parse(manifest.path.read_text(encoding="utf-8"))
    if repository_descriptor:
        aliases = {item.get("alias") for item in document.get("repositories", [])}
        if repository_descriptor["alias"] not in aliases:
            repository = tomlkit.table()
            repository.add("alias", repository_descriptor["alias"])
            repository.add("machine", repository_descriptor["machine"])
            repository.add("path", repository_descriptor["path"])
            repositories = document.get("repositories")
            if repositories is None:
                repositories = tomlkit.aot()
                document.add("repositories", repositories)
            repositories.append(repository)
    document["project"]["truth_scope"] = list(truth_scope)
    document.pop(LEGACY_COMPUTE_CONNECTIONS_KEY, None)
    content = tomlkit.dumps(document)
    Manifest.model_validate(tomlkit.parse(content).unwrap())
    return content


def write_agent_settings(
    manifest: Manifest,
    default_run_truth_scope: list[str],
    profiles: dict[AgentExecutionProfile, AgentSurfaceConfig],
    skill_defaults: SkillDefaults | None = None,
    default_auto_research_invocation_ceiling: int | None = None,
) -> Manifest:
    document = tomlkit.parse(manifest.path.read_text(encoding="utf-8"))
    agent = document.get("agent")
    if agent is None:
        agent = tomlkit.table()
        document.add("agent", agent)
    agent["default_run_truth_scope"] = list(default_run_truth_scope)
    agent.pop("default_campaign_invocation_ceiling", None)
    agent["default_auto_research_invocation_ceiling"] = (
        manifest.agent.default_auto_research_invocation_ceiling
        if default_auto_research_invocation_ceiling is None
        else default_auto_research_invocation_ceiling
    )
    selected_defaults = skill_defaults or manifest.agent.skill_defaults
    defaults_table = tomlkit.table()
    defaults_table.add("workflow_ids", list(selected_defaults.workflow_ids))
    defaults_table.add("skill_ids", list(selected_defaults.skill_ids))
    agent["skill_defaults"] = defaults_table

    for surface in AGENT_EXECUTION_PROFILES:
        # A settings write is a merge: a client that predates a surface omits it,
        # and the omitted surface keeps what the manifest already holds rather
        # than failing the whole save.
        profile = profiles.get(surface) or getattr(manifest.agent, surface)
        if profile is None:
            raise ValueError(f"agent.{surface} has no configuration to write")
        table = tomlkit.table()
        table.add("provider", profile.provider)
        table.add("runtime", profile.runtime)
        table.add("model", profile.model)
        table.add("reasoning", profile.reasoning)
        table.add("run_on", profile.run_on)
        permission_table = tomlkit.table()
        for key, value in permissions_for(surface).model_dump(mode="json").items():
            permission_table.add(key, value)
        table.add("permissions", permission_table)
        agent[surface] = table

    if "execution" in document:
        del document["execution"]
    paper = document.get("paper")
    if paper is not None and "coach" in paper:
        del paper["coach"]
        if not paper:
            del document["paper"]

    document.pop(LEGACY_COMPUTE_CONNECTIONS_KEY, None)

    content = tomlkit.dumps(document)
    Manifest.model_validate(tomlkit.parse(content).unwrap())
    _atomic_write(manifest.path, content)
    return load_manifest(manifest.path)


def write_added_machine(manifest: Manifest, machine: MachineConfig) -> Manifest:
    """Append one execution machine; existing aliases and repositories stay as they are."""

    if machine.alias in manifest.machine_map:
        raise ValueError(f"this project already has a machine named {machine.alias}")
    if any(existing.host == machine.host for existing in manifest.machines):
        raise ValueError("this project already uses that machine")
    document = tomlkit.parse(manifest.path.read_text(encoding="utf-8"))
    table = tomlkit.table()
    table.add("alias", machine.alias)
    table.add("host", machine.host)
    table.add("os_account", machine.os_account)
    document.setdefault("machines", tomlkit.aot()).append(table)
    document.pop(LEGACY_COMPUTE_CONNECTIONS_KEY, None)
    content = tomlkit.dumps(document)
    Manifest.model_validate(tomlkit.parse(content).unwrap())
    _atomic_write(manifest.path, content)
    return load_manifest(manifest.path)


def write_removed_machine(manifest: Manifest, alias: str) -> Manifest:
    """Drop one machine the project no longer uses; its space card stays."""

    if alias not in manifest.machine_map:
        raise ValueError(f"unknown machine: {alias}")
    if any(repository.machine == alias for repository in manifest.repositories):
        raise ValueError(f"a repository lives on {alias}")
    document = tomlkit.parse(manifest.path.read_text(encoding="utf-8"))
    machines = document["machines"]
    for index, table in enumerate(machines):
        if str(table.get("alias")) == alias:
            del machines[index]
            break
    state_machine = manifest.base_machine
    for table in document.get("agent", {}).values():
        if isinstance(table, dict) and table.get("run_on") == alias:
            table["run_on"] = state_machine
    content = tomlkit.dumps(document)
    Manifest.model_validate(tomlkit.parse(content).unwrap())
    _atomic_write(manifest.path, content)
    return load_manifest(manifest.path)


def _atomic_write(path: Path, content: str) -> None:
    """Publish the manifest so a reader never sees a partial file.

    The bytes reach a same-directory temporary file, are fsynced, and replace
    the manifest in one rename; the directory is fsynced so the rename itself
    survives a crash. `tests/test_config.py::test_interrupted_manifest_write_leaves_the_manifest_intact`
    stops the write before the rename and expects the previous manifest.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _expand_local_user_path(value: str, *, local_home: Path | None) -> Path:
    if local_home is not None and (value == "~" or value.startswith("~/")):
        if not local_home.is_absolute():
            raise ValueError("local manifest home must be absolute")
        return local_home if value == "~" else local_home / value[2:]
    return Path(value).expanduser()


def _resolve_local_source_root(
    project_root: Path,
    value: str,
    *,
    local_home: Path | None,
) -> str:
    path = _expand_local_user_path(value, local_home=local_home)
    if path.is_absolute() or value.startswith("~"):
        return str(path)
    return str((project_root / path).resolve())
