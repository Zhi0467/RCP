"""Pure whole-field merge comparison and core-owned review interference analysis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, JsonValue

from rcp.core.authority import classify_graph_changes, derived_agent_action
from rcp.core.materialize import (
    apply_valid_patch,
    prepare_patch_bookkeeping,
    proposal_filing_dependency_rule,
)
from rcp.core.models import (
    GatedCard,
    GraphState,
    Patch,
    Proposal,
    graph_fields,
)
from rcp.core.operations import (
    CreateProposalsOperation,
    GraphOperation,
    ProposalOperation,
    graph_operations_from_proposal,
)
from rcp.core.transitions import GraphTransitionManager
from rcp.core.validation import ValidationContext
from rcp.core.validation.proposals import (
    ProposalDependenciesAndEffects,
    connected_proposal_groups,
    proposal_approval_operations,
    proposal_dependencies_and_effects,
    proposal_is_stale,
)
from rcp.history.delta import semantic_document
from rcp.runs.merge_contract import (
    Collection,
    FieldConflict,
    IncidentEdgeConflict,
    MergeChange,
    MergeConflict,
    MergeDiagnostic,
    MergeHiddenClash,
    MergePlan,
    MergePlanningContext,
    MergeRefused,
    MergeValue,
    MissingValue,
    NodeGroupConflict,
    NodeGroupPromotion,
    PresenceConflict,
    PresentValue,
    PriorReview,
    ReviewGroup,
    merge_contract_digest,
)

_COLLECTIONS: tuple[Collection, ...] = ("nodes", "edges", "proposals", "ambiguities", "glossary")
_GLOBALS: tuple[Collection, ...] = ("project_truth_scope", "ontology")


def _key(collection: Collection, entity_id: str | None, field: str | None) -> str:
    return merge_contract_digest([collection, entity_id, field])


def _value(value: BaseModel | None) -> MergeValue:
    return MissingValue() if value is None else PresentValue(value=semantic_document(value))


def _field(value: BaseModel | None, name: str) -> MergeValue:
    if value is None or name not in graph_fields(type(value), role="content"):
        return MissingValue()
    return PresentValue(value=semantic_document(value)[name])


def _fields(*values: BaseModel | None) -> list[str]:
    return sorted(set().union(*(graph_fields(type(v), role="content") for v in values if v)))


def _change(
    collection: Collection,
    entity_id: str | None,
    field: str | None,
    base: MergeValue,
    branch: MergeValue,
    destination: MergeValue,
) -> MergeChange:
    return MergeChange(
        change_id=_key(collection, entity_id, field),
        collection=collection,
        entity_id=entity_id,
        field_name=field,
        base=base,
        branch=branch,
        destination=destination,
    )


def _field_conflict(change: MergeChange) -> FieldConflict:
    assert change.field_name is not None or change.entity_id is None
    return FieldConflict(
        conflict_id=change.change_id,
        collection=change.collection,
        entity_id=change.entity_id,
        field_name=change.field_name or change.collection,
        base=change.base,
        branch=change.branch,
        destination=change.destination,
        allowed_answer_kinds=("branch", "destination")
        if change.collection == "nodes" and change.field_name in {"status", "standing"}
        else ("branch", "destination", "merged"),
    )


def _incident_changes(
    node_id: str, base: GraphState, branch: GraphState, destination: GraphState, *, removed_on: str
) -> tuple[MergeChange, ...]:
    competing = destination if removed_on == "branch" else branch
    return tuple(
        _change(
            "edges",
            edge_id,
            None,
            _value(base.edges.get(edge_id)),
            _value(branch.edges.get(edge_id)),
            _value(destination.edges.get(edge_id)),
        )
        for edge_id, edge in sorted(competing.edges.items())
        if node_id in {edge.source, edge.target} and _value(base.edges.get(edge_id)) != _value(edge)
    )


def compare_graphs(
    base: GraphState, branch: GraphState, destination: GraphState
) -> tuple[tuple[MergeChange, ...], tuple[MergeConflict, ...]]:
    """Return source candidates (including conflicted ones) and explicit conflicts.

    A field is indivisible: nested objects and lists are compared as whole values.
    Destination-only and already-agreed changes need no source application.
    Presence and incident-edge conflicts own the complete affected entity.
    """
    changes: list[MergeChange] = []
    conflicts: list[MergeConflict] = []
    removed_nodes = base.nodes.keys() - branch.nodes.keys()
    # Node removal owns incident deletion. Emitting a second edge removal could
    # apply that half directly while the node removal waits for human review.
    incident_edge_ids = {
        edge_id
        for edge_id, edge in base.edges.items()
        if edge_id not in branch.edges and removed_nodes.intersection((edge.source, edge.target))
    }
    for collection in _COLLECTIONS:
        before: Mapping[str, BaseModel] = getattr(base, collection)
        source: Mapping[str, BaseModel] = getattr(branch, collection)
        target: Mapping[str, BaseModel] = getattr(destination, collection)
        for identity in sorted(before.keys() | source.keys() | target.keys()):
            old, new, current = before.get(identity), source.get(identity), target.get(identity)
            b, s, d = _value(old), _value(new), _value(current)
            if old is not None and (new is None) != (current is None):
                removed_on = "branch" if new is None else "destination"
                incident = (
                    _incident_changes(identity, base, branch, destination, removed_on=removed_on)
                    if collection == "nodes"
                    else ()
                )
                if incident:
                    conflicts.append(
                        IncidentEdgeConflict(
                            conflict_id=_key(collection, identity, None),
                            node_id=identity,
                            base=b,
                            branch=s,
                            destination=d,
                            removed_on=removed_on,
                            incident_edges=incident,
                        )
                    )
                    incident_edge_ids.update(c.entity_id for c in incident if c.entity_id)
                elif (d if new is None else s) != b:
                    conflicts.append(
                        PresenceConflict(
                            conflict_id=_key(collection, identity, None),
                            collection=collection,
                            entity_id=identity,
                            base=b,
                            branch=s,
                            destination=d,
                            removed_on=removed_on,
                        )
                    )
                if s != b or incident:
                    changes.append(_change(collection, identity, None, b, s, d))
                continue
            if s in (b, d):
                continue
            if new is None or current is None:
                changes.append(_change(collection, identity, None, b, s, d))
                continue
            # Concurrent creation also compares whole fields against absence.
            for name in _fields(old, new, current):
                bv, sv, dv = _field(old, name), _field(new, name), _field(current, name)
                if sv in (bv, dv):
                    continue
                change = _change(collection, identity, name, bv, sv, dv)
                changes.append(change)
                if dv != bv:
                    conflicts.append(_field_conflict(change))
    for collection in _GLOBALS:
        values: list[JsonValue] = []
        for graph in (base, branch, destination):
            value = getattr(graph, collection)
            values.append(semantic_document(value) if isinstance(value, BaseModel) else value)
        b, s, d = (PresentValue(value=value) for value in values)
        if s in (b, d):
            continue
        change = _change(collection, None, None, b, s, d)
        changes.append(change)
        if d != b:
            conflicts.append(_field_conflict(change))
    # The incident conflict owns those edge edits, avoiding contradictory answers
    # for the same work in both a presence decision and a separate edge field.
    changes = [
        c for c in changes if not (c.collection == "edges" and c.entity_id in incident_edge_ids)
    ]
    conflicts = [
        c
        for c in conflicts
        if not (
            isinstance(c, (FieldConflict, PresenceConflict))
            and c.collection == "edges"
            and c.entity_id in incident_edge_ids
        )
    ]
    return tuple(changes), tuple(conflicts)


@dataclass(frozen=True)
class ReviewCandidate:
    changes: tuple[MergeChange, ...]
    operations: tuple[ProposalOperation, ...]
    replaces_proposal_ids: tuple[str, ...] = ()
    carried_proposal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateGroup:
    """One coherent builder encoding, before core classifies its authority."""

    changes: tuple[MergeChange, ...]
    operations: tuple[GraphOperation, ...]


def _draft(operations: Sequence[ProposalOperation]) -> Proposal:
    return Proposal(
        id="proposal/merge-plan-probe",
        title="Merge review",
        card=GatedCard(),
        ops=list(operations),
        dependency_rule=proposal_filing_dependency_rule(len(operations), branch_merge=True),
    )


def _interferes(
    left: ProposalDependenciesAndEffects, right: ProposalDependenciesAndEffects
) -> bool:
    return bool(left.effects & right.dependencies or right.effects & left.dependencies)


def group_reviews(
    state: GraphState, candidates: Sequence[ReviewCandidate]
) -> tuple[ReviewGroup, ...]:
    """Connected approval groups using only core's shared dependency/effect API.

    Recompute combined footprints until stable: combined approval can generate
    effects not present in either individual draft. Read/read overlap never joins.
    """
    memberships = connected_proposal_groups(
        state,
        [_draft(candidate.operations) for candidate in candidates],
        links=[frozenset(candidate.replaces_proposal_ids) for candidate in candidates],
    )
    components = [[candidates[index] for index in component] for component in memberships]
    footprints = [
        proposal_dependencies_and_effects(
            state, _draft([op for candidate in component for op in candidate.operations])
        )
        for component in components
    ]
    groups = []
    for component, footprint in zip(components, footprints, strict=True):
        changes = tuple(
            sorted(
                {c.change_id: c for candidate in component for c in candidate.changes}.values(),
                key=lambda c: (c.collection, c.entity_id or "", c.field_name or ""),
            )
        )
        subjects = sorted(
            {(c.collection, c.entity_id) for c in changes},
            key=lambda pair: (pair[0], pair[1] or ""),
        )
        operations = [op for candidate in component for op in candidate.operations]
        groups.append(
            ReviewGroup(
                subject_key=merge_contract_digest(subjects),
                changes=changes,
                footprint=footprint,
                effect_digest=_effect_digest(state, operations, footprint),
                carried_proposal_ids=tuple(
                    sorted({p for candidate in component for p in candidate.carried_proposal_ids})
                ),
                replaces_proposal_ids=tuple(
                    sorted({p for candidate in component for p in candidate.replaces_proposal_ids})
                ),
            )
        )
    return tuple(sorted(groups, key=lambda g: g.subject_key))


def _effect_digest(
    state: GraphState,
    operations: Sequence[ProposalOperation],
    footprint: ProposalDependenciesAndEffects,
) -> str:
    """Bind core's actual approval outputs, not just the identities it writes."""
    proposal = _draft(operations)
    before = state.model_copy(update={"proposals": {**state.proposals, proposal.id: proposal}})
    patch = Patch(
        kind="approval",
        author="human",
        producer="human",
        summary="Inspect merge review",
        revision=state.revision + 1,
        created_at=datetime(2000, 1, 1, tzinfo=UTC),
        ops=proposal_approval_operations(state, proposal),
    )
    after = GraphTransitionManager().prepare_validated(before, [patch]).projection.graph
    effects: list[object] = []
    for resource in sorted(footprint.effects, key=lambda r: (r.kind, r.id)):
        if resource.kind == "node":
            value = _value(after.nodes.get(resource.id)).model_dump(mode="json")
        elif resource.kind == "edge":
            value = _value(after.edges.get(resource.id)).model_dump(mode="json")
        elif resource.kind == "incident_edges":
            value = sorted(
                e.id for e in after.edges.values() if resource.id in {e.source, e.target}
            )
        else:
            value = getattr(after, resource.id)
            if isinstance(value, BaseModel):
                value = semantic_document(value)
        effects.append([resource.kind, resource.id, value])
    return merge_contract_digest(
        {
            "ops": [op.model_dump(mode="json", exclude_unset=True) for op in operations],
            "effects": effects,
        }
    )


