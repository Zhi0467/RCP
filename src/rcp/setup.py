from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

import tomlkit
from pydantic import BaseModel, Field, model_validator

from rcp.agents import AgentLauncher, ProviderReadiness
from rcp.config import (
    GRAPH_AGENT_SURFACES,
    AgentSurface,
    Manifest,
    load_manifest,
    permissions_for,
)
from rcp.projects import ProjectCatalog
from rcp.providers import DEFAULT_PROVIDER, PROVIDER_IDS, ProviderId
from rcp.transport.ssh import ssh_arguments


class SetupRepository(BaseModel):
    alias: str
    location: Literal["local", "ssh"]
    path: str
    host: str = ""
    default_read: bool = True

    @model_validator(mode="after")
    def validate_location(self) -> SetupRepository:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", self.alias):
            raise ValueError(
                "repository aliases must start with a letter and use lowercase letters, "
                "numbers, or hyphens"
            )
        if self.location == "local":
            self.host = ""
            path = Path(self.path).expanduser()
            if not path.is_absolute() or path == Path("/"):
                raise ValueError(f"local repository {self.alias} needs a specific absolute path")
            self.path = str(path)
        else:
            if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", self.host):
                raise ValueError(f"remote repository {self.alias} needs a valid SSH host")
            path = PurePosixPath(self.path)
            if not path.is_absolute() or str(path) == "/":
                raise ValueError(f"remote repository {self.alias} needs a specific absolute path")
            self.path = str(path)
        return self


class SetupExecution(BaseModel):
    location: Literal["local", "ssh"] = "local"
    host: str = ""

    @model_validator(mode="after")
    def validate_host(self) -> SetupExecution:
        if self.location == "local":
            self.host = ""
        elif not re.fullmatch(r"[A-Za-z0-9_.@:-]+", self.host):
            raise ValueError("remote execution needs a valid SSH host")
        return self


class SetupAgentProfile(BaseModel):
    provider: ProviderId = DEFAULT_PROVIDER
    model: str = ""
    reasoning: str = "medium"
    location: Literal["local", "ssh"] = "local"
    host: str = ""

    @model_validator(mode="after")
    def validate_host(self) -> SetupAgentProfile:
        if self.location == "local":
            self.host = ""
        elif not re.fullmatch(r"[A-Za-z0-9_.@:-]+", self.host):
            raise ValueError("remote agent execution needs a valid SSH host")
        return self


class SetupAgents(BaseModel):
    seed: SetupAgentProfile = Field(default_factory=SetupAgentProfile)
    refresh: SetupAgentProfile = Field(default_factory=SetupAgentProfile)
    node_chat: SetupAgentProfile = Field(default_factory=SetupAgentProfile)
    project_chat: SetupAgentProfile = Field(default_factory=SetupAgentProfile)
    paper_coach: SetupAgentProfile = Field(
        default_factory=lambda: SetupAgentProfile(model="gpt-5.6-luna")
    )

    def profile(self, surface: AgentSurface) -> SetupAgentProfile:
        return getattr(self, surface)


class ProjectSetupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    repositories: list[SetupRepository] = Field(min_length=1)
    state_repository: str
    execution: SetupExecution = Field(default_factory=SetupExecution)
    agents: SetupAgents | None = None
    confirmed: bool = False

    @model_validator(mode="after")
    def validate_project(self) -> ProjectSetupRequest:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("project name cannot be blank")
        aliases = [repository.alias for repository in self.repositories]
        if len(aliases) != len(set(aliases)):
            raise ValueError("repository aliases must be unique")
        if self.state_repository not in aliases:
            raise ValueError("canonical state must name one of the project repositories")
        if not any(repository.default_read for repository in self.repositories):
            raise ValueError("select at least one repository for default agent reads")
        locations = [
            (repository.location, repository.host, repository.path)
            for repository in self.repositories
        ]
        if len(locations) != len(set(locations)):
            raise ValueError("the same repository path cannot be added twice")
        remote_hosts = {
            repository.host for repository in self.repositories if repository.location == "ssh"
        }
        if self.agents is None:
            canonical = next(
                repository
                for repository in self.repositories
                if repository.alias == self.state_repository
            )
            graph_profile = SetupAgentProfile(
                location=canonical.location,
                host=canonical.host,
            )
            self.agents = SetupAgents(
                seed=graph_profile.model_copy(),
                refresh=graph_profile.model_copy(),
                node_chat=graph_profile.model_copy(),
                project_chat=graph_profile.model_copy(),
            )
        canonical = next(
            repository
            for repository in self.repositories
            if repository.alias == self.state_repository
        )
        for surface in GRAPH_AGENT_SURFACES:
            profile = self.agents.profile(surface)
            if (profile.location, profile.host) != (canonical.location, canonical.host):
                target = canonical.host or "this machine"
                raise ValueError(
                    f"{surface.replace('_', ' ')} must run beside canonical state on {target}"
                )
        for surface in _SETUP_AGENT_SURFACES:
            profile = self.agents.profile(surface)
            if profile.location == "ssh" and profile.host not in remote_hosts:
                raise ValueError(
                    f"{surface.replace('_', ' ')} must run on a host that owns a project repository"
                )
        return self


