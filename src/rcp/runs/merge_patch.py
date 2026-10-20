"""Deterministic merge construction and the shared, non-publishing preparation path."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import BaseModel, TypeAdapter, ValidationError

from rcp.core.authority import (
    AuthorityRefusal,
    ChangeClassification,
    classify_graph_changes,
    derived_agent_action,
)
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
    ValidationMessage,
    graph_field,
    graph_fields,
)
from rcp.core.operations import (
    GraphOperation,
    NewEdge,
    ProposalOperation,
    ProposalStatusChangeOperation,
    strict_project_node,
)
from rcp.core.project_types import project_type_of
from rcp.core.transitions import (
    PreparedTransition,
    current_project_projection,
    project_transition_projection,
)
from rcp.core.validation import ValidationContext, is_coherence_clash
from rcp.core.validation.ops import edge_shape_refusals
from rcp.core.validation.proposals import (
    ProposalEffectRejected,
    connected_proposal_operation_groups,
    proposal_dependencies_and_effects,
    proposal_is_stale,
)
from rcp.history import PatchRejected, RevisionConflict
from rcp.runs.merge_contract import (
    ConflictAnswer,
    FieldAnswer,
    FieldConflict,
    IncidentEdgeConflict,
    MergeAnswers,
    MergeAnswersRejected,
    MergeChange,
    MergedAnswer,
    MergeDiagnostic,
    MergeHiddenClash,
    MergePlan,
    MergePreparationContract,
    MergeRefused,
    MergeValue,
    MissingValue,
    NodeGroupConflict,
    NodeGroupPromotion,
    PresentValue,
    ReviewGroup,
    merge_contract_digest,
)

if TYPE_CHECKING:
    from rcp.runs.merge_contract import MergeHistory

Coordinate = tuple[str, str | None, str | None]
_OPERATION = TypeAdapter(GraphOperation)


@dataclass
class _Effect:
    operations: list[GraphOperation]
    coordinates: set[Coordinate]
    restore_standing: GraphOperation | None = None


def _diagnostic(code: str, message: str, *nodes: str) -> MergeDiagnostic:
    return MergeDiagnostic(code=code, message=message, related_node_ids=tuple(nodes))


def _refuse(code: str, message: str, *nodes: str) -> NoReturn:
    raise MergeRefused((_diagnostic(code, message, *nodes),))


def _answer_error(conflict_id: str, message: str) -> NoReturn:
    raise MergeAnswersRejected(
        (
            _diagnostic(
                "invalid-merge-answer", f"{conflict_id}: {message}", conflict_id
            ).model_copy(update={"conflict_ids": (conflict_id,)}),
        )
    )


def _reject_answer_effect(plan: MergePlan, diagnostics: tuple[MergeDiagnostic, ...]) -> NoReturn:
    """Construction failures on answered subjects belong to the answer check."""
    nodes = {node for diagnostic in diagnostics for node in diagnostic.related_node_ids}
    conflicts = []
    for conflict in plan.conflicts:
        subjects = (
            set(conflict.node_ids)
            if isinstance(conflict, NodeGroupConflict)
            else {conflict.node_id}
            if isinstance(conflict, IncidentEdgeConflict)
            else {conflict.entity_id}
        )
        if not nodes or nodes & subjects:
            conflicts.append(conflict.conflict_id)
    if not conflicts:
        raise MergeRefused(diagnostics)
    raise MergeAnswersRejected(
        tuple(
            diagnostic.model_copy(
                update={
                    "conflict_ids": tuple(conflicts),
                }
            )
            for diagnostic in diagnostics
        )
    )


def _coordinate(change: MergeChange | FieldConflict) -> Coordinate:
    return change.collection, change.entity_id, change.field_name


def _patch(plan: MergePlan, operations: Iterable[GraphOperation]) -> Patch:
    patch = plan.captured_authority.patch_template.model_copy(update={"ops": list(operations)})
    return patch.model_copy(
        update={"agent_action": derived_agent_action(plan.destination_graph, patch)}
    )


def _check_plan(plan: MergePlan) -> None:
    # Frozen models can still contain mutable graph values; verify the whole binding.
    if merge_contract_digest(plan.model_dump(mode="json", exclude={"plan_id"})) != plan.plan_id:
        _refuse("invalid-merge-plan", "Merge plan content no longer matches its identity")


def _field_value(plan: MergePlan, conflict: FieldConflict, answer: ConflictAnswer) -> MergeValue:
    if answer.kind not in conflict.allowed_answer_kinds:
        _answer_error(conflict.conflict_id, f"answer kind {answer.kind!r} is not allowed")
    value = (
        PresentValue(value=answer.value)
        if isinstance(answer, MergedAnswer)
        else getattr(conflict, answer.kind)
    )
    if isinstance(value, MissingValue):
        _answer_error(conflict.conflict_id, "a model field cannot be absent")
    owner: BaseModel | None = plan.destination_graph
    if conflict.entity_id is not None:
        owner = next(
            (
                entity
                for graph in (plan.destination_graph, plan.source_graph, plan.base_graph)
                if (entity := getattr(graph, conflict.collection).get(conflict.entity_id))
                is not None
            ),
            None,
        )
        if owner is None:
            _answer_error(conflict.conflict_id, "field owner is absent from every snapshot")
    elif conflict.collection == "ontology":
        owner = plan.destination_graph.ontology
    field = type(owner).model_fields.get(conflict.field_name)
    if field is None or graph_field(field).role != "content":
        _answer_error(conflict.conflict_id, "not a content field of the model")
    try:
        # Strict JSON validation preserves JSON enum/datetime forms without numeric coercion.
        TypeAdapter(field.rebuild_annotation()).validate_json(
            json.dumps(value.value, allow_nan=False), strict=True
        )
    except (ValidationError, TypeError, ValueError) as exc:
        _answer_error(conflict.conflict_id, str(exc))
    return value


def _resolved_changes(plan: MergePlan, answers: MergeAnswers) -> dict[Coordinate, MergeValue]:
    expected = {conflict.conflict_id for conflict in plan.conflicts}
    if set(answers.answers) != expected:
        _answer_error("answers", f"expected keys {sorted(expected)}, got {sorted(answers.answers)}")
    unknown = set(answers.cards) - {group.subject_key for group in plan.review_groups}
    if unknown:
        _answer_error(sorted(unknown)[0], f"unknown review subjects {sorted(unknown)}")
    # The planner emits only branch-side changes the destination must receive.
    selected = {
        _coordinate(change): change.branch
        for change in (*plan.direct_changes, *(c for g in plan.review_groups for c in g.changes))
    }
    for conflict in plan.conflicts:
        answer = answers.answers[conflict.conflict_id]
        if (
            not isinstance(conflict, FieldConflict)
            and answer.kind not in conflict.allowed_answer_kinds
        ):
            _answer_error(conflict.conflict_id, f"answer kind {answer.kind!r} is not allowed")
        if isinstance(conflict, FieldConflict):
            selected[_coordinate(conflict)] = _field_value(plan, conflict, answer)
        elif isinstance(conflict, NodeGroupConflict):
            nested = None
            if isinstance(answer, MergedAnswer):
                try:
                    nested = TypeAdapter(dict[str, dict[str, FieldAnswer]]).validate_python(
                        answer.value, strict=True
                    )
                except ValidationError as exc:
                    _answer_error(conflict.conflict_id, str(exc))
                keys = {(node, field) for node, fields in nested.items() for field in fields}
                if keys != {(field.entity_id, field.field_name) for field in conflict.fields}:
                    _answer_error(
                        conflict.conflict_id, "merged group must answer exactly its fields"
                    )
            for field in conflict.fields:
                if field.entity_id is None:
                    _answer_error(conflict.conflict_id, "node group field needs a node identity")
                item = nested[field.entity_id][field.field_name] if nested is not None else answer
                selected[_coordinate(field)] = _field_value(plan, field, item)
        else:
            collection = (
                "nodes" if isinstance(conflict, IncidentEdgeConflict) else conflict.collection
            )
            identity = (
                conflict.node_id
                if isinstance(conflict, IncidentEdgeConflict)
                else conflict.entity_id
            )
            surviving_side = "destination" if conflict.removed_on == "branch" else "branch"
            selected[(collection, identity, None)] = (
                MissingValue()
                if answer.kind == "keep-removal"
                else getattr(conflict, surviving_side)
            )
            # Presence controls all fields of that entity, including deterministic work.
            for key in list(selected):
                if key[:2] == (collection, identity) and key[2] is not None:
                    del selected[key]
            if isinstance(conflict, IncidentEdgeConflict):
                for edge in conflict.incident_edges:
                    if answer.kind == "keep-removal":
                        for key in list(selected):
                            if key[:2] == ("edges", edge.entity_id):
                                del selected[key]
                        selected[("edges", edge.entity_id, None)] = MissingValue()
                    else:
                        selected[_coordinate(edge)] = getattr(edge, surviving_side)
    return selected


def _documents(
    plan: MergePlan, selected: dict[Coordinate, MergeValue]
) -> dict[tuple[str, str | None], tuple[Any, set[Coordinate]]]:
    documents: dict[tuple[str, str | None], tuple[Any, set[Coordinate]]] = {}
    # Presence before fields; an explicit presence conflict has already removed its fields.
    for coordinate in sorted(selected, key=lambda key: (key[0], key[1] or "", key[2] or "")):
        collection, identity, field = coordinate
        value = selected[coordinate]
        key = collection, identity
        if key not in documents:
            current = getattr(plan.destination_graph, collection)
            if identity is not None:
                current = current.get(identity)
            raw = current.model_dump(mode="json") if isinstance(current, BaseModel) else current
            documents[key] = deepcopy(raw), set()
        raw, coordinates = documents[key]
        coordinates.add(coordinate)
        if field is None:
            raw = deepcopy(value.value) if isinstance(value, PresentValue) else None
        else:
            if raw is None or isinstance(value, MissingValue):
                _refuse(
                    "invalid-merge-change",
                    f"Cannot write absent field {coordinate!r}",
                    *([identity] if identity else []),
                )
            raw[field] = deepcopy(value.value)
        documents[key] = raw, coordinates
    return documents


def _status_cause(
    plan: MergePlan,
    identity: str,
    status: Any,
    documents: dict[tuple[str, str | None], tuple[Any, set[Coordinate]]],
) -> dict[str, Any]:
    source = plan.source_graph.nodes.get(identity)
    graph = (
        plan.source_graph
        if source is not None and getattr(source, "status", None) == status
        else plan.destination_graph
    )
    transitions = [
        transition
        for transition in graph.belief_transitions
        if transition.hypothesis_id == identity and transition.to_status == status
    ]
    if transitions:
        transition = max(enumerate(transitions), key=lambda item: (item[1].revision, item[0]))[1]
        if transition.cause.get("kind") == "human_edit":
            proven = graph is plan.source_graph and any(
                fact.revision == transition.revision
                and isinstance(fact.operation, ProposalStatusChangeOperation)
                and any(
                    update.id == identity and update.changes == {"status": status}
                    for update in fact.operation.nodes
                )
                for fact in plan.source_human_facts
            )
            if not proven:
                _refuse(
                    "unproven-human-judgment",
                    "No exact source human fact proves this status",
                    identity,
                )
        if graph is plan.source_graph and transition.cause.get("kind") == "evidence_edge":
            # A later branch edit may relabel or remove the recorded evidence edge while
            # the status stands; the merge carries the evidence it still delivers.
            live = [
                edge_id
                for edge_id in _evidence_edges(graph, identity)
                if _merged_evidence_edge(plan, documents, edge_id, identity)
            ]
            if transition.cause.get("ref_id") not in live and live:
                return {"kind": "evidence_edge", "ref_id": live[-1]}
        return deepcopy(transition.cause)
    _refuse(
        "missing-status-cause", "No recorded transition proves the carried status cause", identity
    )


def _merged_evidence_edge(
    plan: MergePlan,
    documents: dict[tuple[str, str | None], tuple[Any, set[Coordinate]]],
    edge_id: str,
    hypothesis_id: str,
) -> bool:
    """Whether the merged destination holds this edge as evidence for the Hypothesis."""
    document = documents.get(("edges", edge_id))
    if document is not None:
        edge = document[0]
    else:
        current = plan.destination_graph.edges.get(edge_id)
        edge = current.model_dump(mode="json") if current is not None else None
    if edge is None or edge["target"] != hypothesis_id:
        return False
    node = documents.get(("nodes", edge["source"]))
    if node is not None:
        source_type = node[0]["type"] if node[0] is not None else None
    else:
        current_node = plan.destination_graph.nodes.get(edge["source"])
        source_type = current_node.type if current_node is not None else None
    project_type = project_type_of(plan.destination_graph)
    return (
        source_type is not None
        and edge["relation"] in project_type.belief_outcome_relations
        and project_type.is_outcome(source_type)
    )


def _check_answered_edge_shape(
    plan: MergePlan,
    documents: dict[tuple[str, str | None], tuple[Any, set[Coordinate]]],
    identity: str,
    raw: dict[str, Any],
) -> None:
    """A shape either side admitted is carried; one an answer assembled meets today's rules."""
    shape = (raw["source"], raw["relation"], raw["target"])
    for graph in (plan.source_graph, plan.destination_graph):
        admitted = graph.edges.get(identity)
        if admitted is not None and (admitted.source, admitted.relation, admitted.target) == shape:
            return

    def node_type(node_id: str) -> str | None:
        document = documents.get(("nodes", node_id))
        if document is not None:
            return document[0]["type"] if document[0] is not None else None
        node = plan.destination_graph.nodes.get(node_id)
        return node.type if node is not None else None

    refusals = edge_shape_refusals(
        plan.destination_graph.ontology,
        identity,
        raw["relation"],
        raw["source"],
        raw["target"],
        node_type(raw["source"]),
        node_type(raw["target"]),
    )
    if refusals:
        _reject_answer_effect(
            plan,
            tuple(_diagnostic(code, message, identity) for code, message, _ in refusals),
        )