def _source_for_comparison(context: MergePlanningContext, baseline: GraphState) -> GraphState:
    # Source resolutions never resolve a destination Proposal. Current live
    # source filings retain identity; core alone decides whether they are stale.
    proposals = dict(context.source_graph.proposals)
    for identity, proposal in context.source_graph.proposals.items():
        if proposal.status != "pending" or proposal_is_stale(context.source_graph, proposal):
            if identity in baseline.proposals:
                proposals[identity] = baseline.proposals[identity]
            else:
                proposals.pop(identity)
    return context.source_graph.model_copy(update={"proposals": proposals})


def plan_merge(context: MergePlanningContext) -> MergePlan:
    """Plan against the delivered source baseline, never re-deliver settled work."""
    # Revalidate to reject a model_copy that widened captured authority or scope.
    context = MergePlanningContext.model_validate(context.model_dump())
    baseline = context.previous_source_graph or context.base_graph
    source = _source_for_comparison(context, baseline)
    changes, conflicts = compare_graphs(baseline, source, context.destination_graph)
    changes = retain_pending_effects(context, changes)
    conflicts = _pending_conflicts(context, changes, conflicts)
    while True:
        try:
            direct, reviews = _route_changes(context, changes, conflicts)
        except MergeHiddenClash as clash:
            provisional = MergePlan(
                **context.model_dump(), direct_changes=changes, conflicts=conflicts
            )
            promoted = promote_node_group(provisional, clash.promotion)
            conflicts = promoted.conflicts
            by_id = {change.change_id: change for change in changes}
            for conflict in conflicts:
                if isinstance(conflict, NodeGroupConflict):
                    by_id.update(
                        (
                            field.conflict_id,
                            _change(
                                field.collection,
                                field.entity_id,
                                field.field_name,
                                field.base,
                                field.branch,
                                field.destination,
                            ),
                        )
                        for field in conflict.fields
                    )
            changes = tuple(by_id.values())
        else:
            return MergePlan(
                **context.model_dump(),
                direct_changes=direct,
                conflicts=conflicts,
                review_groups=reviews,
            )


