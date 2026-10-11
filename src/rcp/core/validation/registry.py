"""The single declaration of the patch operation vocabulary.

Every operation RCP accepts appears exactly once in ``OP_RULES``. Validation,
authority actions, application, and proposal dependencies dispatch through it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from rcp.core import operation_actions
from rcp.core.models import GraphState
from rcp.core.operations import (
    DecisionCause,
    EvidenceEdgeCause,
    ProposalContentChangeOperation,
    ProposalOperation,
    ProposalProtectedRelationOperation,
    ProposalRemovalOperation,
    ProposalStandingChangeOperation,
    ProposalStatusChangeOperation,
)
from rcp.core.validation import apply_ops
from rcp.core.validation.context import OpRule, OpValidator
from rcp.core.validation.ops import (
    author_create_edges,
    author_create_nodes,
    author_create_proposals,
    author_update_nodes,
    depends_create_ambiguities,
    depends_create_edges,
    depends_merge_nodes,
    depends_remove_edges,
    depends_remove_nodes,
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
    validate_remove_nodes,
    validate_resolve_ambiguities,
    validate_resolve_proposals,
    validate_set_ontology,
    validate_set_project_truth_scope,
    validate_set_standing,
    validate_supersede_nodes,
    validate_update_nodes,
    validate_withdraw_proposals,
)

OP_RULES: dict[str, OpRule] = {
    "create_nodes": OpRule(
        actions=operation_actions.actions_create_nodes,
        apply=apply_ops.apply_create_nodes,
        structural_validate=validate_create_nodes,
        authoring_validate=author_create_nodes,
    ),
    "update_nodes": OpRule(
        actions=operation_actions.actions_update_nodes,
        apply=apply_ops.apply_update_nodes,
        structural_validate=validate_update_nodes,
        authoring_validate=author_update_nodes,
        dependencies=depends_update_nodes,
    ),
    "create_edges": OpRule(
        actions=operation_actions.actions_create_edges,
        apply=apply_ops.apply_create_edges,
        structural_validate=validate_create_edges,
        authoring_validate=author_create_edges,
        dependencies=depends_create_edges,
    ),
    "remove_edges": OpRule(
        actions=operation_actions.actions_remove_edges,
        apply=apply_ops.apply_remove_edges,
        structural_validate=validate_remove_edges,
        dependencies=depends_remove_edges,
    ),
    "remove_nodes": OpRule(
        actions=operation_actions.actions_remove_nodes,
        apply=apply_ops.apply_remove_nodes,
        structural_validate=validate_remove_nodes,
        dependencies=depends_remove_nodes,
    ),
    "supersede_nodes": OpRule(
        actions=operation_actions.actions_supersede_nodes,
        apply=apply_ops.apply_supersede_nodes,
        structural_validate=validate_supersede_nodes,
        dependencies=depends_supersede_nodes,
    ),
    "merge_nodes": OpRule(
        actions=operation_actions.actions_merge_nodes,
        apply=apply_ops.apply_merge_nodes,
        structural_validate=validate_merge_nodes,
        dependencies=depends_merge_nodes,
    ),
    "create_ambiguities": OpRule(
        actions=operation_actions.actions_create_ambiguities,
        apply=apply_ops.apply_create_ambiguities,
        dependencies=depends_create_ambiguities,
        legacy_only=True,
    ),
    "resolve_ambiguities": OpRule(
        actions=operation_actions.actions_resolve_ambiguities,
        apply=apply_ops.apply_resolve_ambiguities,
        structural_validate=validate_resolve_ambiguities,
        dependencies=depends_resolve_ambiguities,
        legacy_only=True,
    ),
    "create_proposals": OpRule(
        actions=operation_actions.actions_create_proposals,
        apply=apply_ops.apply_create_proposals,
        structural_validate=validate_create_proposals,
        authoring_validate=author_create_proposals,
    ),
    "resolve_proposals": OpRule(
        actions=operation_actions.actions_resolve_proposals,
        apply=apply_ops.apply_resolve_proposals,
        structural_validate=cast(OpValidator, validate_resolve_proposals),
    ),
    "withdraw_proposals": OpRule(
        actions=operation_actions.actions_withdraw_proposals,
        apply=apply_ops.apply_withdraw_proposals,
        structural_validate=cast(OpValidator, validate_withdraw_proposals),
    ),
    "upsert_glossary": OpRule(
        actions=operation_actions.actions_upsert_glossary,
        apply=apply_ops.apply_upsert_glossary,
    ),
    "set_coverage": OpRule(
        actions=operation_actions.actions_set_coverage,
        apply=apply_ops.apply_set_coverage,
        legacy_only=True,
    ),
    "set_standing": OpRule(
        actions=operation_actions.actions_set_standing,
        apply=apply_ops.apply_set_standing,
        structural_validate=cast(OpValidator, validate_set_standing),
    ),
    "set_project_truth_scope": OpRule(
        actions=operation_actions.actions_set_project_truth_scope,
        apply=apply_ops.apply_set_project_truth_scope,
        structural_validate=validate_set_project_truth_scope,
        dependencies=depends_set_project_truth_scope,
    ),
    "set_ontology": OpRule(
        actions=operation_actions.actions_set_ontology,
        apply=apply_ops.apply_set_ontology,
        structural_validate=validate_set_ontology,
        dependencies=depends_set_ontology,
    ),
}


def proposal_dependencies(
    state: GraphState, ops: Iterable[ProposalOperation]
) -> tuple[list[str], list[str], list[str]]:
    """Derive the graph objects and config whose exact state a proposal depends on."""
    node_ids: set[str] = set()
    edge_ids: set[str] = set()
    config_keys: set[str] = set()

    for op in ops:
        if isinstance(op, ProposalStandingChangeOperation):
            node_ids.add(op.node_id)
        name = op.op
        rule = OP_RULES[name]
        if rule.dependencies is None:
            continue
        candidates, keys = rule.dependencies(op, state)
        for node_id in candidates:
            if isinstance(node_id, str):
                node_ids.add(node_id)
        config_keys.update(keys)

        if isinstance(op, ProposalProtectedRelationOperation) and op.op == "remove_edges":
            edge_ids.update(op.edge_ids or [])
        if isinstance(op, ProposalRemovalOperation):
            target_ids = set(op.node_ids)
            edge_ids.update(
                edge.id
                for edge in state.edges.values()
                if edge.source in target_ids or edge.target in target_ids
            )
        if isinstance(op, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
            for update in op.nodes:
                cause = update.cause
                if isinstance(cause, EvidenceEdgeCause):
                    edge_ids.add(cause.ref_id)
                elif isinstance(cause, DecisionCause):
                    node_ids.add(cause.ref_id)

    return sorted(node_ids), sorted(edge_ids), sorted(config_keys)
