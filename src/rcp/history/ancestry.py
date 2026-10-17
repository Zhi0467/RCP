"""Validate complete canonical branch inventories before copying history."""

from __future__ import annotations

from collections.abc import Mapping

from rcp.core.models import GraphBranchMetadata
from rcp.limits import GRAPH_BRANCH_MAX_DEPTH


def validate_branch_ancestry(
    branches: Mapping[str, GraphBranchMetadata], *, main_revision: int
) -> None:
    for branch in branches.values():
        seen: set[str] = set()
        current = branch
        while True:
            if current.branch_id in seen:
                raise ValueError("branch ancestry contains a cycle")
            seen.add(current.branch_id)
            if len(seen) > GRAPH_BRANCH_MAX_DEPTH:
                raise ValueError("branch ancestry exceeds the depth limit")
            parent = current.effective_parent_head
            if parent.target.kind == "main":
                if parent.revision > main_revision or parent != current.base_head:
                    raise ValueError("branch ancestry has an invalid main base")
                break
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
            current = ancestor