def pending_reviews(context: MergePlanningContext) -> tuple[PriorReview, ...]:
    """Only the latest canonical filing for this source/destination/subject counts."""
    latest: dict[str, PriorReview] = {}
    for review in sorted(
        context.prior_reviews,
        key=lambda r: (r.provenance.branch_head.revision, r.proposal.raised_rev, r.proposal.id),
    ):
        if (
            review.provenance.branch_id == context.provenance.branch_id
            and review.provenance.rebased_main_head.target == context.destination_head.target
        ):
            latest[review.subject_key] = review
    return tuple(
        r
        for _, r in sorted(latest.items())
        if r.proposal.status == "pending"
        and not proposal_is_stale(context.destination_graph, r.proposal)
    )


def outstanding_review_changes(
    filing_graph: GraphState,
    destination: GraphState,
    proposal: Proposal,
    *,
    created_at: datetime,
) -> tuple[MergeChange, ...]:
    """Derive outstanding effects from the filing snapshot, or none if stale.

    Historical operations never run against the live destination. Approval's
    implicit effects belong to core's review footprint, not new source edits.
    """
    if proposal_is_stale(destination, proposal):
        return ()
    patch = Patch(
        kind="approval",
        author="human",
        producer="human",
        summary="Inspect outstanding merge work",
        revision=filing_graph.revision + 1,
        created_at=created_at,
        ops=graph_operations_from_proposal(proposal.ops),
    )
    proposed = apply_valid_patch(filing_graph, patch)
    changes, _ = compare_graphs(filing_graph, proposed, destination)
    return changes


