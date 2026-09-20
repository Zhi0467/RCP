from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from rcp.agents.write_scope import RegisteredRepositoryRoot, protected_repository_paths
from rcp.config import Manifest
from rcp.terminals.models import TerminalSession, TerminalUnavailable
from rcp.transport.run_stage import RemoteRunStage


def resolve_repository(
    *,
    manifest: Manifest,
    project_id: str,
    repository_id: str,
    inventory: list[RegisteredRepositoryRoot],
    data_dir: Path,
    remote_stage: RemoteRunStage | None = None,
) -> tuple[Path, list[str]]:
    repository = manifest.repository_map.get(repository_id)
    if repository is None:
        raise ValueError("Repository is not registered to this project.")
    host = manifest.machine_map[repository.machine].host
    if host:
        if remote_stage is None or remote_stage.host != host:
            raise TerminalUnavailable("Remote repository resolution requires its execution host.")
        return _resolve_remote_repository(
            manifest, project_id, repository_id, inventory, remote_stage
        )
    root = Path(repository.path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise TerminalUnavailable("The registered repository is not a directory.")
    forbidden = [Path("/"), Path.home().resolve(), Path(tempfile.gettempdir()).resolve()]
    if root in forbidden or _overlap(root, data_dir.resolve()):
        raise TerminalUnavailable(
            "The registered repository overlaps an application or account root."
        )
    local = [item for item in inventory if not item.execution_host]
    owners = [
        item
        for item in local
        if item.project_id == project_id
        and item.alias == repository_id
        and item.machine == repository.machine
        and item.path == repository.path
    ]
    if len(owners) != 1:
        raise TerminalUnavailable("Repository ownership is missing from the project inventory.")
    roots = []
    for item in local:
        try:
            candidate = Path(item.path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise TerminalUnavailable("A registered local repository is unavailable.") from exc
        if not candidate.is_dir():
            raise TerminalUnavailable("A registered local repository is unavailable.")
        if item not in owners and _overlap(root, candidate):
            raise TerminalUnavailable("The repository overlaps another registered repository.")
        roots.append(str(candidate))
    protected = protected_repository_paths(manifest=manifest, repository_roots=roots)
    if any(root == Path(path) or Path(path) in root.parents for path in protected):
        raise TerminalUnavailable("Canonical state cannot be a terminal repository.")
    return root, protected


def _resolve_remote_repository(
    manifest: Manifest,
    project_id: str,
    repository_id: str,
    inventory: list[RegisteredRepositoryRoot],
    remote_stage: RemoteRunStage,
) -> tuple[Path, list[str]]:
    repository = manifest.repository_map[repository_id]
    same_host = [item for item in inventory if item.execution_host == remote_stage.host]
    owners = [
        item
        for item in same_host
        if (item.project_id, item.alias, item.machine, item.path)
        == (project_id, repository_id, repository.machine, repository.path)
    ]
    if len(owners) != 1:
        raise TerminalUnavailable("Repository ownership is missing from the project inventory.")
    canonical, home = remote_stage.canonical_directories(
        [item.path for item in same_host], require_writable=False
    )
    root = Path(canonical[repository.path])
    if root in {Path("/"), Path(home), Path("/tmp"), Path("/private/tmp")}:
        raise TerminalUnavailable("The registered repository overlaps an account root.")
    for item in same_host:
        if item not in owners and _overlap(root, Path(canonical[item.path])):
            raise TerminalUnavailable("The repository overlaps another registered repository.")
    protected = protected_repository_paths(
        manifest=manifest,
        repository_roots=list(dict.fromkeys([*canonical, *canonical.values()])),
        remote_stage=remote_stage,
    )
    if any(root == Path(path) or Path(path) in root.parents for path in protected):
        raise TerminalUnavailable("Canonical state cannot be a terminal repository.")
    return root, protected


def save_metadata(directory: Path, session: TerminalSession) -> None:
    destination = directory / f"{session.session_id}.json"
    fd, temporary = tempfile.mkstemp(prefix=".terminal-", dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(session), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
