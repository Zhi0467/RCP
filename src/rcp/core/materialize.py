from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from rcp.core.models import (
    GraphState,
    Patch,
    Proposal,
    ReplayFailure,
)
from rcp.core.ontology import edge_layer
from rcp.core.operations import (
    CreateProposalsOperation,
    GraphOperation,
    UpsertGlossaryOperation,
)
from rcp.core.validation import (
    ValidationContext,
    ValidationReport,
    proposal_dependencies,
    validate_patch,
)
from rcp.core.validation.apply_ops import ApplyContext
from rcp.core.validation.registry import OP_RULES

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
    context: ValidationContext,
    accepted_patch_observer: AcceptedPatchObserver | None = None,
) -> MaterializationResult:
    """Replay patches, optionally observing successful applications through a read-only callback."""

    state = GraphState(project_truth_scope=list(context.project_truth_scope))
    return materialize_patches_from_state(
        patches,
        initial_state=state,
        context=context,
        accepted_patch_observer=accepted_patch_observer,
    )


def materialize_patches_from_state(
    patches: Iterable[Patch],
    *,
    initial_state: GraphState,
    context: ValidationContext,
    accepted_patch_observer: AcceptedPatchObserver | None = None,
) -> MaterializationResult:
    """Replay a tail from an already materialized immutable graph state."""

    replayed_patches = list(patches)
    state = initial_state
    reports: dict[int, ValidationReport] = {}
    descriptors: list[dict[str, str]] = []
    processed_cursors: dict[str, str] = {}

    for patch in replayed_patches:
        if patch.admission == "rejected":
            report = ValidationReport()
            report.messages.extend(patch.admission_messages)
            reports[patch.revision] = report
            state = state.model_copy(
                update={
                    "revision": max(state.revision, patch.revision),
                    "validation_messages": [
                        *state.validation_messages,
                        *patch.admission_messages,
                    ],
                }
            )
            continue

        report = validate_patch(
            state,
            patch,
            replace(context, project_truth_scope=state.project_truth_scope, mode="replay"),
        )
        report.messages.extend(patch.admission_messages)
        reports[patch.revision] = report
        if report.rejected:
            failure = next(item for item in report.messages if item.level == "reject")
            state = state.model_copy(
                update={
                    "replay_status": "degraded",
                    "replay_failure": ReplayFailure(
                        revision=patch.revision,
                        created_at=patch.created_at,
                        code=failure.code,
                        message=failure.message,
                    ),
                }
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
            state = state.model_copy(
                update={
                    "replay_status": "degraded",
                    "replay_failure": ReplayFailure(
                        revision=patch.revision,
                        created_at=patch.created_at,
                        code="malformed-operation",
                        message=f"Patch operations could not be applied atomically: {exc}.",
                    ),
                }
            )
            break
        state = candidate
        descriptors.extend(candidate_descriptors)
        processed_cursors.update(patch.processed_cursors)
        state = state.model_copy(
            update={
                "validation_messages": [
                    *state.validation_messages,
                    *patch.admission_messages,
                ]
            }
        )
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
    operation: GraphOperation,
) -> GraphState:
    """Stage one already-validated operation without advancing the graph revision."""

    updated = _fork_state(state)
    _apply_patch(updated, patch.model_copy(update={"ops": [operation]}), [])
    updated.revision = state.revision
    return updated


def apply_transition_generated_operation(
    state: GraphState,
    patch: Patch,
    operation: GraphOperation,
) -> GraphState:
    """Stage one manager-owned system-field action during transition closure."""

    updated = _fork_state(state)
    _apply_patch(
        updated,
        patch.model_copy(update={"ops": [operation]}),
        [],
        system_field_operation_indexes={0},
    )
    updated.revision = state.revision
    return updated