def _evidence_edges(graph: GraphState, hypothesis_id: str) -> list[str]:
    """Edges that can cause this Hypothesis's status, oldest first."""
    project_type = project_type_of(graph)
    return [
        edge.id
        for edge in sorted(graph.edges.values(), key=lambda item: (item.created_rev, item.id))
        if edge.target == hypothesis_id
        and edge.relation in project_type.belief_outcome_relations
        and edge.source in graph.nodes
        and project_type.is_outcome(graph.nodes[edge.source].type)
    ]


def _ops(raw: list[dict[str, Any]]) -> list[GraphOperation]:
    try:
        return [_OPERATION.validate_python(operation) for operation in raw]
    except ValidationError as exc:
        _refuse("malformed-operation", str(exc))


def _retirement_operation(identity: str, edge: dict[str, Any]) -> dict[str, Any]:
    if edge["relation"] == "supersedes":
        return {
            "op": "supersede_nodes",
            "nodes": [
                {
                    "id": identity,
                    "superseded_by": edge["target"],
                    "explanation": edge.get("explanation", ""),
                }
            ],
        }
    return {
        "op": "merge_nodes",
        "merges": [
            {
                "duplicate": identity,
                "canonical": edge["target"],
                "explanation": edge.get("explanation", ""),
            }
        ],
    }


