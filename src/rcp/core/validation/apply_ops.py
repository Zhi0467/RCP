from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from pydantic import TypeAdapter

from rcp.core.models import (
    Ambiguity,
    BeliefTransition,
    Edge,
    GlossaryTerm,
    GraphState,
    Patch,
    ProjectNode,
    Standing,
)
from rcp.core.ontology import custom_relation, edge_layer
from rcp.core.operations import (
    BeliefCause,
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
    strict_project_node,
)
from rcp.core.project_types import project_type_of
from rcp.core.validation.constants import (
    IMMUTABLE_NODE_UPDATE_FIELDS,
    LEGACY_COMPATIBILITY_UPDATE_FIELDS,
)

NODE_ADAPTER = TypeAdapter(ProjectNode)


@dataclass
class ApplyContext:
    state: GraphState
    patch: Patch
    repository_descriptors: list[dict[str, str]]
    created_edge_ids: list[str]
    system_fields: bool = False


def apply_create_nodes(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, CreateNodesOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for raw in op.nodes:
        data = raw.model_dump(mode="python", exclude_unset=True)
        data["created_rev"] = revision
        data["updated_rev"] = revision
        data["standing"] = data.get("standing", "asserted")
        node = NODE_ADAPTER.validate_python(data)
        state.nodes[node.id] = node


def apply_update_nodes(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, UpdateNodesOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for update in op.nodes:
        node = state.nodes[update.id]
        changes = update.changes
        immutable_fields = IMMUTABLE_NODE_UPDATE_FIELDS
        if patch.schema_generation == 1:
            immutable_fields = immutable_fields - LEGACY_COMPATIBILITY_UPDATE_FIELDS
        elif ctx.system_fields:
            immutable_fields = immutable_fields - {
                "current_summary_stale",
                "next_action_stale",
            }
        immutable = sorted(set(changes) & immutable_fields)
        if immutable:
            raise ValueError(f"node updates cannot change system fields: {', '.join(immutable)}")
        data = node.model_dump(mode="python")
        data.update(changes)
        data["updated_rev"] = revision
        data["standing"] = node.standing if patch.kind == "approval" else "asserted"
        updated = (
            NODE_ADAPTER.validate_python(data)
            if patch.schema_generation == 1
            else strict_project_node(data)
        )
        _record_belief_transition(
            state,
            node,
            updated,
            revision,
            update.cause,
        )
        state.nodes[node.id] = updated


def apply_create_edges(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, CreateEdgesOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    created_edge_ids = ctx.created_edge_ids
    for raw in op.edges:
        data = raw.model_dump(mode="python", exclude_unset=True)
        data.setdefault("id", f"{data['source']}::{data['relation']}::{data['target']}")
        if relation := custom_relation(state.ontology, raw.relation):
            data["layer"] = relation.layer
        data["created_rev"] = revision
        edge = Edge.model_validate(data)
        derived = edge_layer(state, edge.source, edge.target, edge.layer)
        if derived != edge.layer:
            edge = edge.model_copy(update={"layer": derived})
        state.edges[edge.id] = edge
        created_edge_ids.append(edge.id)


def apply_remove_edges(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, RemoveEdgesOperation)
    state = ctx.state
    for edge_id in op.edge_ids:
        state.edges.pop(edge_id, None)


def apply_remove_nodes(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, RemoveNodesOperation)
    state = ctx.state
    node_ids = set(op.node_ids)
    state.nodes = {
        node_id: node for node_id, node in state.nodes.items() if node_id not in node_ids
    }
    state.edges = {
        edge_id: edge
        for edge_id, edge in state.edges.items()
        if edge.source not in node_ids and edge.target not in node_ids
    }


def apply_supersede_nodes(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, SupersedeNodesOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for item in op.nodes:
        previous = state.nodes[item.id]
        _retire_node(
            state,
            item.id,
            revision,
            preserve_standing=patch.kind == "approval",
        )
        _record_belief_transition(
            state,
            previous,
            state.nodes[item.id],
            revision,
            item.cause,
        )
        target = item.superseded_by
        if target:
            edge = Edge.model_validate(
                dict(
                    id=f"{item.id}::supersedes::{target}",
                    source=item.id,
                    target=target,
                    relation="supersedes",
                    explanation=item.explanation,
                    created_rev=revision,
                )
            )
            state.edges[edge.id] = edge


def apply_merge_nodes(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, MergeNodesOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for item in op.merges:
        duplicate = item.duplicate
        canonical = item.canonical
        previous = state.nodes[duplicate]
        _retire_node(
            state,
            duplicate,
            revision,
            preserve_standing=patch.kind == "approval",
        )
        _record_belief_transition(
            state,
            previous,
            state.nodes[duplicate],
            revision,
            item.cause,
        )
        edge = Edge.model_validate(
            dict(
                id=f"{duplicate}::duplicate_of::{canonical}",
                source=duplicate,
                target=canonical,
                relation="duplicate_of",
                explanation=item.explanation,
                created_rev=revision,
            )
        )
        state.edges[edge.id] = edge


def apply_create_ambiguities(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, CreateAmbiguitiesOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for raw in op.ambiguities:
        data = raw.model_dump(mode="python", exclude_unset=True)
        data["raised_rev"] = revision
        ambiguity = Ambiguity.model_validate(data)
        state.ambiguities[ambiguity.id] = ambiguity


def apply_resolve_ambiguities(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, ResolveAmbiguitiesOperation)
    state = ctx.state
    for resolution in op.resolutions:
        ambiguity = state.ambiguities[resolution.id]
        state.ambiguities[ambiguity.id] = ambiguity.model_copy(update={"status": resolution.status})


def apply_create_proposals(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, CreateProposalsOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    from rcp.core.validation.registry import proposal_dependencies

    for raw in op.proposals:
        related_node_ids, related_edge_ids, related_config_keys = proposal_dependencies(
            state, raw.ops
        )
        proposal = raw.model_copy(
            update={
                "base_rev": state.revision,
                "related_node_ids": related_node_ids,
                "related_edge_ids": related_edge_ids,
                "related_config_keys": related_config_keys,
                "created_by": raw.created_by
                if "created_by" in raw.model_fields_set
                else ("human" if patch.author == "human" else "agent"),
                "created_by_operation_id": raw.created_by_operation_id
                if "created_by_operation_id" in raw.model_fields_set
                else patch.source_operation_id,
                "raised_rev": revision,
            }
        )
        state.proposals[proposal.id] = proposal


def apply_resolve_proposals(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, ResolveProposalsOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for resolution in op.resolutions:
        proposal = state.proposals[resolution.id]
        state.proposals[proposal.id] = proposal.model_copy(
            update={
                "status": resolution.status,
                "resolved_rev": revision,
                "resolved_by": "human" if patch.author == "human" else "agent",
                "resolved_by_operation_id": patch.source_operation_id,
                "resolution_reason": resolution.reason,
                "rejection_reason": (
                    resolution.reason if resolution.status == "rejected" else None
                ),
            }
        )


def apply_withdraw_proposals(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, WithdrawProposalsOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for withdrawal in op.proposals:
        proposal = state.proposals[withdrawal.id]
        state.proposals[proposal.id] = proposal.model_copy(
            update={
                "status": "withdrawn",
                "resolved_rev": revision,
                "resolved_by": "agent",
                "resolved_by_operation_id": patch.source_operation_id,
                "resolution_reason": withdrawal.reason,
            }
        )


def apply_upsert_glossary(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, UpsertGlossaryOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    for raw in op.terms:
        data = raw.model_dump(mode="python", exclude_unset=True)
        data["updated_rev"] = revision
        term = GlossaryTerm.model_validate(data)
        state.glossary[term.term] = term


def apply_set_coverage(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, SetCoverageOperation)
    # Historical reading reports have no current graph effect.


def apply_set_standing(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, SetStandingOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    node = state.nodes[op.node_id]
    state.nodes[node.id] = node.model_copy(
        update={"standing": Standing(op.standing), "updated_rev": revision}
    )


def apply_set_project_truth_scope(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, SetProjectTruthScopeOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    repository_descriptors = ctx.repository_descriptors
    new_scope = set(op.truth_scope)
    state.project_truth_scope = sorted(new_scope)
    state.config_revisions["project_truth_scope"] = revision
    if op.repository is not None:
        repository_descriptors.append(op.repository.model_dump(mode="python", exclude_unset=True))


def apply_set_ontology(op: GraphOperation, ctx: ApplyContext) -> None:
    assert isinstance(op, SetOntologyOperation)
    state, patch = ctx.state, ctx.patch
    revision = patch.revision
    state.ontology = op.ontology
    state.config_revisions["ontology"] = revision


def _retire_node(
    state: GraphState,
    node_id: str,
    revision: int,
    *,
    preserve_standing: bool,
) -> None:
    node = state.nodes[node_id]
    project_type = project_type_of(state)
    data: dict[str, Any] = node.model_dump(mode="python")
    data[project_type.lifecycle_field(node.type)] = project_type.retired_value
    data["updated_rev"] = revision
    data["standing"] = node.standing if preserve_standing else "asserted"
    state.nodes[node_id] = NODE_ADAPTER.validate_python(data)


def _record_belief_transition(
    state: GraphState,
    previous: ProjectNode,
    updated: ProjectNode,
    revision: int,
    cause: BeliefCause | None,
) -> None:
    project_type = project_type_of(state)
    if (
        not project_type.is_belief(previous.type)
        or not project_type.is_belief(updated.type)
        or cast(Any, previous).status == cast(Any, updated).status
        or cause is None
    ):
        return
    state.belief_transitions.append(
        BeliefTransition(
            hypothesis_id=previous.id,
            from_status=cast(Any, previous).status,
            to_status=cast(Any, updated).status,
            revision=revision,
            cause=cause.model_dump(mode="python", exclude_unset=True),
        )
    )
