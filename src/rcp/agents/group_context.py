"""Launch-time group read pointers, independent of the project's write scope."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, model_validator

from rcp.config import Manifest, load_manifest
from rcp.transport import RepositoryAccess
from rcp.transport.state import (
    SSHStateWorkspace,
    StateUnavailable,
    load_remote_workspace_manifest,
    state_workspace_for_probe,
)

if TYPE_CHECKING:
    from rcp.service import ProjectService
    from rcp.storage import AppStore


_LOGGER = logging.getLogger(__name__)


class GroupProjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    name: str
    graph: RepositoryAccess | None = None
    research_md: RepositoryAccess | None = None
    repositories: tuple[RepositoryAccess, ...] = ()
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def available_pointers(self) -> Self:
        if self.unavailable_reason is None and (self.graph is None or self.research_md is None):
            raise ValueError("An available group project requires graph and research pointers.")
        return self


class GroupContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group_name: str
    project_id: str
    project_name: str
    siblings: tuple[GroupProjectContext, ...]

    @property
    def readable_project_ids(self) -> frozenset[str]:
        return frozenset(
            (
                self.project_id,
                *(item.project_id for item in self.siblings if item.unavailable_reason is None),
            )
        )

    def read_dirs(self) -> list[Path]:
        """Only pointers local to the execution machine become provider read roots."""
        paths: list[Path] = []
        for sibling in self.siblings:
            if sibling.unavailable_reason is not None:
                continue
            pointers = (sibling.graph, sibling.research_md, *sibling.repositories)
            for pointer in pointers:
                if pointer is None or pointer.host:
                    continue
                path = Path(pointer.path)
                if pointer in (sibling.graph, sibling.research_md):
                    path = path.parent
                if path not in paths:
                    paths.append(path)
        return paths


def build_group_context(
    service: ProjectService,
    store: AppStore | None,
    execution_machine: str,
) -> GroupContext | None:
    """Resolve sibling reads; unavailable siblings remain visible but grant no access."""
    project_id = service._project_id
    if store is None or project_id is None:
        return None
    group = store.project_group_for(project_id)
    if group is None:
        return None
    sibling_ids = [
        item for item in store.project_group_project_ids(project_id) if item != project_id
    ]
    if not sibling_ids:
        return None
    execution_host = service.manifest.machine_map[execution_machine].host
    siblings = []
    for sibling_id in sibling_ids:
        record = None
        try:
            record = store.project(sibling_id)
            if record is None:
                raise ValueError(f"Group project is unavailable: {sibling_id}")
            manifest = _canonical_manifest(record.locator, service._data_dir)

            def pointer(
                alias: str,
                machine: str,
                path: str,
                manifest: Manifest = manifest,
                sibling_id: str = sibling_id,
            ) -> RepositoryAccess:
                host = manifest.machine_map[machine].host
                if execution_host and not host:
                    raise ValueError(
                        f"Group project {sibling_id!r} machine {machine!r} has no SSH host "
                        f"reachable from execution machine {execution_machine!r}."
                    )
                return RepositoryAccess(
                    alias=alias,
                    machine=machine,
                    host="" if host == execution_host else host,
                    path=path,
                )

            state_repository = manifest.repository_map[manifest.state.repository]
            root = PurePosixPath(state_repository.path) / ".research"
            siblings.append(
                GroupProjectContext(
                    project_id=sibling_id,
                    name=record.name,
                    graph=pointer("graph", state_repository.machine, str(root / "graph.json")),
                    research_md=pointer(
                        "research_md", state_repository.machine, str(root / "research.md")
                    ),
                    repositories=tuple(
                        pointer(item.alias, item.machine, item.path)
                        for item in manifest.repositories
                        if item.alias in manifest.project.truth_scope
                    ),
                )
            )
        except (OSError, ValueError, KeyError, StateUnavailable) as exc:
            _LOGGER.warning("Group project %s is unavailable: %s", sibling_id, exc)
            siblings.append(
                GroupProjectContext(
                    project_id=sibling_id,
                    name=record.name if record is not None else sibling_id,
                    unavailable_reason=f"Sibling read context unavailable ({type(exc).__name__}).",
                )
            )
    return GroupContext(
        group_name=group.name,
        project_id=project_id,
        project_name=service.manifest.name,
        siblings=tuple(siblings),
    )


def _canonical_manifest(locator: str, data_dir: Path | None) -> Manifest:
    bootstrap = load_manifest(locator)
    state_repository = bootstrap.repository_map[bootstrap.state.repository]
    if not bootstrap.machine_map[state_repository.machine].host:
        return bootstrap
    if data_dir is None:
        raise ValueError("Remote group project requires the space data directory.")
    workspace = state_workspace_for_probe(bootstrap, data_dir)
    if not isinstance(workspace, SSHStateWorkspace):
        raise ValueError("Remote group project has no SSH state workspace.")
    return load_remote_workspace_manifest(
        bootstrap, workspace, project_root=bootstrap.path.parent.parent
    )
