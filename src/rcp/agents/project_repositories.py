"""Which repositories a project's agents read and write: one source for every launch.

Every agent reads every repository in the manifest, plus every repository of an
available sibling project in its group. Writes are bounded by the graph's project
truth scope, which only a human approval changes (invariant 3), and then by each
run kind's fixed capability (invariant 4). The manifest's `project.truth_scope` is
a mirror the history manager writes; nothing decides from it.

A repository is named by a ref: a bare alias names this project's repository, and
`<project_id>:<alias>` names a sibling's. `repository_ref` and
`parse_repository_ref` are the only writer and reader of that encoding.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from rcp.config import Manifest, RepositoryConfig
from rcp.core.models import GraphState

if TYPE_CHECKING:
    from rcp.agents.group_context import GroupContext


def write_repositories(state: GraphState) -> list[str]:
    """The repositories Work-like runs may write: the graph's project truth scope."""

    if not state.project_truth_scope:
        raise ValueError("The graph has no project truth scope.")
    return sorted(set(state.project_truth_scope))


def readable_repositories(manifest: Manifest) -> list[RepositoryConfig]:
    """Every repository of the project, in manifest order."""

    return list(manifest.repositories)


def admitted_write_repositories(
    state: GraphState, requested: list[str] | None, *, worktree: bool
) -> list[str]:
    """One run's write set: a worktree names its one repository, every other run all.

    `requested` matters only for a worktree, where it is that worktree's repository.
    """

    truth = write_repositories(state)
    if not worktree:
        return truth
    if not requested or len(requested) != 1 or requested[0] not in truth:
        raise ValueError("A worktree needs exactly one repository the project's agents can write.")
    return list(requested)


def repository_ref(project_id: str, alias: str) -> str:
    """Name a sibling project's repository."""

    return f"{project_id}:{alias}"


def parse_repository_ref(ref: str) -> tuple[str | None, str]:
    """Split a ref into its project id (None for this project) and alias."""

    project_id, separator, alias = ref.partition(":")
    return (project_id, alias) if separator else (None, ref)


class RepositoryReadScope(BaseModel):
    """Every repository one launch may read, resolved once beside its write scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str | None
    # Ref to the path the launch was given for it.
    paths: dict[str, str]

    @property
    def refs(self) -> list[str]:
        return sorted(self.paths)

    def ref_for(self, declared: str) -> str:
        """Read an agent's declaration back to its ref; an unknown name stays as written.

        An agent may name a repository by ref, by `<this project id>:<alias>`, or by
        any path at or under the root the launch showed it.
        """

        if declared in self.paths:
            return declared
        project_id, alias = parse_repository_ref(declared)
        if project_id is not None and project_id == self.project_id and alias in self.paths:
            return alias
        candidate = PurePosixPath(declared)
        roots = sorted(
            ((PurePosixPath(path), ref) for ref, path in self.paths.items()),
            key=lambda item: len(item[0].parts),
            reverse=True,
        )
        return next((ref for root, ref in roots if candidate.is_relative_to(root)), declared)


class _Located(Protocol):
    alias: str
    path: str


def resolve_read_scope(
    project_id: str | None,
    repositories: Iterable[_Located],
    group_context: GroupContext | None,
) -> RepositoryReadScope:
    """This project's repositories plus every available sibling's, from the launch's group.

    `repositories` are this project's, at the paths this launch shows them.
    """

    paths = {item.alias: item.path for item in repositories}
    paths.update(sibling_repository_paths(group_context))
    return RepositoryReadScope(project_id=project_id, paths=paths)


def sibling_repository_paths(group_context: GroupContext | None) -> dict[str, str]:
    """Each available sibling repository's ref and path; an unavailable sibling grants none."""

    if group_context is None:
        return {}
    return {
        repository_ref(sibling.project_id, repository.alias): repository.path
        for sibling in group_context.siblings
        if sibling.unavailable_reason is None
        for repository in sibling.repositories
    }