class SetupCheck(BaseModel):
    label: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class SetupPreview(BaseModel):
    checks: list[SetupCheck]
    can_create: bool
    action: Literal["create", "connect"]
    canonical_location: str
    existing_project_name: str | None = None
    manifest_preview: str
    remote_write: bool
    providers: dict[str, ProviderReadiness]
    agent_readiness: dict[str, ProviderReadiness]


_SETUP_AGENT_SURFACES: tuple[AgentSurface, ...] = (
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
)


class ProjectSetupManager:
    def __init__(
        self,
        data_dir: Path,
        catalog: ProjectCatalog,
        launcher: AgentLauncher,
    ) -> None:
        self.data_dir = data_dir
        self.catalog = catalog
        self.launcher = launcher

    def preflight(self, request: ProjectSetupRequest) -> SetupPreview:
        checks = [self._check_repository(repository) for repository in request.repositories]
        canonical = self._repository(request, request.state_repository)
        existing_content = self._read_existing_manifest(canonical)
        existing_name: str | None = None
        action: Literal["create", "connect"] = "create"
        if existing_content is not None:
            action = "connect"
            try:
                existing_name = self._validate_existing_manifest(canonical, existing_content)
                checks.append(
                    SetupCheck(
                        label="Canonical manifest",
                        status="pass",
                        detail=(
                            f"Found existing RCP project “{existing_name}”. Its configuration "
                            "will be connected without being overwritten."
                        ),
                    )
                )
            except ValueError as exc:
                checks.append(
                    SetupCheck(
                        label="Canonical manifest",
                        status="fail",
                        detail=str(exc),
                    )
                )
        else:
            checks.append(self._check_canonical_writable(canonical))

        assert request.agents is not None
        readiness_cache: dict[tuple[str, str], ProviderReadiness] = {}
        machine_hosts = {
            repository.host if repository.location == "ssh" else ""
            for repository in request.repositories
        }
        machine_hosts.update(
            request.agents.profile(surface).host
            if request.agents.profile(surface).location == "ssh"
            else ""
            for surface in _SETUP_AGENT_SURFACES
        )
        for host in sorted(machine_hosts):
            for provider in PROVIDER_IDS:
                readiness_cache[(provider, host)] = self.launcher.readiness(
                    provider,
                    host=host,
                )
        agent_readiness: dict[str, ProviderReadiness] = {}
        for surface in _SETUP_AGENT_SURFACES:
            profile = request.agents.profile(surface)
            host = profile.host if profile.location == "ssh" else ""
            key = (profile.provider, host)
            if key not in readiness_cache:
                readiness_cache[key] = self.launcher.readiness(profile.provider, host=host)
            readiness = readiness_cache[key]
            agent_readiness[surface] = readiness
            checks.append(
                SetupCheck(
                    label=f"{surface.replace('_', ' ').title()} agent",
                    status="pass" if readiness.authenticated else "warn",
                    detail=(
                        f"{readiness.version or profile.provider.title()} is installed and "
                        f"authenticated on {host or 'this machine'}."
                        if readiness.authenticated
                        else readiness.reason or f"{profile.provider.title()} is unavailable."
                    ),
                )
            )
        if request.agents.paper_coach.location == "ssh":
            checks.append(
                SetupCheck(
                    label="Paper coach session resume",
                    status="warn",
                    detail=(
                        "The paper coach can be configured remotely, but v1 native-session "
                        "resume requires a local execution machine."
                    ),
                )
            )
        execution_host = request.execution.host if request.execution.location == "ssh" else ""
        providers = {
            provider: readiness_cache.get((provider, execution_host))
            or self.launcher.readiness(provider, host=execution_host)
            for provider in PROVIDER_IDS
        }

        provider_paths = {
            host: {
                provider: readiness.binary_path
                for provider in PROVIDER_IDS
                if (readiness := readiness_cache[(provider, host)]).installed
                and readiness.binary_path
            }
            for host in machine_hosts
        }
        manifest = existing_content or render_manifest(request, provider_paths)
        return SetupPreview(
            checks=checks,
            can_create=not any(check.status == "fail" for check in checks),
            action=action,
            canonical_location=_canonical_location(canonical),
            existing_project_name=existing_name,
            manifest_preview=manifest,
            remote_write=canonical.location == "ssh",
            providers=providers,
            agent_readiness=agent_readiness,
        )

    def create(self, request: ProjectSetupRequest) -> dict[str, object]:
        if not request.confirmed:
            raise ValueError("project creation requires final human confirmation")
        preview = self.preflight(request)
        if not preview.can_create:
            failures = [check.detail for check in preview.checks if check.status == "fail"]
            raise ValueError("; ".join(failures))

        canonical = self._repository(request, request.state_repository)
        existing_content = self._read_existing_manifest(canonical)
        if canonical.location == "local":
            manifest_path = Path(canonical.path) / ".research" / "manifest.toml"
            if existing_content is None:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                _exclusive_write(manifest_path, preview.manifest_preview)
            locator = str(manifest_path)
        else:
            content = existing_content or preview.manifest_preview
            locator = str(self._write_bootstrap(canonical, content))

        record = self.catalog.register(locator)
        _, snapshot = self.catalog.open_snapshot(record.project_id)
        self.catalog.update_summary(record.project_id, snapshot)
        return self.catalog.card(record.project_id)

    @staticmethod
    def _repository(request: ProjectSetupRequest, alias: str) -> SetupRepository:
        return next(repository for repository in request.repositories if repository.alias == alias)

    @staticmethod
    def _check_repository(repository: SetupRepository) -> SetupCheck:
        if repository.location == "local":
            path = Path(repository.path)
            if not path.is_dir():
                return SetupCheck(
                    label=repository.alias,
                    status="fail",
                    detail=f"Local directory does not exist: {path}",
                )
            return SetupCheck(
                label=repository.alias,
                status="pass",
                detail=f"Local repository is reachable at {path}.",
            )
        result = _ssh(repository.host, ["test", "-d", repository.path])
        if result.returncode:
            detail = result.stderr.strip() or f"Remote directory does not exist: {repository.path}"
            return SetupCheck(label=repository.alias, status="fail", detail=detail)
        return SetupCheck(
            label=repository.alias,
            status="pass",
            detail=f"SSH reached {repository.host}:{repository.path}.",
        )

    @staticmethod
    def _read_existing_manifest(repository: SetupRepository) -> str | None:
        if repository.location == "local":
            path = Path(repository.path) / ".research" / "manifest.toml"
            return path.read_text(encoding="utf-8") if path.is_file() else None
        path = str(PurePosixPath(repository.path) / ".research" / "manifest.toml")
        result = _ssh(repository.host, ["cat", path])
        if result.returncode == 0:
            return result.stdout
        exists = _ssh(repository.host, ["test", "-f", path])
        if exists.returncode == 1:
            return None
        raise ValueError(result.stderr.strip() or "Could not read the remote manifest")

    @staticmethod
    def _validate_existing_manifest(repository: SetupRepository, content: str) -> str:
        try:
            if repository.location == "local":
                manifest = load_manifest(Path(repository.path) / ".research" / "manifest.toml")
            else:
                data = tomlkit.parse(content).unwrap()
                manifest = Manifest.model_validate(data)
        except (ValueError, tomlkit.exceptions.ParseError) as exc:
            raise ValueError(f"Existing manifest is invalid: {exc}") from exc
        state = manifest.repository_map[manifest.state.repository]
        machine = manifest.machine_map[state.machine]
        expected_host = repository.host if repository.location == "ssh" else ""
        expected_path = str(
            PurePosixPath(repository.path)
            if repository.location == "ssh"
            else Path(repository.path).resolve()
        )
        actual_path = str(
            PurePosixPath(state.path) if expected_host else Path(state.path).expanduser().resolve()
        )
        if machine.host != expected_host or actual_path != expected_path:
            raise ValueError(
                "Existing manifest points to a different canonical repository; "
                "RCP will not relabel or overwrite it."
            )
        return manifest.name

    @staticmethod
    def _check_canonical_writable(repository: SetupRepository) -> SetupCheck:
        if repository.location == "local":
            research_dir = Path(repository.path) / ".research"
            if research_dir.exists() and not research_dir.is_dir():
                return SetupCheck(
                    label="Canonical state write",
                    status="fail",
                    detail=f"Canonical state path is not a directory: {research_dir}",
                )
            target = research_dir if research_dir.is_dir() else Path(repository.path)
            writable = os.access(target, os.W_OK)
        else:
            research_dir = str(PurePosixPath(repository.path) / ".research")
            exists = _ssh(repository.host, ["test", "-e", research_dir])
            if exists.returncode == 0:
                directory = _ssh(repository.host, ["test", "-d", research_dir])
                if directory.returncode:
                    return SetupCheck(
                        label="Canonical state write",
                        status="fail",
                        detail=f"Canonical state path is not a directory: {_canonical_location(repository)}",
                    )
                target = research_dir
            else:
                target = repository.path
            result = _ssh(repository.host, ["test", "-w", target])
            writable = result.returncode == 0
        return SetupCheck(
            label="Canonical state write",
            status="pass" if writable else "fail",
            detail=(
                f"RCP can create .research/ at {_canonical_location(repository)}."
                if writable
                else f"RCP cannot write to {_canonical_location(repository)}."
            ),
        )

    def _write_bootstrap(self, canonical: SetupRepository, content: str) -> Path:
        digest = hashlib.sha256(f"{canonical.host}\0{canonical.path}".encode()).hexdigest()[:16]
        path = self.data_dir / "bootstrap-manifests" / f"{digest}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
        return path