def prepare_patch_bookkeeping(state: GraphState, patch: Patch) -> Patch:
    """Resolve admission metadata and glossary spelling before recording operations."""

    operations: list[GraphOperation] = []
    # Match the inline lookup's case-insensitive identity, preserving its first
    # sorted spelling even when old history contains multiple cased variants.
    # Persist this choice in the new Patch; never change historical replay.
    glossary_spelling: dict[str, str] = {}
    for term in sorted(state.glossary):
        glossary_spelling.setdefault(term.lower(), term)
    for operation in patch.ops:
        if isinstance(operation, UpsertGlossaryOperation):
            terms = []
            for term in operation.terms:
                spelling = glossary_spelling.setdefault(term.term.lower(), term.term)
                terms.append(term.model_copy(update={"term": spelling}))
            operations.append(operation.model_copy(update={"terms": terms}))
            continue
        if not isinstance(operation, CreateProposalsOperation):
            operations.append(operation)
            continue
        proposals: list[Proposal] = []
        for proposal in operation.proposals:
            related_node_ids, related_edge_ids, related_config_keys = proposal_dependencies(
                state, proposal.ops
            )
            proposals.append(
                proposal.model_copy(
                    update={
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
            )
        operations.append(operation.model_copy(update={"proposals": proposals}))
    return patch.model_copy(update={"ops": operations})


def finalize_patch_bookkeeping(patch: Patch, staged_state: GraphState) -> Patch:
    """Persist Proposal dependencies exactly as staged operations observed them."""

    operations: list[GraphOperation] = []
    for operation in patch.ops:
        if not isinstance(operation, CreateProposalsOperation):
            operations.append(operation)
            continue
        proposals: list[Proposal] = []
        for proposal in operation.proposals:
            staged = staged_state.proposals.get(proposal.id)
            if staged is not None:
                proposal = proposal.model_copy(
                    update={
                        "related_node_ids": list(staged.related_node_ids),
                        "related_edge_ids": list(staged.related_edge_ids),
                        "related_config_keys": list(staged.related_config_keys),
                    }
                )
            proposals.append(proposal)
        operations.append(operation.model_copy(update={"proposals": proposals}))
    return patch.model_copy(update={"ops": operations})


def _fork_state(state: GraphState) -> GraphState:
    """Fork a state so a failed apply cannot touch the caller's copy.

    Only the mutable containers are copied; the nodes, edges, proposals,
    ambiguities and glossary terms inside them are shared. That is
    safe because ``_apply_patch`` never mutates one of those objects in place —
    every change replaces a container slot or the whole attribute — so a patch
    that raises part-way leaves the caller's containers untouched
    (`tests/test_history.py::test_patch_failing_part_way_leaks_no_earlier_operation`,
    `tests/test_glossary_authoring.py::test_failed_apply_after_glossary_upsert_preserves_shared_previous_term`).

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
    state: GraphState,
    patch: Patch,
    repository_descriptors: list[dict[str, str]],
    *,
    system_field_operation_indexes: set[int] | None = None,
) -> None:
    revision = patch.revision
    created_edge_ids: list[str] = []
    generated_operation_indexes = set(system_field_operation_indexes or ())
    generated_operation_indexes.update(
        {item.operation_index for item in patch.transition.generated_actions}
        if patch.transition is not None
        else set()
    )
    for operation_index, op in enumerate(patch.ops):
        OP_RULES[op.op].apply(
            op,
            ApplyContext(
                state,
                patch,
                repository_descriptors,
                created_edge_ids,
                system_fields=operation_index in generated_operation_indexes,
            ),
        )

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
    # The ingestion watermark moves only for an ingestion Patch that
    # materialized, so a failed or rejected run and every conversation turn
    # leave it alone. It is an overlap-tolerant timestamp, not an exactly-once
    # cursor (tests/test_direct_ingestion_run.py::
    # test_failed_graph_run_stages_no_conversation_bytes_and_keeps_watermark,
    # tests/test_direct_ingestion_contract.py).
    if patch.kind in {"seed", "refresh"}:
        state.last_refresh_at = patch.created_at
