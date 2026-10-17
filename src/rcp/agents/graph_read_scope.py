"""What graph history a run may read, resolved once at launch.

The owner that launches a run builds one scope. The command handler checks every
``graph_diff`` / ``graph_log`` request and cursor against it, and the prompt
renders the same object, so the prose never states a second allowlist. Reading
grants no graph authority: writes stay pinned to the run's one target
(invariant 4b).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from rcp.agents.group_context import GroupContext
from rcp.core.transition_models import GraphTargetRef

GraphHistoryVerb = Literal["graph_diff", "graph_log"]
GRAPH_HISTORY_VERBS: frozenset[GraphHistoryVerb] = frozenset(("graph_diff", "graph_log"))


class GraphReadScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    group_context: GroupContext | None = None
    project_id: str
    # The run's own target; a head reference without a target resolves here.
    target: GraphTargetRef
    # Main and every branch of the launch-time group are readable.
    verbs: frozenset[GraphHistoryVerb] = GRAPH_HISTORY_VERBS

    @classmethod
    def for_launch(
        cls, project_id: str, target: GraphTargetRef, group_context: GroupContext | None = None
    ) -> GraphReadScope:
        return cls(project_id=project_id, target=target, group_context=group_context)

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

    def readable(self, project_id: str, target: GraphTargetRef) -> bool:
        del target  # Every target of each readable project is allowed.
        return project_id == self.project_id or project_id in self.readable_project_ids