def render_manifest(
    request: ProjectSetupRequest,
    provider_paths: dict[str, dict[str, str]] | None = None,
) -> str:
    assert request.agents is not None
    document = tomlkit.document()
    document.add("name", request.name)

    host_aliases: dict[str, str] = {}
    needs_local = any(
        repository.location == "local" for repository in request.repositories
    ) or any(
        request.agents.profile(surface).location == "local"
        for surface in _SETUP_AGENT_SURFACES
    )
    machines = tomlkit.aot()
    if needs_local:
        host_aliases[""] = "laptop"
        machine = tomlkit.table()
        machine.add("alias", "laptop")
        machine.add("host", "")
        _add_provider_paths(machine, (provider_paths or {}).get("", {}))
        machines.append(machine)
    remote_hosts = sorted(
        {repository.host for repository in request.repositories if repository.location == "ssh"}
    )
    for index, host in enumerate(remote_hosts, start=1):
        alias = f"remote-{index}"
        host_aliases[host] = alias
        machine = tomlkit.table()
        machine.add("alias", alias)
        machine.add("host", host)
        _add_provider_paths(machine, (provider_paths or {}).get(host, {}))
        machines.append(machine)
    document.add("machines", machines)

    repositories = tomlkit.aot()
    for item in request.repositories:
        repository = tomlkit.table()
        repository.add("alias", item.alias)
        repository.add("machine", host_aliases[item.host])
        repository.add("path", item.path)
        repositories.append(repository)
    document.add("repositories", repositories)

    project = tomlkit.table()
    project.add("truth_scope", [repository.alias for repository in request.repositories])
    document.add("project", project)

    state = tomlkit.table()
    state.add("repository", request.state_repository)
    document.add("state", state)

    agent = tomlkit.table()
    agent.add(
        "default_run_truth_scope",
        [repository.alias for repository in request.repositories if repository.default_read],
    )
    for surface in _SETUP_AGENT_SURFACES:
        setup_profile = request.agents.profile(surface)
        profile = tomlkit.table()
        profile.add("provider", setup_profile.provider)
        profile.add("model", setup_profile.model)
        profile.add("reasoning", setup_profile.reasoning)
        profile.add("run_on", host_aliases[setup_profile.host])
        permissions = tomlkit.table()
        for key, value in permissions_for(surface).model_dump(mode="json").items():
            permissions.add(key, value)
        profile.add("permissions", permissions)
        agent.add(surface, profile)
    document.add("agent", agent)

    sources = tomlkit.table()
    sources.add("claude_roots", ["~/.claude/projects"])
    sources.add("codex_roots", ["~/.codex/sessions"])
    sources.add("remote_claude_roots", ["~/.claude/projects"])
    sources.add("remote_codex_roots", ["~/.codex/sessions"])
    document.add("sources", sources)

    return tomlkit.dumps(document)


def _add_provider_paths(machine: tomlkit.items.Table, paths: dict[str, str]) -> None:
    if not paths:
        return
    provider_paths = tomlkit.inline_table()
    for provider in PROVIDER_IDS:
        path = paths.get(provider)
        if path:
            provider_paths.append(provider, path)
    if provider_paths:
        machine.add("provider_paths", provider_paths)


def _canonical_location(repository: SetupRepository) -> str:
    return (
        f"{repository.host}:{repository.path}/.research"
        if repository.location == "ssh"
        else str(Path(repository.path) / ".research")
    )


def _ssh(host: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    command = shlex.join(arguments)
    try:
        return subprocess.run(
            ssh_arguments(host, command),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess([], 255, "", str(exc))


def _exclusive_write(path: Path, content: str) -> None:
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValueError(
            f"A manifest appeared at {path} after preflight; review it before connecting."
        ) from exc
