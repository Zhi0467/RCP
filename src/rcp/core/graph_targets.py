"""Canonical SQLite identity encoding for graph targets."""

from __future__ import annotations

from rcp.core.transition_models import GraphTargetRef


def graph_target_json(target: GraphTargetRef) -> str:
    """Serialize every persisted target and raw-equality query identically."""
    return GraphTargetRef(kind=target.kind, branch_id=target.branch_id).model_dump_json()