def retain_pending_effects(
    context: MergePlanningContext, changes: tuple[MergeChange, ...]
) -> tuple[MergeChange, ...]:
    """Include outstanding earlier work on a newly touched review subject.

    New source edits override older pending values at the same coordinate. A
    rejected/approved review contributes no changes: the delivered snapshot is
    already the source baseline, preserving subsequent destination judgments.
    """
    retained: dict[str, MergeChange] = {}
    baseline = context.previous_source_graph or context.base_graph
    source_changes, _ = compare_graphs(baseline, context.source_graph, baseline)
    subjects = {(c.collection, c.entity_id) for c in source_changes}
    changed_coordinates = {c.change_id for c in source_changes}
    rebased: dict[str, MergeChange] = {}
    for review in pending_reviews(context):
        outstanding = review.outstanding
        if subjects.intersection((c.collection, c.entity_id) for c in outstanding):
            retained.update(
                (c.change_id, c) for c in outstanding if c.change_id not in changed_coordinates
            )
            old_coordinates = {c.change_id for c in outstanding}
            # Agreement with the destination still cancels an outstanding old
            # effect. Keep that explicit no-op descriptor for the builder's
            # withdrawal/replacement decision instead of resurrecting old ops.
            for change in source_changes:
                if change.change_id not in old_coordinates:
                    continue
                value = _coordinate_value(context.destination_graph, change)
                # Undelivered work never became the destination's baseline.
                # Reverting it to the fork value cancels it without undoing
                # newer destination work at that coordinate.
                branch = (
                    value
                    if change.branch == _coordinate_value(context.base_graph, change)
                    else change.branch
                )
                rebased[change.change_id] = change.model_copy(
                    update={"base": value, "branch": branch, "destination": value}
                )
                retained[change.change_id] = rebased[change.change_id]
    for change in changes:
        # Presence supersedes older fields on that entity (including a source
        # deletion after an earlier pending content review).
        if change.field_name is None:
            retained = {
                k: c
                for k, c in retained.items()
                if (c.collection, c.entity_id) != (change.collection, change.entity_id)
            }
        retained[change.change_id] = rebased.get(change.change_id, change)
    # An older removal and a newer field edit are competing presence choices,
    # not two operations that can be applied together. Preserve the edited
    # surviving entity as the keep-node answer, including destination-only work.
    for removal in tuple(retained.values()):
        if removal.field_name is not None or removal.branch.present:
            continue
        edits = {
            c.change_id: c
            for c in (*source_changes, *retained.values())
            if (c.collection, c.entity_id) == (removal.collection, removal.entity_id)
            and c.field_name is not None
        }
        if not edits:
            continue
        assert isinstance(removal.destination, PresentValue)
        assert isinstance(removal.destination.value, dict)
        surviving = dict(removal.destination.value)
        for edit in edits.values():
            assert edit.field_name is not None
            if isinstance(edit.branch, PresentValue):
                surviving[edit.field_name] = edit.branch.value
            else:
                surviving.pop(edit.field_name, None)
            retained.pop(edit.change_id, None)
        retained[removal.change_id] = removal.model_copy(
            update={"destination": PresentValue(value=surviving)}
        )
    return tuple(
        sorted(
            retained.values(), key=lambda c: (c.collection, c.entity_id or "", c.field_name or "")
        )
    )