def construct_effects(
    plan: MergePlan,
    selected: dict[Coordinate, MergeValue],
    flags: list[ValidationMessage] | None = None,
) -> list[_Effect]:
    flags = flags if flags is not None else []
    documents = _documents(plan, selected)
    creates: list[_Effect] = []
    edges: list[_Effect] = []
    updates: list[_Effect] = []
    other: list[_Effect] = []
    project_type = project_type_of(plan.destination_graph)
    semantic: dict[str, tuple[dict[str, Any], set[Coordinate]]] = {}
    for (collection, _identity), (raw, coordinates) in documents.items():
        if (
            collection != "edges"
            or raw is None
            or raw.get("relation") not in {"supersedes", "duplicate_of"}
        ):
            continue
        node_key = "nodes", raw["source"]
        node_document = documents.get(node_key)
        if node_document is None or node_document[0] is None:
            continue
        node_raw, node_coordinates = node_document
        lifecycle = project_type.lifecycle_field(node_raw["type"])
        if node_raw.get(lifecycle) == "superseded":
            semantic[raw["source"]] = raw, coordinates | node_coordinates
    semantic_edges = {edge[0]["id"] for edge in semantic.values()}
    for (collection, identity), (raw, coordinates) in documents.items():
        current = getattr(plan.destination_graph, collection)
        current = current.get(identity) if identity is not None else current
        before = current.model_dump(mode="json") if isinstance(current, BaseModel) else current
        if raw == before:
            continue
        operations: list[dict[str, Any]] = []
        if collection == "nodes":
            assert identity is not None
            if raw is None:
                updates.append(
                    _Effect(_ops([{"op": "remove_nodes", "node_ids": [identity]}]), coordinates)
                )
                continue
            if current is None:
                try:
                    node = strict_project_node(raw)
                except ValidationError as exc:
                    _refuse("malformed-operation", str(exc), identity)
                raw = node.model_dump(
                    mode="json",
                    exclude=set(
                        graph_fields(type(node), role="engine")
                        | graph_fields(type(node), node_update="replay_only")
                    ),
                )
                standing = raw.pop("standing", "asserted")
                status = getattr(node, "status", None)
                lifecycle = project_type.lifecycle_field(node.type)
                retired = getattr(node, lifecycle) == "superseded"
                choice = (
                    project_type.is_chooser(node.type)
                    and node.model_dump().get("selected_option") is not None
                )
                if retired:
                    raw[lifecycle] = type(node).model_fields[lifecycle].get_default()
                    semantic.setdefault(
                        identity, ({"relation": "supersedes", "target": None}, coordinates)
                    )
                belief = project_type.is_belief(node.type)
                chosen = None
                if choice:
                    chosen = raw.pop("selected_option")
                    raw["status"] = "open"
                if belief:
                    raw["status"] = "proposed"
                operations.append({"op": "create_nodes", "nodes": [raw]})
                if choice:
                    operations.append(
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {
                                    "id": identity,
                                    "changes": {"status": "decided", "selected_option": chosen},
                                }
                            ],
                        }
                    )
                if choice and not retired and status != "decided":
                    operations.append(
                        {
                            "op": "update_nodes",
                            "nodes": [{"id": identity, "changes": {"status": status}}],
                        }
                    )
                if not belief and not retired and standing != "asserted":
                    operations.append(
                        {"op": "set_standing", "node_id": identity, "standing": standing}
                    )
                creates.append(_Effect(_ops(operations), coordinates))
                review: list[dict[str, Any]] = []
                if retired:
                    edge, combined = semantic[identity]
                    coordinates = coordinates | combined
                    review.append(_retirement_operation(identity, edge))
                elif belief and status != "proposed":
                    review.append(
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {
                                    "id": identity,
                                    "changes": {"status": status},
                                    "cause": _status_cause(plan, identity, status, documents),
                                }
                            ],
                        }
                    )
                if (belief or retired) and standing != "asserted":
                    review.append({"op": "set_standing", "node_id": identity, "standing": standing})
                if review:
                    updates.append(_Effect(_ops(review), coordinates))
                continue
            current = plan.destination_graph.nodes[identity]
            fields = graph_fields(type(current), role="content")
            changes = {
                field: raw[field]
                for field in sorted(fields - {"standing"})
                if raw[field] != before[field]
            }
            lifecycle = project_type.lifecycle_field(current.type)
            if changes.get(lifecycle) == "superseded" and identity not in semantic:
                semantic[identity] = (
                    {"relation": "supersedes", "target": None, "explanation": ""},
                    coordinates,
                )
            if identity in semantic:
                edge, combined = semantic[identity]
                changes.pop(project_type.lifecycle_field(current.type), None)
                coordinates = coordinates | combined
                operations.append(_retirement_operation(identity, edge))
            if changes:
                update: dict[str, Any] = {"id": identity, "changes": changes}
                if project_type.is_belief(current.type) and "status" in changes:
                    update["cause"] = _status_cause(plan, identity, changes["status"], documents)
                operations.append({"op": "update_nodes", "nodes": [update]})
            standing = raw["standing"]
            restoration = None
            if standing != before["standing"]:
                operations.append({"op": "set_standing", "node_id": identity, "standing": standing})
            elif operations and standing != "asserted":
                # Only an admitted write resets standing. Pending review must not
                # acquire an unrelated standing request merely because of node type.
                restoration = _ops(
                    [{"op": "set_standing", "node_id": identity, "standing": standing}]
                )[0]
            if operations:
                updates.append(_Effect(_ops(operations), coordinates, restoration))
        elif collection == "edges":
            assert identity is not None
            if identity in semantic_edges:
                continue
            if current is not None:
                operations.append({"op": "remove_edges", "edge_ids": [identity]})
            if raw is not None:
                raw = {key: value for key, value in raw.items() if key in NewEdge.model_fields}
                _check_answered_edge_shape(plan, documents, identity, raw)
                operations.append({"op": "create_edges", "edges": [raw]})
            if operations:
                edges.append(_Effect(_ops(operations), coordinates))
        elif collection == "proposals":
            assert identity is not None
            # Source resolution is source history, never authority on the destination.
            source = plan.source_graph.proposals.get(identity)
            if source is not None and source.status == "pending" and current is None:
                if proposal_is_stale(plan.source_graph, source):
                    flags.append(
                        ValidationMessage(
                            level="flag",
                            code="stale-source-proposal",
                            message=f"Stale source Proposal {identity} was not carried",
                            related_node_ids=[identity],
                        )
                    )
                    continue
                operations = [
                    {
                        "op": "create_proposals",
                        "proposals": [
                            source.model_dump(
                                mode="json",
                                exclude=set(graph_fields(Proposal, proposal_owner="filing")),
                            )
                        ],
                    }
                ]
        elif collection == "glossary":
            if raw is None:
                _refuse("unsupported-merge-effect", "Glossary removal has no graph operation")
            operations = [
                {
                    "op": "upsert_glossary",
                    "terms": [
                        {key: raw[key] for key in ("term", "plain_definition", "where_defined")}
                    ],
                }
            ]
        elif collection == "ambiguities":
            if current is None and raw is not None:
                operations = [
                    {
                        "op": "create_ambiguities",
                        "ambiguities": [
                            {key: value for key, value in raw.items() if key != "raised_rev"}
                        ],
                    }
                ]
            elif raw is not None and set(key for key in raw if raw[key] != before[key]) <= {
                "status"
            }:
                operations = [
                    {
                        "op": "resolve_ambiguities",
                        "resolutions": [{"id": identity, "status": raw["status"]}],
                    }
                ]
            else:
                _refuse("unsupported-merge-effect", "Ambiguity edit has no graph operation")
        elif collection == "ontology":
            operations = [{"op": "set_ontology", "ontology": raw}]
        elif collection == "project_truth_scope":
            operations = [{"op": "set_project_truth_scope", "truth_scope": raw}]
        if operations and collection not in {"nodes", "edges"}:
            other.append(_Effect(_ops(operations), coordinates))
    return creates + edges + updates + other


