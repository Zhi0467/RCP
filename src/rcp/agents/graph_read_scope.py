"""What graph history a run may read, resolved once at launch.

The owner that launches a run builds one scope. The command handler checks every
``graph_diff`` / ``graph_log`` request and cursor against it, and the prompt
renders the same object, so the prose never states a second allowlist. Reading
grants no graph authority: writes stay pinned to the run's one target
(invariant 4b).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.core.transition_models import GraphTargetRef

GraphHistoryVerb = Literal["graph_diff", "graph_log"]
GRAPH_HISTORY_VERBS: frozenset[GraphHistoryVerb] = frozenset(("graph_diff", "graph_log"))


class GraphReadScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    # The run's own target; a head reference without a target resolves here.
    target: GraphTargetRef
    # Main and every branch of ``project_id`` are readable.
    verbs: frozenset[GraphHistoryVerb] = GRAPH_HISTORY_VERBS

    def readable(self, project_id: str, target: GraphTargetRef) -> bool:
        del target  # Every target of the run's own project is readable.
        return project_id == self.project_id