def _coordinate_value(state: GraphState, change: MergeChange) -> MergeValue:
    value = getattr(state, change.collection)
    if change.entity_id is None:
        return PresentValue(
            value=semantic_document(value) if isinstance(value, BaseModel) else value
        )
    entity = value.get(change.entity_id)
    return _field(entity, change.field_name) if change.field_name else _value(entity)


def _pending_conflicts(
    context: MergePlanningContext,
    changes: tuple[MergeChange, ...],
    conflicts: tuple[MergeConflict, ...],
) -> tuple[MergeConflict, ...]:
    """Reconcile conflicts with pending coordinates' effective comparison values."""
    by_id = {c.change_id: c for c in changes}
    result: list[MergeConflict] = []
    for conflict in conflicts:
        if isinstance(conflict, FieldConflict):
            change = by_id.get(conflict.conflict_id)
            if (
                change is None
                or change.destination == change.base
                or change.branch == change.destination
            ):
                continue
            conflict = _field_conflict(change)
        result.append(conflict)
    present = {c.conflict_id for c in result}
    for change in changes:
        if (
            change.field_name is None
            and change.entity_id is not None
            and not change.branch.present
            and change.destination.present
            and change.change_id not in present
        ):
            baseline = context.previous_source_graph or context.base_graph
            previous = getattr(baseline, change.collection).get(change.entity_id)
            source = getattr(context.source_graph, change.collection).get(change.entity_id)
            edited = (
                previous is not None and source is not None and _value(previous) != _value(source)
            )
            if change.destination == change.base and not edited:
                continue
            result.append(
                PresenceConflict(
                    conflict_id=change.change_id,
                    collection=change.collection,
                    entity_id=change.entity_id,
                    base=change.base,
                    branch=change.branch,
                    destination=change.destination,
                    removed_on="branch",
                )
            )
    return tuple(result)


def _route_changes(
    context: MergePlanningContext,
    changes: tuple[MergeChange, ...],
    conflicts: tuple[MergeConflict, ...],
) -> tuple[tuple[MergeChange, ...], tuple[ReviewGroup, ...]]:
    if not changes:
        return (), ()
    candidates = _encode_candidates(context, changes, conflicts)
    return classify_candidates(context, candidates, conflicts)


