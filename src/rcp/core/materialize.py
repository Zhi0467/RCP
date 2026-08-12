from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter

from rcp.core.models import (
    Ambiguity,
    BeliefTransition,
    CoverageBoundary,
    Edge,
    GlossaryTerm,
    GraphState,
    Hypothesis,
    OntologyState,
    Patch,
    ProjectNode,
    Proposal,
    ReplayFailure,
    Standing,
)
from rcp.core.ontology import custom_relation, edge_layer
from rcp.core.validation import (
    IMMUTABLE_NODE_UPDATE_FIELDS,
    ValidationReport,
    proposal_dependencies,
    validate_patch,
)

NODE_ADAPTER = TypeAdapter(ProjectNode)
AcceptedPatchObserver = Callable[[GraphState, Patch, GraphState], None]


@dataclass
class MaterializationResult:
    state: GraphState
    reports: dict[int, ValidationReport] = field(default_factory=dict)
    repository_descriptors: list[dict[str, str]] = field(default_factory=list)
    processed_cursors: dict[str, str] = field(default_factory=dict)
    patches: list[Patch] = field(default_factory=list)


def materialize_patches(
    patches: Iterable[Patch],
    initial_truth_scope: Iterable[str],
    repository_aliases: Iterable[str] | None = None,
    machine_aliases: Iterable[str] | None = None,
    default_run_truth_scope: Iterable[str] | None = None,
    state_repository: str | None = None,
    accepted_patch_observer: AcceptedPatchObserver | None = None,
) -> MaterializationResult:
    """Replay patches, optionally observing successful applications through a read-only callback."""

    replayed_patches = list(patches)
    initial_scope = list(initial_truth_scope)
    state = GraphState(project_truth_scope=initial_scope)
    state.coverage = state.coverage.model_copy(
        update={"repositories_never_seen": sorted(initial_scope)}
    )
    reports: dict[int, ValidationReport] = {}
    descriptors: list[dict[str, str]] = []
    processed_cursors: dict[str, str] = {}

    for patch in replayed_patches:
        if patch.admission == "rejected":
            report = ValidationReport()
            report.messages.extend(patch.admission_messages)
            reports[patch.revision] = report
            state.revision = max(state.revision, patch.revision)
            state.validation_messages.extend(patch.admission_messages)
            continue

        report = validate_patch(
            state,
            patch,
            state.project_truth_scope,
            repository_aliases=repository_aliases,
            machine_aliases=machine_aliases,
            default_run_truth_scope=default_run_truth_scope,
            state_repository=state_repository,
            mode="replay",
        )
        report.messages.extend(patch.admission_messages)
        reports[patch.revision] = report
        if report.rejected:
            failure = next(item for item in report.messages if item.level == "reject")
            state.replay_status = "degraded"
            state.replay_failure = ReplayFailure(
                revision=patch.revision,
                created_at=patch.created_at,
                code=failure.code,
                message=failure.message,
            )
            break
        previous_state = state
        candidate = _fork_state(previous_state)
        candidate_descriptors: list[dict[str, str]] = []
        try:
            _apply_patch(candidate, patch, candidate_descriptors)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            report.reject(
                "malformed-operation",
                f"Patch operations could not be applied atomically: {exc}.",
                patch.revision,
            )
            state.replay_status = "degraded"
            state.replay_failure = ReplayFailure(
                revision=patch.revision,
                created_at=patch.created_at,
                code="malformed-operation",
                message=f"Patch operations could not be applied atomically: {exc}.",
            )
            break
        state = candidate
        descriptors.extend(candidate_descriptors)
        processed_cursors.update(patch.processed_cursors)
        state.validation_messages.extend(patch.admission_messages)
        if accepted_patch_observer is not None:
            accepted_patch_observer(previous_state, patch, state)

    return MaterializationResult(
        state=state,
        reports=reports,
        repository_descriptors=descriptors,
        processed_cursors=processed_cursors,
        patches=replayed_patches,
    )


def apply_valid_patch(state: GraphState, patch: Patch) -> GraphState:
    updated = _fork_state(state)
    _apply_patch(updated, patch, [])
    return updated


