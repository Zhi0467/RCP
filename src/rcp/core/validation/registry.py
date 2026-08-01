"""The single declaration of the patch operation vocabulary.

Every operation RCP accepts appears exactly once in ``OP_RULES``. Both the
patch validator and the proposal-dependency walk look their operations up here,
so the two cannot drift apart about which operations exist.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rcp.core.models import GraphState
from rcp.core.validation.context import OpRule
from rcp.core.validation.ops import (
    author_create_edges,
    author_create_nodes,
    author_create_proposals,
    author_merge_nodes,
    author_supersede_nodes,
    author_update_nodes,
    depends_create_ambiguities,
    depends_create_edges,
    depends_merge_nodes,
    depends_remove_edges,
    depends_resolve_ambiguities,
    depends_set_ontology,
    depends_set_project_truth_scope,
    depends_supersede_nodes,
    depends_update_nodes,
    validate_create_edges,
    validate_create_nodes,
    validate_create_proposals,
    validate_merge_nodes,
    validate_remove_edges,
    validate_resolve_ambiguities,
    validate_resolve_proposals,
    validate_set_ontology,
    validate_set_project_truth_scope,
    validate_set_standing,
    validate_supersede_nodes,
    validate_update_nodes,
)

OP_RULES: dict[str, OpRule] = {
    "create_nodes": OpRule(
        structural_validate=validate_create_nodes,
        authoring_validate=author_create_nodes,
    ),
    "update_nodes": OpRule(
        structural_validate=validate_update_nodes,
        authoring_validate=author_update_nodes,
        dependencies=depends_update_nodes,
    ),
    "create_edges": OpRule(
        structural_validate=validate_create_edges,
        authoring_validate=author_create_edges,
        dependencies=depends_create_edges,
    ),
    "remove_edges": OpRule(
        structural_validate=validate_remove_edges,
        dependencies=depends_remove_edges,
    ),
    "supersede_nodes": OpRule(
        structural_validate=validate_supersede_nodes,
        authoring_validate=author_supersede_nodes,
        dependencies=depends_supersede_nodes,
    ),
    "merge_nodes": OpRule(
        structural_validate=validate_merge_nodes,
        authoring_validate=author_merge_nodes,
        dependencies=depends_merge_nodes,
    ),
    "create_ambiguities": OpRule(dependencies=depends_create_ambiguities),
    "resolve_ambiguities": OpRule(
        structural_validate=validate_resolve_ambiguities,
        dependencies=depends_resolve_ambiguities,
    ),
    "create_proposals": OpRule(
        structural_validate=validate_create_proposals,
        authoring_validate=author_create_proposals,
    ),
    "resolve_proposals": OpRule(structural_validate=validate_resolve_proposals),
    "upsert_glossary": OpRule(),
    "set_coverage": OpRule(),
    "set_standing": OpRule(structural_validate=validate_set_standing),
    "set_project_truth_scope": OpRule(
        structural_validate=validate_set_project_truth_scope,
        dependencies=depends_set_project_truth_scope,
    ),
    "set_ontology": OpRule(
        structural_validate=validate_set_ontology,
        dependencies=depends_set_ontology,
    ),
}


def proposal_dependencies(
    state: GraphState, ops: Iterable[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Derive the existing graph/config objects whose state a proposal depends on."""
    node_ids: set[str] = set()
    config_keys: set[str] = set()

    for op in ops:
        name = op.get("op")
        rule = OP_RULES.get(name) if isinstance(name, str) else None
        if rule is None or rule.dependencies is None:
            continue
        candidates, keys = rule.dependencies(op, state)
        for node_id in candidates:
            if isinstance(node_id, str) and node_id in state.nodes:
                node_ids.add(node_id)
        config_keys.update(keys)

    return sorted(node_ids), sorted(config_keys)