def _encode_candidates(
    context: MergePlanningContext,
    changes: tuple[MergeChange, ...],
    conflicts: tuple[MergeConflict, ...],
) -> tuple[CandidateGroup, ...]:
    """Use slice 3's single operation encoder, including semantic retirement.

    The local import keeps the planner/builder relationship acyclic. The sibling
    module arrives with slice 3; no encoder or semantic-op inference lives here.
    """
    from rcp.runs.merge_patch import construct_effects  # pyright: ignore[reportMissingImports]

    by_coordinate = {(c.collection, c.entity_id, c.field_name): c for c in changes}
    for conflict in conflicts:
        if isinstance(conflict, IncidentEdgeConflict) and conflict.removed_on == "destination":
            by_coordinate.update(
                ((c.collection, c.entity_id, c.field_name), c) for c in conflict.incident_edges
            )
    provisional = MergePlan(**context.model_dump(), direct_changes=changes, conflicts=conflicts)
    effects = construct_effects(provisional, {key: c.branch for key, c in by_coordinate.items()})
    candidates = tuple(
        CandidateGroup(
            tuple(
                by_coordinate[key]
                for key in sorted(
                    effect.coordinates, key=lambda key: (key[0], key[1] or "", key[2] or "")
                )
            ),
            tuple(effect.operations),
        )
        for effect in effects
    )
    encoded = {c.change_id for candidate in candidates for c in candidate.changes}
    unencoded = tuple(c for c in by_coordinate.values() if c.change_id not in encoded)
    if any(c.branch != c.destination for c in unencoded):
        raise MergeRefused(
            (
                MergeDiagnostic(
                    code="unencoded-merge-change",
                    message="The builder did not encode a changed merge coordinate.",
                    related_node_ids=tuple(
                        sorted(
                            {
                                c.entity_id
                                for c in unencoded
                                if c.collection == "nodes" and c.entity_id
                            }
                        )
                    ),
                ),
            )
        )
    if unencoded:
        candidates += (CandidateGroup(unencoded, ()),)
    # Creation and its connecting edges are one authority candidate. Core must
    # see both together to recognize same-Patch creation authority; status and
    # standing follow-ups emitted separately by the builder stay separate.
    created_ids = {
        node.id
        for candidate in candidates
        for op in candidate.operations
        if op.op == "create_nodes"
        for node in op.nodes
    }
    creation = [
        candidate
        for candidate in candidates
        if any(
            op.op == "create_nodes"
            or (
                op.op == "create_edges"
                and any(
                    edge.source in created_ids or edge.target in created_ids for edge in op.edges
                )
            )
            for op in candidate.operations
        )
    ]
    if not creation:
        return candidates
    joined = CandidateGroup(
        tuple({c.change_id: c for candidate in creation for c in candidate.changes}.values()),
        tuple(op for candidate in creation for op in candidate.operations),
    )
    return (joined, *(c for c in candidates if c not in creation))