def _classify(
    plan: MergePlan, groups: list[list[GraphOperation]]
) -> tuple[ChangeClassification, ...]:
    """Classify an ordered partition once, keeping every effect's boundary."""
    state = plan.destination_graph
    operations = [operation for group in groups for operation in group]
    candidate = prepare_patch_bookkeeping(state, _patch(plan, operations))
    indices = []
    offset = 0
    for group in groups:
        indices.append(tuple(range(offset, offset + len(group))))
        offset += len(group)
    return classify_graph_changes(
        state,
        candidate,
        authority=plan.captured_authority,
        project_id=plan.captured_authority.project_id,
        apply_target=plan.destination_head.target,
        context=ValidationContext(project_truth_scope=state.project_truth_scope),
        operation_groups=tuple(indices),
    )


def _require_admitted(plan: MergePlan, result: ChangeClassification) -> None:
    if result.disposition == "clash":
        _reject_preparation(plan, result.diagnostics)
    if result.disposition != "admitted":
        raise MergeRefused(_diagnostics(result.diagnostics))


def _staged_graph(plan: MergePlan, operations: list[GraphOperation]) -> GraphState:
    patch = _patch(plan, operations).model_copy(
        update={"revision": plan.destination_head.revision + 1}
    )
    return apply_valid_patch(
        plan.destination_graph, prepare_patch_bookkeeping(plan.destination_graph, patch)
    )


