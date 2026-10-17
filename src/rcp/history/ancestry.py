"""Validate complete canonical branch inventories before copying history."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from rcp import limits
from rcp.core.models import GraphBranchMetadata


def branch_ancestry(branch_id: str, parent_of: Callable[[str], str | None]) -> list[str]:
    """Return a bounded child-to-root chain, loading each parent through its owner."""
    chain: list[str] = []
    current: str | None = branch_id
    while current is not None:
        if current in chain:
            raise ValueError("branch ancestry contains a cycle")
        if len(chain) >= limits.GRAPH_BRANCH_MAX_DEPTH:
            raise ValueError("branch ancestry exceeds the depth limit")
        chain.append(current)
        current = parent_of(current)
    return chain


def validate_branch_ancestry(
    branches: Mapping[str, GraphBranchMetadata], *, main_revision: int
) -> None:
    def parent_of(branch_id: str) -> str | None:
        current = branches[branch_id]
        parent = current.effective_parent_head
        if parent.target.kind == "main":
            if parent.revision > main_revision or parent != current.base_head:
                raise ValueError("branch ancestry has an invalid main base")
            return None
        ancestor = branches.get(parent.target.branch_id or "")
        if ancestor is None:
            raise ValueError("branch ancestry requires every parent")
        if (
            ancestor.project_id != current.project_id
            or ancestor.base_head != current.base_head
            or not ancestor.effective_parent_head.revision
            <= parent.revision
            <= ancestor.head.revision
        ):
            raise ValueError("branch ancestry has an invalid parent head")
        return ancestor.branch_id

    for branch_id in branches:
        branch_ancestry(branch_id, parent_of)