def classify_candidates(
    context: MergePlanningContext,
    candidates: Sequence[CandidateGroup],
    conflicts: tuple[MergeConflict, ...] = (),
) -> tuple[tuple[MergeChange, ...], tuple[ReviewGroup, ...]]:
    """Route coherent builder candidates using the captured core authority only."""
    if not candidates:
        return (), ()
    conflicted = {c.conflict_id for c in conflicts} | {
        change.change_id
        for conflict in conflicts
        if isinstance(conflict, IncidentEdgeConflict)
        for change in conflict.incident_edges
    }
    conflicted.update(
        field.conflict_id
        for conflict in conflicts
        if isinstance(conflict, NodeGroupConflict)
        for field in conflict.fields
    )
    direct: list[MergeChange] = []
    reviews: list[ReviewCandidate] = []
    admitted: list[GraphOperation] = []
    carried: list[ReviewCandidate] = []
    pending = [(review, review.outstanding) for review in pending_reviews(context)]
    for candidate in candidates:
        subjects = {(c.collection, c.entity_id) for c in candidate.changes}
        predecessors = tuple(
            review.proposal.id
            for review, changes in pending
            if subjects.intersection((c.collection, c.entity_id) for c in changes)
        )
        if not candidate.operations:
            if any(c.branch != c.destination for c in candidate.changes):
                raise ValueError("a nonempty effect must encode at least one operation")
            if predecessors:
                reviews.append(ReviewCandidate(candidate.changes, (), predecessors))
            continue
        prefix = context.captured_authority.patch_template.model_copy(update={"ops": admitted})
        patch = prefix.model_copy(update={"ops": [*admitted, *candidate.operations]})
        patch = patch.model_copy(
            update={"agent_action": derived_agent_action(context.destination_graph, patch)}
        )
        operation_groups = ((tuple(range(len(admitted))),) if admitted else ()) + (
            tuple(range(len(admitted), len(patch.ops))),
        )
        patch = prepare_patch_bookkeeping(context.destination_graph, patch)
        results = classify_graph_changes(
            context.destination_graph,
            patch,
            authority=context.captured_authority,
            project_id=context.captured_authority.project_id,
            apply_target=context.destination_head.target,
            authorized_by=context.authorized_by,
            context=ValidationContext(
                project_truth_scope=context.destination_graph.project_truth_scope,
                merge_source=context.source_graph,
            ),
            operation_groups=operation_groups,
        )
        result = next((item for item in results if item.disposition == "refused"), results[-1])
        if result.disposition == "admitted":
            direct.extend(c for c in candidate.changes if c.change_id not in conflicted)
            admitted.extend(candidate.operations)
            for operation in candidate.operations:
                if operation.op == "create_proposals":
                    for proposal in operation.proposals:
                        proposal_changes = tuple(
                            change
                            for change in candidate.changes
                            if change.collection == "proposals" and change.entity_id == proposal.id
                        )
                        carried.append(
                            ReviewCandidate(
                                proposal_changes,
                                tuple(proposal.ops),
                                carried_proposal_ids=(proposal.id,),
                            )
                        )
            if predecessors:
                reviews.append(ReviewCandidate(candidate.changes, (), predecessors))
        elif result.disposition == "reviewable":
            reviews.append(
                ReviewCandidate(candidate.changes, result.review_operations, predecessors)
            )
        elif result.disposition == "clash":
            diagnostics = tuple(
                MergeDiagnostic(
                    code=d.code, message=d.message, related_node_ids=tuple(d.related_node_ids)
                )
                for d in result.diagnostics
            )
            raise MergeHiddenClash(
                NodeGroupPromotion(
                    node_ids=tuple(sorted({n for d in diagnostics for n in d.related_node_ids})),
                    diagnostics=diagnostics,
                )
            )
        else:
            # A refused core classification must never silently drop work, even
            # if a future core diagnostic schema omits its reject-level entry.
            raise MergeRefused(
                tuple(
                    MergeDiagnostic(
                        code=d.code, message=d.message, related_node_ids=tuple(d.related_node_ids)
                    )
                    for d in result.diagnostics
                )
            )
    staged = context.destination_graph
    if admitted:
        patch = context.captured_authority.patch_template.model_copy(
            update={"ops": admitted, "revision": context.destination_head.revision + 1}
        )
        patch = patch.model_copy(update={"agent_action": derived_agent_action(staged, patch)})
        staged = (
            GraphTransitionManager()
            .prepare_validated(staged, [patch], pre_head=context.destination_head)
            .projection.graph
        )
    groups = _connected_pending_reviews(context, staged, [*reviews, *carried])
    groups = tuple(
        group
        for group in groups
        if any(
            change.collection != "proposals" or change.entity_id not in group.carried_proposal_ids
            for change in group.changes
        )
    )
    absorbed = {identity for group in groups for identity in group.carried_proposal_ids}
    return tuple(
        change
        for change in direct
        if not (change.collection == "proposals" and change.entity_id in absorbed)
    ), groups


