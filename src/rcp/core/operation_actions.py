from __future__ import annotations

from rcp.core.authority import (
    CREATE_AMBIGUITY,
    CREATE_NODE,
    CREATE_PROPOSAL,
    MERGE_NODE,
    MERGE_PROTECTED_EPISTEMIC,
    REMOVE_NODE,
    REMOVE_PROTECTED_EPISTEMIC,
    RESOLVE_AMBIGUITY,
    RESOLVE_PROPOSAL,
    SET_COVERAGE,
    SET_ONTOLOGY,
    SET_PROJECT_TRUTH_SCOPE,
    SET_STANDING,
    SUPERSEDE_NODE,
    SUPERSEDE_PROTECTED_EPISTEMIC,
    UPDATE_PROTECTED_EPISTEMIC,
    UPSERT_GLOSSARY,
    WITHDRAW_PROPOSAL,
    GraphAction,
    _create_edge_actions,
    _created_node_type,
    _node_lifecycle_actions,
    _node_target_actions,
    _remove_edge_actions,
    _update_actions,
    is_existing_protected_node,
)
from rcp.core.models import GraphState, Patch
from rcp.core.operations import (
    CreateAmbiguitiesOperation,
    CreateEdgesOperation,
    CreateNodesOperation,
    CreateProposalsOperation,
    GraphOperation,
    MergeNodesOperation,
    RemoveEdgesOperation,
    RemoveNodesOperation,
    ResolveAmbiguitiesOperation,
    ResolveProposalsOperation,
    SetCoverageOperation,
    SetOntologyOperation,
    SetProjectTruthScopeOperation,
    SetStandingOperation,
    SupersedeNodesOperation,
    UpdateNodesOperation,
    UpsertGlossaryOperation,
    WithdrawProposalsOperation,
)
from rcp.core.project_types import project_type_of


def actions_create_nodes(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, CreateNodesOperation)
    return frozenset({CREATE_NODE})


def actions_update_nodes(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, UpdateNodesOperation)
    return _update_actions(state, patch, operation)


def actions_create_edges(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, CreateEdgesOperation)
    return _create_edge_actions(state, patch, operation)


def actions_remove_edges(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, RemoveEdgesOperation)
    return _remove_edge_actions(state, patch, operation)


def actions_remove_nodes(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, RemoveNodesOperation)
    return _node_target_actions(
        state,
        patch,
        operation.node_ids,
        REMOVE_NODE,
        REMOVE_PROTECTED_EPISTEMIC,
    )


def actions_supersede_nodes(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, SupersedeNodesOperation)
    return _node_lifecycle_actions(
        state,
        patch,
        (item.id for item in operation.nodes),
        (
            (item.id, item.superseded_by)
            for item in operation.nodes
            if item.superseded_by is not None
        ),
        "supersedes",
        SUPERSEDE_NODE,
        SUPERSEDE_PROTECTED_EPISTEMIC,
    )


def actions_merge_nodes(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, MergeNodesOperation)
    return _node_lifecycle_actions(
        state,
        patch,
        (item.duplicate for item in operation.merges),
        ((item.duplicate, item.canonical) for item in operation.merges),
        "duplicate_of",
        MERGE_NODE,
        MERGE_PROTECTED_EPISTEMIC,
    )


def actions_set_standing(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, SetStandingOperation)
    if is_existing_protected_node(state, operation.node_id) or project_type_of(
        state
    ).is_protected_belief(_created_node_type(patch, operation.node_id)):
        return frozenset({UPDATE_PROTECTED_EPISTEMIC})
    return frozenset({SET_STANDING})


def actions_create_ambiguities(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, CreateAmbiguitiesOperation)
    return frozenset({CREATE_AMBIGUITY})


def actions_resolve_ambiguities(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, ResolveAmbiguitiesOperation)
    return frozenset({RESOLVE_AMBIGUITY})


def actions_create_proposals(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, CreateProposalsOperation)
    return frozenset({CREATE_PROPOSAL})


def actions_resolve_proposals(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, ResolveProposalsOperation)
    return frozenset({RESOLVE_PROPOSAL})


def actions_withdraw_proposals(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, WithdrawProposalsOperation)
    return frozenset({WITHDRAW_PROPOSAL})


def actions_upsert_glossary(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, UpsertGlossaryOperation)
    return frozenset({UPSERT_GLOSSARY})


def actions_set_coverage(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, SetCoverageOperation)
    return frozenset({SET_COVERAGE})


def actions_set_project_truth_scope(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, SetProjectTruthScopeOperation)
    return frozenset({SET_PROJECT_TRUTH_SCOPE})


def actions_set_ontology(
    state: GraphState, patch: Patch, operation: GraphOperation
) -> frozenset[GraphAction]:
    assert isinstance(operation, SetOntologyOperation)
    return frozenset({SET_ONTOLOGY})