def apply_valid_operation(
    state: GraphState,
    patch: Patch,
    operation: dict[str, Any],
) -> GraphState:
    """Stage one already-validated operation without advancing the graph revision."""

    updated = _fork_state(state)
    _apply_patch(updated, patch.model_copy(update={"ops": [operation]}), [])
    updated.revision = state.revision
    return updated


def prepare_patch_bookkeeping(state: GraphState, patch: Patch) -> Patch:
    """Replace RCP-owned Proposal metadata using the graph being appended to."""

    operations = [dict(operation) for operation in patch.ops]
    for operation in operations:
        if operation.get("op") != "create_proposals":
            continue
        proposals: list[dict[str, Any]] = []
        for raw in operation.get("proposals", []):
            proposal = dict(raw)
            related_node_ids, related_edge_ids, related_config_keys = proposal_dependencies(
                state, proposal.get("ops", [])
            )
            proposal.update(
                {
                    "related_node_ids": related_node_ids,
                    "related_edge_ids": related_edge_ids,
                    "related_config_keys": related_config_keys,
                    "base_rev": state.revision,
                    "status": "pending",
                    "created_by": "human" if patch.author == "human" else "agent",
                    "created_by_operation_id": patch.source_operation_id,
                    "raised_rev": 0,
                    "resolved_rev": None,
                    "resolved_by": None,
                    "resolved_by_operation_id": None,
                    "resolution_reason": None,
                    "rejection_reason": None,
                }
            )
            proposals.append(proposal)
        operation["proposals"] = proposals
    return patch.model_copy(update={"ops": operations})


def finalize_patch_bookkeeping(patch: Patch, staged_state: GraphState) -> Patch:
    """Persist Proposal dependencies exactly as staged operations observed them."""

    operations = [dict(operation) for operation in patch.ops]
    for operation in operations:
        if operation.get("op") != "create_proposals":
            continue
        proposals: list[dict[str, Any]] = []
        for raw in operation.get("proposals", []):
            proposal = dict(raw)
            staged = staged_state.proposals.get(proposal.get("id"))
            if staged is not None:
                proposal.update(
                    {
                        "related_node_ids": list(staged.related_node_ids),
                        "related_edge_ids": list(staged.related_edge_ids),
                        "related_config_keys": list(staged.related_config_keys),
                    }
                )
            proposals.append(proposal)
        operation["proposals"] = proposals
    return patch.model_copy(update={"ops": operations})


def _fork_state(state: GraphState) -> GraphState:
    """Fork a state so a failed apply cannot touch the caller's copy.

    Only the mutable containers are copied; the nodes, edges, proposals,
    ambiguities, glossary terms, and coverage inside them are shared. That is
    safe because ``_apply_patch`` never mutates one of those objects in place —
    every change replaces a container slot or the whole attribute — so a patch
    that raises part-way leaves the caller's containers untouched.

    Deep-copying instead made replay quadratic in graph size: it dominated
    materialization at 98% of total time, and a 800-patch log took 15s to open.
    """
    return state.model_copy(
        update={
            "nodes": dict(state.nodes),
            "edges": dict(state.edges),
            "proposals": dict(state.proposals),
            "ambiguities": dict(state.ambiguities),
            "glossary": dict(state.glossary),
            "config_revisions": dict(state.config_revisions),
            "project_truth_scope": list(state.project_truth_scope),
            "validation_messages": list(state.validation_messages),
            "belief_transitions": list(state.belief_transitions),
        }
    )


