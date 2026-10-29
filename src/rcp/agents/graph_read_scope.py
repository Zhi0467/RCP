"""What graph history a run may read, resolved once at launch.

The owner that launches a run builds one scope. The command handler checks every
``graph_diff`` / ``graph_log`` request and cursor against it, and the prompt
renders the same object, so the prose never states a second allowlist. Reading
grants no graph authority: writes stay pinned to the run's one target
(invariant 4b).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from rcp.agents.group_context import GroupContext
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef

if TYPE_CHECKING:
    from rcp.history.branches import BranchHistoryManager
    from rcp.history.manager import HistoryManager
    from rcp.storage import AppStore

GraphHistoryVerb = Literal["graph_diff", "graph_log"]
GRAPH_HISTORY_VERBS: frozenset[GraphHistoryVerb] = frozenset(("graph_diff", "graph_log"))


def branch_label(branch_id: str | None, names: Mapping[str, str]) -> str:
    return "main" if branch_id is None else names.get(branch_id, branch_id)


class ReadableBranch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    id: str
    parent: str


class GraphReadScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    branches: tuple[ReadableBranch, ...] = ()
    group_context: GroupContext | None = None
    project_id: str
    # The run's own target; a head reference without a target resolves here.
    target: GraphTargetRef
    # None preserves project-wide reads for non-fork runs and old saved scopes.
    ancestry_ceilings: tuple[GraphHeadRef, ...] | None = None
    # Every target of each other project in the launch-time group is readable.
    verbs: frozenset[GraphHistoryVerb] = GRAPH_HISTORY_VERBS

    @classmethod
    def resolve(
        cls,
        project_id: str,
        history: HistoryManager | BranchHistoryManager,
        group_context: GroupContext | None = None,
        *,
        store: AppStore,
    ) -> GraphReadScope:
        target = history.graph_target
        scope = cls(project_id=project_id, target=target, group_context=group_context)
        if target.kind == "branch":
            generations = history.history_engine.snapshots([target], refresh=False)
            metadata = generations[target.key].source.metadata
            if metadata is not None and metadata.kind == "fork":
                ceilings = []
                while metadata is not None:
                    parent = metadata.effective_parent_head
                    ceilings.append(parent)
                    metadata = generations[parent.target.key].source.metadata
                scope = scope.model_copy(update={"ancestry_ceilings": tuple(ceilings)})
        # Identity only: a head revision here would change with every commit and
        # resend the list to every session; agents read heads through graph_log.
        records = store.graph_branches(project_id)
        names = {branch.branch_id: branch.name for branch in records}
        branches = [
            ReadableBranch(
                name=branch.name,
                id=branch.branch_id,
                parent=branch_label(branch.parent_head.target.branch_id, names),
            )
            for branch in records
            if scope.readable(project_id, GraphTargetRef(kind="branch", branch_id=branch.branch_id))
        ]
        return scope.model_copy(update={"branches": tuple(branches)})

    def branch_values(self) -> list[dict[str, Any]]:
        return [branch.model_dump(mode="json") for branch in self.branches]

    @model_validator(mode="after")
    def validate_group_project(self) -> GraphReadScope:
        if self.group_context is not None and self.group_context.project_id != self.project_id:
            raise ValueError("Group context must belong to the run's project.")
        return self

    @property
    def readable_project_ids(self) -> frozenset[str]:
        return (
            self.group_context.readable_project_ids
            if self.group_context is not None
            else frozenset({self.project_id})
        )

    def readable(
        self, project_id: str, target: GraphTargetRef, revision: int | None = None
    ) -> bool:
        if project_id != self.project_id:
            return project_id in self.readable_project_ids
        if self.ancestry_ceilings is None or target == self.target:
            return True
        return any(
            head.target == target and (revision is None or revision <= head.revision)
            for head in self.ancestry_ceilings
        )

    @property
    def readable_targets(self) -> str:
        if self.ancestry_ceilings is None:
            return "main and every branch of this project"
        return "; ".join(
            [f"`{self.target.key}`"]
            + [
                f"`{head.target.key}` through revision {head.revision}"
                for head in self.ancestry_ceilings
            ]
        )
