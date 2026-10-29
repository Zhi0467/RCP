"""Which repositories a project's agents read and write: one source for every launch.

Every agent reads every repository in the manifest. Writes are bounded by the
graph's project truth scope, which only a human approval changes (invariant 3),
and then by each run kind's fixed capability (invariant 4). The manifest's
`project.truth_scope` is a mirror the history manager writes; nothing decides
from it.
"""

from __future__ import annotations

from rcp.config import Manifest, RepositoryConfig
from rcp.core.models import GraphState


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