def _diagnostics(messages: Iterable[ValidationMessage]) -> tuple[MergeDiagnostic, ...]:
    return tuple(
        MergeDiagnostic(
            code=item.code, message=item.message, related_node_ids=tuple(item.related_node_ids)
        )
        for item in messages
        if item.level == "reject"
    )


def _combine_review_ops(operations: Iterable[ProposalOperation]) -> list[ProposalOperation]:
    # Earlier pending effects survive, while new writes to the same field take precedence.
    result: list[ProposalOperation] = []
    for operation in operations:
        if operation in result:
            continue
        if operation.op == "update_nodes":
            for index, prior in enumerate(result):
                if (
                    prior.op == "update_nodes"
                    and prior.intent == operation.intent
                    and len(prior.nodes) == len(operation.nodes) == 1
                    and prior.nodes[0].id == operation.nodes[0].id
                ):
                    update = operation.nodes[0].model_copy(
                        update={"changes": {**prior.nodes[0].changes, **operation.nodes[0].changes}}
                    )
                    result[index] = operation.model_copy(update={"nodes": [update]})
                    break
            else:
                result.append(operation)
        elif operation.op == "set_standing":
            result = [
                item
                for item in result
                if not (item.op == "set_standing" and item.node_id == operation.node_id)
            ]
            result.append(operation)
        else:
            result.append(operation)
    return result


