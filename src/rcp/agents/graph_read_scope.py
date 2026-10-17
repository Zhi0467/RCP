"""What graph history a run may read, resolved once at launch.

The owner that launches a run builds one scope. The command handler checks every
``graph_diff`` / ``graph_log`` request and cursor against it, and the prompt
renders the same object, so the prose never states a second allowlist. Reading
grants no graph authority: writes stay pinned to the run's one target
(invariant 4b).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from rcp.core.transition_models import GraphHeadRef, GraphTargetRef

if TYPE_CHECKING:
    from rcp.history.branches import BranchHistoryManager
    from rcp.history.manager import HistoryManager

GraphHistoryVerb = Literal["graph_diff", "graph_log"]
GRAPH_HISTORY_VERBS: frozenset[GraphHistoryVerb] = frozenset(("graph_diff", "graph_log"))


class GraphReadScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    # The run's own target; a head reference without a target resolves here.
    target: GraphTargetRef
    # None preserves project-wide reads for non-fork runs and old saved scopes.
    ancestry_ceilings: tuple[GraphHeadRef, ...] | None = None
    verbs: frozenset[GraphHistoryVerb] = GRAPH_HISTORY_VERBS

    @classmethod
    def resolve(
        cls, project_id: str, history: HistoryManager | BranchHistoryManager
    ) -> GraphReadScope:
        target = history.graph_target
        if target.kind != "branch":
            return cls(project_id=project_id, target=target)
        generations = history.history_engine.snapshots([target], refresh=False)
        metadata = generations[target.key].source.metadata
        if metadata is None or metadata.kind != "fork":
            return cls(project_id=project_id, target=target)
        ceilings = []
        while metadata is not None:
            parent = metadata.effective_parent_head
            ceilings.append(parent)
            metadata = generations[parent.target.key].source.metadata
        return cls(project_id=project_id, target=target, ancestry_ceilings=tuple(ceilings))

    def readable(
        self, project_id: str, target: GraphTargetRef, revision: int | None = None
    ) -> bool:
        if project_id != self.project_id:
            return False
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