def _connected_pending_reviews(
    context: MergePlanningContext, state: GraphState, reviews: list[ReviewCandidate]
) -> tuple[ReviewGroup, ...]:
    """A connected predecessor can name different subjects from the new work.

    Design §6 requires pulling in those subjects so no merge Proposal stales another.
    """
    included = {identity for candidate in reviews for identity in candidate.replaces_proposal_ids}
    remaining = [r for r in pending_reviews(context) if r.proposal.id not in included]
    while True:
        groups = group_reviews(state, reviews)
        for previous in remaining:
            draft = _draft(previous.proposal.ops)
            filing = prepare_patch_bookkeeping(
                context.destination_graph,
                Patch(
                    kind="work",
                    author="human",
                    producer="human",
                    summary="Inspect pending dependencies",
                    created_at=context.captured_authority.patch_template.created_at,
                    revision=context.destination_head.revision,
                    ops=[CreateProposalsOperation(op="create_proposals", proposals=[draft])],
                ),
            ).ops[0]
            assert filing.op == "create_proposals"
            # Core checks the complete admitted transition's writes against the
            # stamped dependency snapshot, including incident sets and causes.
            direct_interference = proposal_is_stale(state, filing.proposals[0])
            footprint = proposal_dependencies_and_effects(context.destination_graph, draft)
            if not direct_interference and not any(
                _interferes(footprint, group.footprint) for group in groups
            ):
                continue
            changes = previous.outstanding
            if changes:
                reviews.append(
                    ReviewCandidate(changes, tuple(previous.proposal.ops), (previous.proposal.id,))
                )
            remaining.remove(previous)
            break
        else:
            return groups


def promote_node_group(plan: MergePlan, promotion: NodeGroupPromotion) -> MergePlan:
    """Open changed node fields as one conflict, retaining protected review slots."""
    plan = MergePlan.model_validate(plan.model_dump(mode="json"))
    node_ids = tuple(sorted(set(promotion.node_ids)))
    selected = set(node_ids)
    if any(
        isinstance(c, NodeGroupConflict) and selected.intersection(c.node_ids)
        for c in plan.conflicts
    ):
        raise MergeRefused(promotion.diagnostics)
    baseline = plan.previous_source_graph or plan.base_graph
    fields: dict[str, FieldConflict] = {}
    for node_id in node_ids:
        old, source, target = (
            g.nodes.get(node_id) for g in (baseline, plan.source_graph, plan.destination_graph)
        )
        if source is None or target is None:
            raise MergeRefused(promotion.diagnostics)
        for name in _fields(old, source, target):
            b, s, d = _field(old, name), _field(source, name), _field(target, name)
            if s != b or d != b:
                change = _change("nodes", node_id, name, b, s, d)
                fields[change.change_id] = _field_conflict(change)
    for review in plan.review_groups:
        for change in review.changes:
            if change.collection == "nodes" and change.entity_id in selected and change.field_name:
                fields[change.change_id] = _field_conflict(change)
    if not fields:
        raise MergeRefused(promotion.diagnostics)
    group = NodeGroupConflict(
        conflict_id=merge_contract_digest(["node_group", node_ids]),
        node_ids=node_ids,
        fields=tuple(sorted(fields.values(), key=lambda f: (f.entity_id or "", f.field_name))),
        base={n: _value(baseline.nodes.get(n)) for n in node_ids},
        branch={n: _value(plan.source_graph.nodes.get(n)) for n in node_ids},
        destination={n: _value(plan.destination_graph.nodes.get(n)) for n in node_ids},
        diagnostic_codes=tuple(sorted({d.code for d in promotion.diagnostics})),
    )
    conflicts = tuple(
        c
        for c in plan.conflicts
        if not (
            isinstance(c, FieldConflict) and c.collection == "nodes" and c.entity_id in selected
        )
    ) + (group,)
    direct = tuple(
        c for c in plan.direct_changes if not (c.collection == "nodes" and c.entity_id in selected)
    )
    predecessors = {review.proposal.id: review for review in pending_reviews(plan)}
    reviews: list[ReviewGroup] = []
    for review in plan.review_groups:
        if any(c.change_id in fields for c in review.changes):
            effects = [c.model_dump(mode="json") for c in review.changes]
            for identity in sorted(review.replaces_proposal_ids):
                if identity in predecessors:
                    effects.extend(
                        operation.model_dump(mode="json")
                        for operation in predecessors[identity].proposal.ops
                    )
            for identity in review.carried_proposal_ids:
                effects.extend(
                    operation.model_dump(mode="json")
                    for operation in plan.source_graph.proposals[identity].ops
                )
            review = review.model_copy(update={"effect_digest": merge_contract_digest(effects)})
        reviews.append(review)
    context = MergePlanningContext.model_validate(
        plan.model_dump(exclude={"plan_id", "direct_changes", "conflicts", "review_groups"})
    )
    return MergePlan(
        **context.model_dump(),
        direct_changes=direct,
        conflicts=conflicts,
        review_groups=tuple(reviews),
    )