def _review_proposal(
    plan: MergePlan, group: ReviewGroup, operations: list[ProposalOperation], answers: MergeAnswers
) -> Proposal:
    if group.subject_key not in answers.cards:
        _answer_error(group.subject_key, "missing review card")
    identity = merge_contract_digest(
        {
            "source": plan.source_head.model_dump(mode="json"),
            "destination": plan.destination_head.target.model_dump(mode="json"),
            "subject": group.subject_key,
            "ops": [op.model_dump(mode="json", exclude_unset=True) for op in operations],
        }
    )
    proposal = Proposal(
        id=f"proposal/merge-{identity}",
        title="Review branch changes",
        card=GatedCard.model_validate(answers.cards[group.subject_key].model_dump()),
        ops=operations,
        dependency_rule=proposal_filing_dependency_rule(len(operations), branch_merge=True),
    )
    filing = prepare_patch_bookkeeping(
        plan.destination_graph,
        _patch(plan, _ops([{"op": "create_proposals", "proposals": [proposal]}])),
    ).ops[0]
    assert filing.op == "create_proposals"
    return filing.proposals[0]


def build_merge_patch(plan: MergePlan, answers: MergeAnswers) -> Patch:
    """Resolve typed answers, encode effects, and route every effect through core."""
    _check_plan(plan)
    selected = _resolved_changes(plan, answers)
    flags: list[ValidationMessage] = []
    absorbed = {identity for group in plan.review_groups for identity in group.carried_proposal_ids}
    try:
        effects = construct_effects(
            plan,
            {
                coordinate: value
                for coordinate, value in selected.items()
                if not (coordinate[0] == "proposals" and coordinate[1] in absorbed)
            },
            flags,
        )
    except MergeRefused as exc:
        _reject_answer_effect(plan, exc.diagnostics)
    group_coordinates = {
        group.subject_key: {_coordinate(change) for change in group.changes}
        for group in plan.review_groups
    }
    direct: list[GraphOperation] = []
    reviews: dict[str, list[ProposalOperation]] = defaultdict(list)
    # Keep coherent groups explicit. Core stages admitted prerequisites once;
    # pending review effects never become part of the next group's prefix.
    groups: list[tuple[list[GraphOperation], str | None, list[GraphOperation]]] = []
    for effect in effects:
        keys = [key for key, coords in group_coordinates.items() if coords & effect.coordinates]
        if len(keys) > 1:
            _reject_answer_effect(
                plan,
                (
                    _diagnostic(
                        "merge-review-groups-changed",
                        "A coherent effect spans multiple review groups",
                    ),
                ),
            )
        groups.append(
            (
                effect.operations,
                keys[0] if keys else None,
                [effect.restore_standing] if effect.restore_standing is not None else [],
            )
        )
    restorations: list[GraphOperation] = []
    for (operations, key, restore), result in zip(
        groups, _classify(plan, [group[0] for group in groups]), strict=True
    ):
        if result.disposition == "reviewable":
            if key is None:
                _refuse(
                    "merge-review-group-missing", "Reviewable effect has no planned review subject"
                )
            reviews[key].extend(result.review_operations)
        else:
            _require_admitted(plan, result)
            direct.extend(operations)
            restorations.extend(restore)
    if restorations:
        for result in _classify(plan, [direct, restorations]):
            _require_admitted(plan, result)
        direct.extend(restorations)
    staged = _staged_graph(plan, direct)
    proposals: list[Proposal] = []
    withdrawals: list[dict[str, str]] = []
    for group in sorted(plan.review_groups, key=lambda item: item.subject_key):
        operations = reviews.get(group.subject_key, [])
        for identity in sorted(group.replaces_proposal_ids):
            prior = next(
                (review for review in plan.prior_reviews if review.proposal.id == identity),
                None,
            )
            live = plan.destination_graph.proposals.get(identity)
            if prior is None or live is None or live.status != "pending" or live != prior.proposal:
                _refuse(
                    "invalid-merge-review-replacement",
                    f"No scoped live pending review {identity!r}",
                )
            withdrawals.append(
                {"id": identity, "reason": "Replaced by a newer branch merge review."}
            )
        combined = _combine_review_ops(
            [
                *operations,
                *(
                    operation
                    for identity in group.carried_proposal_ids
                    for operation in plan.source_graph.proposals[identity].ops
                ),
            ]
        )
        if not combined:
            continue
        proposal = _review_proposal(plan, group, combined, answers)
        # Answers can retain a destination value that connected two planned
        # effects. File the remaining connected components with the same card;
        # each component still passes the ordinary filing and approval gates.
        components = connected_proposal_operation_groups(staged, proposal)
        proposals.extend(
            _review_proposal(plan, group, list(component), answers) for component in components
        )
    # Validate all filings with one explicit prefix, then calculate footprints
    # against one staged graph. Carried source cards participate in the same
    # no-mutual-invalidation guarantee as newly constructed merge reviews.
    filing_groups = [
        _ops([{"op": "create_proposals", "proposals": [proposal]}]) for proposal in proposals
    ]
    if filing_groups:
        for result in _classify(plan, ([direct] if direct else []) + filing_groups):
            _require_admitted(plan, result)
    carried = [
        staged.proposals[proposal.id]
        for operation in direct
        if operation.op == "create_proposals"
        for proposal in operation.proposals
    ]
    footprints = []
    for proposal in [*carried, *proposals]:
        try:
            footprint = proposal_dependencies_and_effects(staged, proposal)
        except ProposalEffectRejected as exc:
            _reject_answer_effect(plan, _diagnostics(exc.diagnostics))
        for previous in footprints:
            if (
                footprint.effects & previous.dependencies
                or previous.effects & footprint.dependencies
            ):
                _reject_answer_effect(
                    plan,
                    (
                        _diagnostic(
                            "merge-review-groups-changed",
                            "Answered review groups now affect one another; revise the answers",
                        ),
                    ),
                )
        footprints.append(footprint)
    final = list(direct)
    if withdrawals:
        final.extend(_ops([{"op": "withdraw_proposals", "proposals": withdrawals}]))
    if proposals:
        final.extend(_ops([{"op": "create_proposals", "proposals": proposals}]))
    candidate = prepare_patch_bookkeeping(plan.destination_graph, _patch(plan, final))
    if candidate.ops:
        for result in _classify(plan, [candidate.ops]):
            _require_admitted(plan, result)
    return candidate.model_copy(update={"admission_messages": flags})


