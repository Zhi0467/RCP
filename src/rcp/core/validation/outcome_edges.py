"""Whether an outcome's judgment of a belief still matches the outcome."""

from __future__ import annotations

from rcp.core.models import Edge, GraphState
from rcp.core.project_types import project_type_of


def is_current_evidence(state: GraphState, node_id: str) -> bool:
    """Present, not retired Evidence: what flags and edge judgments may name."""
    node = state.nodes.get(node_id)
    project_type = project_type_of(state)
    return (
        node is not None
        and project_type.is_outcome(node.type)
        and getattr(node, project_type.lifecycle_field(node.type)) != project_type.retired_value
    )


def edge_is_stale(state: GraphState, edge: Edge) -> bool:
    """An outcome-to-belief edge judged before its outcome last moved.

    Rewording or judging the outcome's standing moves it, as it does for a
    Proposal citing that outcome. Re-judging recreates the edge, which clears this.
    """
    project_type = project_type_of(state)
    target = state.nodes.get(edge.target)
    return (
        edge.relation in project_type.belief_outcome_relations
        and target is not None
        and project_type.is_belief(target.type)
        and is_current_evidence(state, edge.source)
        and state.nodes[edge.source].updated_rev > edge.created_rev
    )


def stale_edge_ids(state: GraphState) -> list[str]:
    return sorted(edge.id for edge in state.edges.values() if edge_is_stale(state, edge))