def _apply_patch(
    state: GraphState, patch: Patch, repository_descriptors: list[dict[str, str]]
) -> None:
    revision = patch.revision
    created_edge_ids: list[str] = []
    for op in patch.ops:
        name = op["op"]
        if name == "create_nodes":
            for raw in op.get("nodes", []):
                data = dict(raw)
                data["created_rev"] = revision
                data["updated_rev"] = revision
                data["standing"] = data.get("standing", "asserted")
                node = NODE_ADAPTER.validate_python(data)
                state.nodes[node.id] = node
        elif name == "update_nodes":
            for update in op.get("nodes", []):
                node = state.nodes[update["id"]]
                changes = update.get("changes", {})
                immutable = sorted(set(changes) & IMMUTABLE_NODE_UPDATE_FIELDS)
                if immutable:
                    raise ValueError(
                        f"node updates cannot change system fields: {', '.join(immutable)}"
                    )
                data = node.model_dump(mode="python")
                data.update(changes)
                data["updated_rev"] = revision
                data["standing"] = node.standing if patch.kind == "approval" else "asserted"
                updated = NODE_ADAPTER.validate_python(data)
                _record_belief_transition(
                    state,
                    node,
                    updated,
                    revision,
                    update.get("cause"),
                )
                state.nodes[node.id] = updated
        elif name == "create_edges":
            for raw in op.get("edges", []):
                data = dict(raw)
                data.setdefault("id", f"{data['source']}::{data['relation']}::{data['target']}")
                if relation := custom_relation(state.ontology, data.get("relation")):
                    data["layer"] = relation.layer
                data["created_rev"] = revision
                edge = Edge.model_validate(data)
                derived = edge_layer(state, edge.source, edge.target, edge.layer)
                if derived != edge.layer:
                    edge = edge.model_copy(update={"layer": derived})
                state.edges[edge.id] = edge
                created_edge_ids.append(edge.id)
        elif name == "remove_edges":
            for edge_id in op.get("edge_ids", []):
                state.edges.pop(edge_id, None)
        elif name == "remove_nodes":
            node_ids = set(op.get("node_ids", []))
            state.nodes = {
                node_id: node for node_id, node in state.nodes.items() if node_id not in node_ids
            }
            state.edges = {
                edge_id: edge
                for edge_id, edge in state.edges.items()
                if edge.source not in node_ids and edge.target not in node_ids
            }
        elif name == "supersede_nodes":
            for item in op.get("nodes", []):
                previous = state.nodes[item["id"]]
                _set_node_status(
                    state,
                    item["id"],
                    "superseded",
                    revision,
                    preserve_standing=patch.kind == "approval",
                )
                _record_belief_transition(
                    state,
                    previous,
                    state.nodes[item["id"]],
                    revision,
                    item.get("cause"),
                )
                target = item.get("superseded_by")
                if target:
                    edge = Edge(
                        id=f"{item['id']}::supersedes::{target}",
                        source=item["id"],
                        target=target,
                        relation="supersedes",
                        explanation=item.get("explanation", ""),
                        created_rev=revision,
                    )
                    state.edges[edge.id] = edge
        elif name == "merge_nodes":
            for item in op.get("merges", []):
                duplicate = item["duplicate"]
                canonical = item["canonical"]
                previous = state.nodes[duplicate]
                _set_node_status(
                    state,
                    duplicate,
                    "superseded",
                    revision,
                    preserve_standing=patch.kind == "approval",
                )
                _record_belief_transition(
                    state,
                    previous,
                    state.nodes[duplicate],
                    revision,
                    item.get("cause"),
                )
                edge = Edge(
                    id=f"{duplicate}::duplicate_of::{canonical}",
                    source=duplicate,
                    target=canonical,
                    relation="duplicate_of",
                    explanation=item.get("explanation", ""),
                    created_rev=revision,
                )
                state.edges[edge.id] = edge
        elif name == "create_ambiguities":
            for raw in op.get("ambiguities", []):
                data = dict(raw)
                data["raised_rev"] = revision
                ambiguity = Ambiguity.model_validate(data)
                state.ambiguities[ambiguity.id] = ambiguity
        elif name == "resolve_ambiguities":
            for resolution in op.get("resolutions", []):
                ambiguity = state.ambiguities[resolution["id"]]
                state.ambiguities[ambiguity.id] = ambiguity.model_copy(
                    update={"status": resolution["status"]}
                )
        elif name == "create_proposals":
            for raw in op.get("proposals", []):
                data = dict(raw)
                related_node_ids, related_edge_ids, related_config_keys = proposal_dependencies(
                    state, data.get("ops", [])
                )
                data["base_rev"] = state.revision
                data["related_node_ids"] = related_node_ids
                data["related_edge_ids"] = related_edge_ids
                data["related_config_keys"] = related_config_keys
                data.setdefault("created_by", "human" if patch.author == "human" else "agent")
                data.setdefault("created_by_operation_id", patch.source_operation_id)
                data["raised_rev"] = revision
                proposal = Proposal.model_validate(data)
                state.proposals[proposal.id] = proposal
        elif name == "resolve_proposals":
            for resolution in op.get("resolutions", []):
                proposal = state.proposals[resolution["id"]]
                state.proposals[proposal.id] = proposal.model_copy(
                    update={
                        "status": resolution["status"],
                        "resolved_rev": revision,
                        "resolved_by": "human" if patch.author == "human" else "agent",
                        "resolved_by_operation_id": patch.source_operation_id,
                        "resolution_reason": resolution.get("reason"),
                        "rejection_reason": resolution.get("reason"),
                    }
                )
        elif name == "withdraw_proposals":
            for withdrawal in op.get("proposals", []):
                proposal = state.proposals[withdrawal["id"]]
                state.proposals[proposal.id] = proposal.model_copy(
                    update={
                        "status": "withdrawn",
                        "resolved_rev": revision,
                        "resolved_by": "agent",
                        "resolved_by_operation_id": patch.source_operation_id,
                        "resolution_reason": withdrawal.get("reason"),
                    }
                )
        elif name == "upsert_glossary":
            for raw in op.get("terms", []):
                data = dict(raw)
                data["updated_rev"] = revision
                term = GlossaryTerm.model_validate(data)
                state.glossary[term.term] = term
        elif name == "set_coverage":
            previous = state.coverage
            data = previous.model_dump(mode="python")
            data.update(op.get("coverage", {}))
            data["repositories_seen"] = sorted(set(data.get("repositories_seen", [])))
            data["repositories_never_seen"] = sorted(set(data.get("repositories_never_seen", [])))
            data["sessions_read"] = sorted(set(data.get("sessions_read", [])))
            data["sessions_skipped"] = sorted(set(data.get("sessions_skipped", [])))
            state.coverage = CoverageBoundary.model_validate(data)
        elif name == "set_standing":
            node = state.nodes[op["node_id"]]
            state.nodes[node.id] = node.model_copy(
                update={"standing": Standing(op["standing"]), "updated_rev": revision}
            )
        elif name == "set_project_truth_scope":
            new_scope = set(op.get("truth_scope", []))
            state.project_truth_scope = sorted(new_scope)
            state.config_revisions["project_truth_scope"] = revision
            seen = set(state.coverage.repositories_seen)
            never_seen = set(state.coverage.repositories_never_seen)
            never_seen.update(new_scope - seen)
            never_seen.intersection_update(new_scope)
            state.coverage = state.coverage.model_copy(
                update={"repositories_never_seen": sorted(never_seen)}
            )
            if descriptor := op.get("repository"):
                repository_descriptors.append(dict(descriptor))
        elif name == "set_ontology":
            state.ontology = OntologyState.model_validate(op["ontology"])
            state.config_revisions["ontology"] = revision

    # A legal edge may forward-reference a node created later in this patch.
    # Its first pass necessarily uses the relation's declared fallback layer;
    # derive it once more from the completed staged graph without reordering ops.
    for edge_id in dict.fromkeys(created_edge_ids):
        edge = state.edges.get(edge_id)
        if edge is None:
            continue
        derived = edge_layer(state, edge.source, edge.target, edge.layer)
        if derived != edge.layer:
            state.edges[edge_id] = edge.model_copy(update={"layer": derived})

    state.revision = max(state.revision, revision)
    if patch.kind in {"seed", "refresh"}:
        state.last_refresh_at = patch.created_at


def _set_node_status(
    state: GraphState,
    node_id: str,
    status: str,
    revision: int,
    *,
    preserve_standing: bool,
) -> None:
    node = state.nodes[node_id]
    data: dict[str, Any] = node.model_dump(mode="python")
    data["status"] = status
    data["updated_rev"] = revision
    data["standing"] = node.standing if preserve_standing else "asserted"
    state.nodes[node_id] = NODE_ADAPTER.validate_python(data)


def _record_belief_transition(
    state: GraphState,
    previous: ProjectNode,
    updated: ProjectNode,
    revision: int,
    cause: Any,
) -> None:
    if (
        not isinstance(previous, Hypothesis)
        or not isinstance(updated, Hypothesis)
        or previous.status == updated.status
        or not isinstance(cause, dict)
    ):
        return
    state.belief_transitions.append(
        BeliefTransition(
            hypothesis_id=previous.id,
            from_status=previous.status,
            to_status=updated.status,
            revision=revision,
            cause=dict(cause),
        )
    )