def _reject_preparation(plan: MergePlan, messages: Iterable[ValidationMessage]) -> NoReturn:
    messages = tuple(messages)
    diagnostics = _diagnostics(messages)
    if not is_coherence_clash(messages):
        raise MergeRefused(diagnostics)
    nodes = {node for item in diagnostics for node in item.related_node_ids}
    if not nodes:
        raise MergeRefused(diagnostics)
    open_nodes: set[str] = set()
    for conflict in plan.conflicts:
        if isinstance(conflict, NodeGroupConflict):
            open_nodes.update(conflict.node_ids)
    if nodes & open_nodes:
        _reject_answer_effect(plan, diagnostics)
    raise MergeHiddenClash(
        NodeGroupPromotion(node_ids=tuple(sorted(nodes)), diagnostics=diagnostics)
    )


def prepare_merge_candidate(
    plan: MergePlan, answers: MergeAnswers, history: MergeHistory
) -> PreparedTransition:
    """The single dry preparation for check and commit; never appends history."""
    _check_plan(plan)
    if plan.preparation_contract != MergePreparationContract.current():
        _refuse(
            "merge-preparation-contract-changed", "Merge preparation rules changed; reload the plan"
        )
    if history.head_ref() != plan.destination_head:
        raise RevisionConflict("Merge destination moved; rebuild the plan")
    candidate = build_merge_patch(plan, answers)
    try:
        prepared, report, current = history.validate_candidate(candidate)
    except PatchRejected as exc:
        _reject_preparation(plan, exc.report.messages)
    except AuthorityRefusal as exc:
        raise MergeRefused(_diagnostics(exc.diagnostics)) from exc
    if current.revision != plan.destination_head.revision:
        raise RevisionConflict("Merge destination moved during preparation; rebuild the plan")
    if report.rejected:
        _reject_preparation(plan, report.messages)
    if prepared.branch_merge != plan.provenance or prepared.authorized_by != plan.authorized_by:
        _refuse(
            "merge-preparation-binding-changed",
            "History changed the merge's provenance or authorizer",
        )
    messages = list(prepared.admission_messages)
    for diagnostic in [*report.messages, *candidate.admission_messages]:
        if diagnostic not in messages:
            messages.append(diagnostic)
    prepared = prepared.model_copy(update={"admission_messages": messages})
    if not prepared.ops:
        if history.head_ref() != plan.destination_head:
            raise RevisionConflict("Merge destination moved during no-change preparation")
        return PreparedTransition(
            patch=prepared,
            projection=current_project_projection(
                current,
                target=plan.destination_head.target,
                transition_id=plan.destination_head.transition_id,
            ).model_copy(update={"canonical": False, "base_head": plan.destination_head}),
        )
    if prepared.transition is None:
        _refuse("missing-merge-transition", "History did not prepare a transition")
    trace = prepared.transition
    if trace.pre_head != plan.destination_head:
        raise RevisionConflict("Merge destination head differs from the prepared transition")
    graph = apply_valid_patch(current, prepared)
    return PreparedTransition(
        patch=prepared,
        projection=project_transition_projection(graph, trace, canonical=False),
    )
