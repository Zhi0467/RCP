from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, ValidationError, field_serializer

from rcp.core.authority import (
    CONTENT_CHANGE_INTENT,
    HYPOTHESIS_PROPOSAL_FIELDS,
    MERGE_INTENT,
    PROPOSAL_INTENTS,
    PROTECTED_EPISTEMIC_RELATIONS,
    PROTECTED_RELATION_CHANGE_INTENT,
    REMOVAL_INTENT,
    STANDING_CHANGE_INTENT,
    STATUS_CHANGE_INTENT,
    SUPERSEDE_INTENT,
    Writer,
    created_edge_ids,
    created_node_ids,
)
from rcp.core.models import (
    Decision,
    GraphState,
    Hypothesis,
    Patch,
    Proposal,
    ResearchQuestion,
    ValidationMessage,
)
from rcp.core.operations import (
    CreateNodesOperation,
    CreateProposalsOperation,
    DecisionCause,
    EvidenceEdgeCause,
    GraphOperation,
    HumanEditCause,
    LegacyProposalMergeOperation,
    LegacyProposalProtectedRelationOperation,
    LegacyProposalRemovalOperation,
    LegacyProposalSupersedeOperation,
    NodeUpdate,
    ProposalContentChangeOperation,
    ProposalMergeOperation,
    ProposalOperation,
    ProposalProtectedRelationOperation,
    ProposalRemovalOperation,
    ProposalResolution,
    ProposalStandingChangeOperation,
    ProposalStatusChangeOperation,
    ProposalSupersedeOperation,
    ResolveProposalsOperation,
    SetStandingOperation,
    UpdateNodesOperation,
    graph_operations_from_proposal,
)
from rcp.core.project_types import project_type_of
from rcp.core.validation.constants import IDENTIFIER_RE
from rcp.core.validation.nodes import validate_source_refs
from rcp.core.validation.report import ValidationReport

PROPOSAL_REPLAY_RULE_ID = "proposal-replay"


class ProposalEffectRejected(ValueError):
    """A proposed approval has structured graph-legality failures."""

    def __init__(self, diagnostics: Iterable[ValidationMessage]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(item.message for item in self.diagnostics))


def _protected_names(state: GraphState) -> str:
    """The protected belief types as rule messages name them."""

    project_type = project_type_of(state)
    return project_type.label_list(project_type.protected_belief_types)


def decision_transition_error(decision: Decision, changes: dict[str, Any]) -> str | None:
    """Return the authority-coherence error for a proposed Decision transition."""

    selected_option = changes.get("selected_option", decision.selected_option)
    status = changes.get("status", decision.status)
    options = changes.get("options", decision.options)
    if (
        changes.get("selected_option") is not None
        and isinstance(options, list)
        and changes["selected_option"] not in options
    ):
        return f"Decision {decision.id} can select only an option listed in its resulting options."
    if status == "decided" and (
        selected_option is None or not isinstance(options, list) or selected_option not in options
    ):
        return (
            f"Decision {decision.id} can be decided only with a selected option listed in its "
            "resulting options."
        )
    if status == "revisit" and decision.status != "revisit" and decision.selected_option is None:
        return f"Decision {decision.id} can be revisited only after it has a prior decision."
    if (
        decision.status in {"open", "ready", "revisit"}
        and changes.get("selected_option") is not None
        and status != "decided"
    ):
        return f"Decision {decision.id} must become decided when an option is selected."
    return None


def normalized_decision_proposal_ops(state: GraphState, proposal: Proposal) -> list[GraphOperation]:
    """Add the one implied field accepted for legacy Decision Proposal approval."""

    operations = graph_operations_from_proposal(proposal.ops)
    normalized: list[GraphOperation] = []
    for operation in operations:
        if not isinstance(operation, UpdateNodesOperation):
            normalized.append(operation)
            continue
        updates: list[NodeUpdate] = []
        for update in operation.nodes:
            node = state.nodes.get(update.id)
            changes = dict(update.changes)
            if (
                isinstance(node, Decision)
                and changes.get("selected_option") is not None
                and "status" not in changes
                and node.status != "decided"
            ):
                changes["status"] = "decided"
                update = update.model_copy(update={"changes": changes})
            updates.append(update)
        normalized.append(operation.model_copy(update={"nodes": updates}))
    return normalized


def proposal_updates_node(proposal: Proposal, node_id: str) -> bool:
    """Whether any semantic node update in a Proposal targets ``node_id``."""

    return any(
        update.id == node_id
        for operation in proposal.ops
        if isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation))
        for update in operation.nodes
    )


class ProposalResource(BaseModel, frozen=True):
    """An identity used to intersect approval writes with another review's reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["node", "edge", "config", "incident_edges"]
    id: str


class ProposalIncidentEdges(BaseModel, frozen=True):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_ids: frozenset[str]
    edge_ids: frozenset[str]

    @field_serializer("node_ids", "edge_ids", when_used="json")
    def serialize_ids(self, values: frozenset[str]) -> list[str]:
        return sorted(values)


class ProposalDependenciesAndEffects(BaseModel, frozen=True):
    """Read dependencies and actual approval writes; shared reads do not couple reviews.

    Required absence contributes an ordinary edge dependency identity as well as
    its explicit absence condition. Incident-set identities are per node, so a
    newly created edge intersects a removal's dependency even without a prior ID.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dependencies: frozenset[ProposalResource]
    effects: frozenset[ProposalResource]
    required_absent_edge_ids: frozenset[str]
    incident_snapshots: tuple[ProposalIncidentEdges, ...]

    @field_serializer("dependencies", "effects", when_used="json")
    def serialize_resources(self, values: frozenset[ProposalResource]) -> list[dict[str, str]]:
        return [
            value.model_dump() for value in sorted(values, key=lambda item: (item.kind, item.id))
        ]

    @field_serializer("required_absent_edge_ids", when_used="json")
    def serialize_absence(self, values: frozenset[str]) -> list[str]:
        return sorted(values)


def _proposal_approval_standing_targets(proposal: Proposal) -> list[str]:
    """Keep historical acceptance semantics; rule 3 accepts only written subjects."""

    explicit = {
        operation.node_id
        for operation in proposal.ops
        if isinstance(operation, ProposalStandingChangeOperation)
    }
    if proposal.dependency_rule < 3:
        if explicit:
            return []
        if len(proposal.ops) != 1 or proposal.ops[0].intent.startswith("legacy_"):
            return list(proposal.related_node_ids)
        operation = proposal.ops[0]
        if isinstance(operation, (ProposalRemovalOperation, ProposalProtectedRelationOperation)):
            return []
        if isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
            return [operation.nodes[0].id]
        if isinstance(operation, ProposalSupersedeOperation):
            return [operation.nodes[0].id]
        if isinstance(operation, ProposalMergeOperation):
            return [operation.merges[0].duplicate]
        return list(proposal.related_node_ids)
    targets: list[str] = []
    for operation in proposal.ops:
        if operation.intent.startswith("legacy_"):
            targets.extend(proposal.related_node_ids)
        elif isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
            targets.extend(update.id for update in operation.nodes)
        elif isinstance(operation, ProposalSupersedeOperation):
            targets.extend(item.id for item in operation.nodes)
        elif isinstance(operation, ProposalMergeOperation):
            targets.extend(item.duplicate for item in operation.merges)
    removed = _proposal_removal_targets(proposal)
    return list(dict.fromkeys(target for target in targets if target not in explicit | removed))


def proposal_approval_operations(
    state: GraphState, proposal: Proposal, *, reason: str | None = None
) -> list[GraphOperation]:
    """Construct approval's exact ordered effects, including resolution and standing.

    The caller checks staleness and human authority. This pure construction is
    shared by Sync and dependency/effect analysis; it does not approve anything.
    Legacy Decision normalization and dependency-rule standing behavior survive.
    """

    return [
        *normalized_decision_proposal_ops(state, proposal),
        ResolveProposalsOperation(
            op="resolve_proposals",
            resolutions=[ProposalResolution(id=proposal.id, status="approved", reason=reason)],
        ),
        *[
            SetStandingOperation(op="set_standing", node_id=node_id, standing="accepted")
            for node_id in _proposal_approval_standing_targets(proposal)
        ],
    ]


class _ProposalDependencyInputs(NamedTuple):
    node_ids: tuple[str, ...]
    edge_ids: frozenset[str]
    config_keys: tuple[str, ...]
    required_absent_edge_ids: frozenset[str]
    incident_snapshots: tuple[ProposalIncidentEdges, ...]
    decision_ids: frozenset[str]
    cause_evidence_sources: dict[str, str]


def _proposal_dependency_contract(
    state: GraphState, proposal: Proposal, *, derive_unfiled: bool = False
) -> _ProposalDependencyInputs:
    """One dependency derivation for staleness and approval interference analysis."""

    # Drafts have no filing snapshot yet. Filed records retain exactly the
    # dependency lists their writer recorded, including rule-1/2 behavior.
    if derive_unfiled and not proposal.raised_rev:
        from rcp.core.validation.registry import proposal_dependencies

        nodes, edges, config = proposal_dependencies(state, proposal.ops)
    else:
        nodes, edges, config = (
            proposal.related_node_ids,
            proposal.related_edge_ids,
            proposal.related_config_keys,
        )
    referenced_edges, decision_ids = _proposal_reference_dependencies(proposal)
    edge_ids = set(edges) | referenced_edges
    absent = _proposal_created_edge_ids(proposal)
    if proposal.dependency_rule >= 3:
        absent -= {
            edge_id
            for operation in proposal.ops
            if isinstance(operation, ProposalProtectedRelationOperation)
            and operation.op == "remove_edges"
            for edge_id in operation.edge_ids or []
        }
    targets = _proposal_removal_targets(proposal)
    snapshots: tuple[ProposalIncidentEdges, ...] = ()
    if targets:
        expected = set(edges)
        if proposal.dependency_rule >= 3:
            # Missing/recreated snapshotted IDs are rejected by the edge check
            # below, so current endpoint filtering cannot hide an incident move.
            expected = {
                edge_id
                for edge_id in expected
                if edge_id in state.edges
                and (
                    state.edges[edge_id].source in targets or state.edges[edge_id].target in targets
                )
            }
        snapshots = (
            ProposalIncidentEdges(node_ids=frozenset(targets), edge_ids=frozenset(expected)),
        )
    return _ProposalDependencyInputs(
        tuple(nodes),
        frozenset(edge_ids),
        tuple(config),
        frozenset(absent),
        snapshots,
        frozenset(decision_ids),
        proposal_cause_evidence_sources(state, proposal),
    )


def proposal_dependencies_and_effects(
    state: GraphState, proposal: Proposal
) -> ProposalDependenciesAndEffects:
    """Describe a valid Proposal's reads and complete dry approval writes.

    Pass the pre-approval state and either a filed Proposal or an unfiled draft.
    The draft's dependency_rule must match the filing rule selected for its form.
    Approval operations pass graph validation before the existing transition
    engine derives implicit standing, lifecycle edges, incident-edge deletion
    and guidance invalidation. Invalid effects raise ProposalEffectRejected
    with structured diagnostics. This does not perform live authority admission.
    Neither input nor canonical history is changed. An effect/dependency
    intersection in either direction couples two reviews; shared reads do not.
    """

    try:
        return _proposal_effect_footprint(state, proposal, validate=True)
    except ProposalEffectRejected:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise ProposalEffectRejected(
            (
                ValidationMessage(
                    level="reject",
                    code="malformed-operation",
                    message=str(exc),
                    patch_revision=state.revision + 1,
                ),
            )
        ) from exc


def _proposal_effect_footprint(
    state: GraphState, proposal: Proposal, *, validate: bool
) -> ProposalDependenciesAndEffects:
    """Project effects; member projections are speculative until the whole group validates.

    Edge replacement's create member is not legal alone. The group boundary may
    inspect it without validation to find its dependency intersection, then must
    validate the complete ordered group before admission.
    """
    from rcp.core.transitions import GraphTransitionManager
    from rcp.core.validation.approval import is_decision_choice
    from rcp.core.validation.patch import validate_patch
    from rcp.core.validation.settings import ValidationContext

    contract = _proposal_dependency_contract(state, proposal, derive_unfiled=True)
    nodes = (
        set(contract.node_ids)
        | contract.decision_ids
        | set(contract.cause_evidence_sources.values())
    )
    dependencies = {
        *(ProposalResource(kind="node", id=value) for value in nodes),
        *(
            ProposalResource(kind="edge", id=value)
            for value in contract.edge_ids | contract.required_absent_edge_ids
        ),
        *(ProposalResource(kind="config", id=value) for value in contract.config_keys),
        *(
            ProposalResource(kind="incident_edges", id=value)
            for snapshot in contract.incident_snapshots
            for value in snapshot.node_ids
        ),
    }
    if not proposal.raised_rev:
        proposal = proposal.model_copy(
            update={
                "base_rev": state.revision,
                "raised_rev": state.revision,
                "related_node_ids": list(contract.node_ids),
                "related_edge_ids": sorted(contract.edge_ids),
                "related_config_keys": list(contract.config_keys),
            }
        )
    before = state.model_copy(update={"proposals": {**state.proposals, proposal.id: proposal}})
    approval_ops = proposal_approval_operations(state, proposal)
    chooses_decision = any(
        isinstance(state.nodes.get(update.id), Decision) and is_decision_choice(update.changes)
        for operation in approval_ops
        if isinstance(operation, UpdateNodesOperation)
        for update in operation.nodes
    )
    patch = Patch(
        revision=state.revision + 1,
        kind="approval",
        author="human",
        producer="human",
        summary=f"Inspect approval effects of {proposal.id}.",
        ops=approval_ops,
        human_action="decision_choice" if chooses_decision else None,
    )
    if validate:
        report = validate_patch(
            before,
            patch,
            ValidationContext(project_truth_scope=state.project_truth_scope),
        )
        if report.rejected:
            raise ProposalEffectRejected(item for item in report.messages if item.level == "reject")
    after = GraphTransitionManager().prepare_validated(before, [patch]).projection.graph
    effects: set[ProposalResource] = set()
    for node_id in before.nodes.keys() | after.nodes.keys():
        if before.nodes.get(node_id) != after.nodes.get(node_id):
            effects.add(ProposalResource(kind="node", id=node_id))
    for edge_id in before.edges.keys() | after.edges.keys():
        if before.edges.get(edge_id) != after.edges.get(edge_id):
            effects.add(ProposalResource(kind="edge", id=edge_id))
            for graph in (before, after):
                if edge := graph.edges.get(edge_id):
                    effects.update(
                        ProposalResource(kind="incident_edges", id=node_id)
                        for node_id in (edge.source, edge.target)
                    )
    for key in before.config_revisions.keys() | after.config_revisions.keys():
        if before.config_revisions.get(key) != after.config_revisions.get(key):
            effects.add(ProposalResource(kind="config", id=key))
    return ProposalDependenciesAndEffects(
        dependencies=frozenset(dependencies),
        effects=frozenset(effects),
        required_absent_edge_ids=contract.required_absent_edge_ids,
        incident_snapshots=contract.incident_snapshots,
    )


def proposal_is_stale(state: GraphState, proposal: Proposal) -> bool:
    """Whether the state a pending Proposal depends on has moved or disappeared."""

    return bool(moved_proposal_dependencies(state, proposal))


def moved_proposal_dependencies(state: GraphState, proposal: Proposal) -> list[str]:
    """The node, edge, and config ids whose move makes a pending Proposal stale."""

    dependency_revision = proposal.raised_rev or proposal.base_rev
    contract = _proposal_dependency_contract(state, proposal)
    moved = [
        node_id
        for node_id in contract.node_ids
        if node_id not in state.nodes or state.nodes[node_id].updated_rev > dependency_revision
    ]
    moved.extend(
        key
        for key in contract.config_keys
        if state.config_revisions.get(key, 0) > dependency_revision
    )
    moved.extend(
        edge_id
        for edge_id in sorted(contract.edge_ids)
        if edge_id not in state.edges or state.edges[edge_id].created_rev > dependency_revision
    )
    moved.extend(
        edge_id for edge_id in sorted(contract.required_absent_edge_ids) if edge_id in state.edges
    )
    for snapshot in contract.incident_snapshots:
        current = {
            edge.id
            for edge in state.edges.values()
            if edge.source in snapshot.node_ids or edge.target in snapshot.node_ids
        }
        moved.extend(sorted(current ^ snapshot.edge_ids))
    for edge_id, source_id in sorted(contract.cause_evidence_sources.items()):
        if state.edges[edge_id].created_rev <= dependency_revision:
            source = state.nodes.get(source_id)
            if source is None or source.updated_rev > dependency_revision:
                moved.append(source_id)
    moved.extend(
        decision_id
        for decision_id in sorted(contract.decision_ids)
        if not isinstance(state.nodes.get(decision_id), Decision)
        or state.nodes[decision_id].updated_rev > dependency_revision
    )
    return list(dict.fromkeys(moved))


def proposal_work_cannot_refile(state: GraphState, proposal: Proposal) -> bool:
    """Whether ordinary Work cannot re-file this Proposal's form.

    Only replay files a legacy intent, and only a canonical branch merge files a
    multi-intent bundle, a standing change, a status change caused by a human
    edit, or the removal of an accepted unprotected node, so a stale one is
    withdrawn rather than refreshed.
    """

    if len(proposal.ops) > 1 or any(op.intent not in PROPOSAL_INTENTS for op in proposal.ops):
        return True
    for operation in proposal.ops:
        if isinstance(operation, ProposalStandingChangeOperation):
            return True
        if isinstance(
            operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation)
        ) and any(isinstance(update.cause, HumanEditCause) for update in operation.nodes):
            return True
        if isinstance(operation, ProposalRemovalOperation) and any(
            _existing_protected_node(state, None, node_id) is None for node_id in operation.node_ids
        ):
            return True
    return False


def proposal_refresh_refusal(state: GraphState, proposal: Proposal | None) -> str | None:
    """Why a light agent may not refresh this Proposal, or None when it may.

    Refresh is offered only where the Sync gate would withdraw the Proposal and
    ordinary Work can file it again against a node that still exists.
    """

    if proposal is None or proposal.status != "pending":
        return "proposal_not_pending"
    if not proposal_is_stale(state, proposal):
        return "proposal_not_stale"
    if proposal_work_cannot_refile(state, proposal):
        return "proposal_not_refileable"
    if proposal_subject_node(state, proposal) is None:
        return "proposal_target_unavailable"
    return None


def proposal_subject_node(state: GraphState, proposal: Proposal) -> str | None:
    """The Proposal's first current subject node, else its first current related node."""

    subjects: list[str] = []
    for operation in proposal.ops:
        if isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
            subjects.extend(update.id for update in operation.nodes)
        elif isinstance(operation, ProposalRemovalOperation):
            subjects.extend(operation.node_ids)
        elif isinstance(operation, ProposalSupersedeOperation):
            subjects.extend(item.id for item in operation.nodes)
        elif isinstance(operation, ProposalMergeOperation):
            subjects.extend(item.duplicate for item in operation.merges)
        elif isinstance(operation, ProposalProtectedRelationOperation):
            subjects.extend(edge.target for edge in operation.edges or [])
    return next(
        (node_id for node_id in [*subjects, *proposal.related_node_ids] if node_id in state.nodes),
        None,
    )


def proposal_evidence_cause_edge_ids(proposal: Proposal) -> set[str]:
    """The Evidence-to-belief edges a Proposal names as its cause."""

    return {
        update.cause.ref_id
        for operation in proposal.ops
        if isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation))
        for update in operation.nodes
        if isinstance(update.cause, EvidenceEdgeCause)
    }


def proposal_cause_evidence_sources(state: GraphState, proposal: Proposal) -> dict[str, str]:
    """Each current cause edge mapped to the Evidence it starts from, under rule 2.

    Rule 2 makes a pending Proposal depend on that Evidence too, so staleness
    and same-patch admission liveness both read it here.
    """

    if proposal.dependency_rule < 2:
        return {}
    return {
        edge_id: state.edges[edge_id].source
        for edge_id in proposal_evidence_cause_edge_ids(proposal)
        if edge_id in state.edges
    }


def _proposal_removal_targets(proposal: Proposal) -> set[str]:
    return {
        node_id
        for operation in proposal.ops
        if isinstance(operation, ProposalRemovalOperation)
        and not isinstance(operation, LegacyProposalRemovalOperation)
        for node_id in operation.node_ids
    }


def _proposal_reference_dependencies(proposal: Proposal) -> tuple[set[str], set[str]]:
    edge_ids: set[str] = set()
    decision_ids: set[str] = set()
    for op in proposal.ops:
        if isinstance(op, ProposalProtectedRelationOperation) and op.op == "remove_edges":
            edge_ids.update(op.edge_ids or [])
        if not isinstance(op, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
            continue
        for update in op.nodes:
            cause = update.cause
            if isinstance(cause, EvidenceEdgeCause):
                edge_ids.add(cause.ref_id)
            elif isinstance(cause, DecisionCause):
                decision_ids.add(cause.ref_id)
    return edge_ids, decision_ids


def _proposal_created_edge_ids(proposal: Proposal) -> set[str]:
    """Return edge IDs whose absence is part of the judged semantic effect."""

    edge_ids: set[str] = set()
    for operation in proposal.ops:
        if (
            isinstance(operation, ProposalProtectedRelationOperation)
            and not isinstance(operation, LegacyProposalProtectedRelationOperation)
            and operation.op == "create_edges"
        ):
            for raw in operation.edges or []:
                edge_ids.add(raw.id or f"{raw.source}::{raw.relation}::{raw.target}")
        elif isinstance(operation, ProposalSupersedeOperation) and not isinstance(
            operation, LegacyProposalSupersedeOperation
        ):
            edge_ids.update(
                f"{item.id}::supersedes::{item.superseded_by}"
                for item in operation.nodes
                if item.superseded_by is not None
            )
        elif isinstance(operation, ProposalMergeOperation) and not isinstance(
            operation, LegacyProposalMergeOperation
        ):
            edge_ids.update(
                f"{item.duplicate}::duplicate_of::{item.canonical}" for item in operation.merges
            )
    return edge_ids


def validate_proposal(
    proposal: Proposal | dict[str, Any],
    state: GraphState,
    report: ValidationReport,
    revision: int | None,
    *,
    project_truth_scope: Iterable[str],
    repository_aliases: Iterable[str],
    machine_aliases: Iterable[str] | None,
    default_run_truth_scope: Iterable[str],
    state_repository: str | None,
    validation_mode: Literal["admission", "replay"] = "replay",
    include_card_flags: bool = False,
    context_patch: Patch | None = None,
    writer: Writer | None = None,
) -> None:
    writer = writer or (
        Writer.from_patch(context_patch)
        if context_patch is not None
        else Writer(author="agent", profile=None, human_action=None, agent_action=None)
    )
    if not isinstance(proposal, Proposal):
        try:
            proposal = Proposal.model_validate(proposal)
        except ValidationError as exc:
            report.reject(
                "invalid-proposal",
                f"Proposal is malformed: {exc.errors()[0]['msg']}.",
                revision,
            )
            return
    if proposal.id in state.proposals:
        report.reject(
            "duplicate-proposal-id", f"Proposal {proposal.id!r} already exists.", revision
        )
        return
    if validation_mode == "admission" and proposal.base_rev != state.revision:
        report.reject(
            "proposal-base-revision",
            f"Proposal {proposal.id} must use the current graph revision {state.revision}.",
            revision,
        )
    if include_card_flags:
        missing = [
            name
            for name, value in proposal.card.model_dump().items()
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            report.flag(
                "incomplete-gated-card",
                f"Proposal {proposal.id} is missing readable card fields: {', '.join(missing)}.",
                revision,
            )
        card_text = " ".join(proposal.card.model_dump().values())
        unresolved = sorted(
            token for token in set(IDENTIFIER_RE.findall(card_text)) if token not in state.glossary
        )
        if unresolved:
            report.flag(
                "missing-glossary-term",
                f"Proposal {proposal.id} uses unexplained identifiers: {', '.join(unresolved)}.",
                revision,
            )
    if validation_mode == "admission" and (writer.author == "agent"):
        _validate_agent_proposal_boundary(
            proposal,
            state,
            report,
            revision,
            context_patch=context_patch,
            writer=writer,
        )
    _validate_proposal_ops(
        proposal,
        state,
        report,
        revision,
        project_truth_scope=project_truth_scope,
        repository_aliases=repository_aliases,
        machine_aliases=machine_aliases,
        default_run_truth_scope=default_run_truth_scope,
        state_repository=state_repository,
        validation_mode=validation_mode,
        context_patch=context_patch,
        writer=writer,
    )


def _validate_agent_proposal_boundary(
    proposal: Proposal,
    state: GraphState,
    report: ValidationReport,
    revision: int | None,
    *,
    context_patch: Patch | None,
    writer: Writer,
) -> None:
    def refuse(message: str) -> None:
        report.reject("invalid-agent-proposal-shape", message, revision)

    if len(proposal.ops) != 1 and not (
        _is_connected_merge_bundle(state, context_patch, proposal, writer)
        if proposal.dependency_rule >= 3
        else _is_same_node_merge_bundle(state, context_patch, proposal, writer)
    ):
        refuse(f"Proposal {proposal.id} must declare exactly one protected-change intent.")
        return
    validators = {
        CONTENT_CHANGE_INTENT: _validate_content_change_intent,
        REMOVAL_INTENT: _validate_removal_intent,
        SUPERSEDE_INTENT: _validate_supersede_intent,
        MERGE_INTENT: _validate_merge_intent,
        STATUS_CHANGE_INTENT: _validate_status_change_intent,
        STANDING_CHANGE_INTENT: _validate_standing_change_intent,
    }
    for operation_index, operation in enumerate(proposal.ops):
        intent = operation.intent
        if intent not in PROPOSAL_INTENTS:
            refuse(
                f"Proposal {proposal.id} must declare one of these intents: "
                f"{', '.join(sorted(PROPOSAL_INTENTS))}."
            )
            continue
        if intent == PROTECTED_RELATION_CHANGE_INTENT:
            error = _validate_protected_relation_change_intent(
                state,
                context_patch,
                operation,
                writer,
                proposal=proposal,
                operation_index=operation_index,
            )
        else:
            error = validators[intent](state, context_patch, operation, writer)
        if error is not None:
            refuse(f"Proposal {proposal.id} declares {intent!r}, but {error}")


def _is_connected_merge_bundle(
    state: GraphState, patch: Patch | None, proposal: Proposal, writer: Writer
) -> bool:
    """Only approvals that can invalidate one another belong in an atomic group.

    In particular a protected edge's remove/recreate pair shares its edge
    dependency. Each member still passes its intent validator and the entire
    ordered group passes the ordinary approval operation validator below.
    """

    if not _is_branch_merge(patch, writer) or proposal.dependency_rule < 3:
        return False
    try:
        return len(connected_proposal_operation_groups(state, proposal)) == 1
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        # The ordinary intent and operation validators report the invalid
        # effect. It cannot gain the exceptional multi-intent admission form.
        return False


def connected_proposal_groups(
    state: GraphState,
    proposals: Sequence[Proposal],
    *,
    links: Sequence[frozenset[str]] | None = None,
) -> tuple[tuple[int, ...], ...]:
    """Connect approval groups by their complete effects and dependencies.

    Recompute the combined footprint after each join: approval standing and
    generated effects depend on the whole ordered group. Shared reads alone do
    not join groups. Optional links keep replacements of one predecessor atomic.
    Projections are speculative because an edge replacement's individual create
    is not legal alone; callers validate each final group before filing.
    """
    from rcp.core.materialize import proposal_filing_dependency_rule

    components = [[index] for index in range(len(proposals))]
    while True:
        footprints = []
        for component in components:
            operations = [op for index in component for op in proposals[index].ops]
            draft = proposals[component[0]].model_copy(
                update={
                    "ops": operations,
                    "raised_rev": 0,
                    "dependency_rule": proposal_filing_dependency_rule(
                        len(operations), branch_merge=True
                    ),
                }
            )
            footprints.append(_proposal_effect_footprint(state, draft, validate=False))
        for left in range(len(components)):
            joined = next(
                (
                    right
                    for right in range(left + 1, len(components))
                    if footprints[left].effects & footprints[right].dependencies
                    or footprints[right].effects & footprints[left].dependencies
                    or (
                        links is not None
                        and any(
                            links[i] & links[j] for i in components[left] for j in components[right]
                        )
                    )
                ),
                None,
            )
            if joined is not None:
                components[left] = sorted(components[left] + components.pop(joined))
                break
        else:
            return tuple(tuple(component) for component in components)


def connected_proposal_operation_groups(
    state: GraphState, proposal: Proposal
) -> tuple[tuple[ProposalOperation, ...], ...]:
    """Split an ordered approval using the same rule as planning and admission."""
    groups = connected_proposal_groups(
        state,
        [proposal.model_copy(update={"ops": [operation]}) for operation in proposal.ops],
    )
    return tuple(tuple(proposal.ops[index] for index in group) for group in groups)


def _is_same_node_merge_bundle(
    state: GraphState, patch: Patch | None, proposal: Proposal, writer: Writer
) -> bool:
    if not _is_branch_merge(patch, writer) or not 2 <= len(proposal.ops) <= 3:
        return False
    intents: set[str] = set()
    targets: set[str] = set()
    for operation in proposal.ops:
        if operation.intent in intents:
            return False
        intents.add(operation.intent)
        if isinstance(operation, ProposalStandingChangeOperation):
            targets.add(operation.node_id)
        elif isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
            if len(operation.nodes) != 1:
                return False
            targets.add(operation.nodes[0].id)
        else:
            return False
    return len(targets) == 1 and isinstance(
        state.nodes.get(next(iter(targets))), (ResearchQuestion, Hypothesis)
    )


def _validate_content_change_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
) -> str | None:
    update, error = _one_update(operation)
    if error is not None:
        return error
    assert update is not None
    if update.cause is not None or update.base_updated_rev is not None:
        return "a content change requires exactly id and changes, with no cause."
    node = _existing_protected_node(state, context_patch, update.id)
    if node is None:
        return f"a content change must target one existing {_protected_names(state)}."
    changes = update.changes
    if not changes:
        return "a content change must contain at least one changed field."
    if "status" in changes and isinstance(node, Hypothesis):
        return "Hypothesis status belongs in a status_change intent."
    if all(getattr(node, field, object()) == value for field, value in changes.items()):
        return "a content change must actually change the target node."
    return None


def _validate_status_change_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
) -> str | None:
    update, error = _one_update(operation)
    if error is not None:
        return error
    assert update is not None
    node = state.nodes.get(update.id)
    if isinstance(node, Decision):
        return (
            f"Decision {node.id!r} cannot be proposed by an agent; its outcome requires the "
            "decide_decision action."
        )
    if not isinstance(node, Hypothesis):
        return "a status change must target one Hypothesis in the staged graph."
    if update.cause is None or update.base_updated_rev is not None:
        return "a status change requires exactly id, changes, and cause."
    changes = update.changes
    if set(changes) != HYPOTHESIS_PROPOSAL_FIELDS:
        return "a status change may change only Hypothesis status."
    if changes["status"] == node.status:
        return "a status change must actually change the Hypothesis status."
    cause = update.cause
    if isinstance(cause, HumanEditCause) and _is_branch_merge(context_patch, writer):
        # human_edit carries a proven source judgment; it stays pending for
        # human review on the destination.
        return None
    if not isinstance(cause, EvidenceEdgeCause) or not cause.ref_id:
        return "a status change requires an evidence_edge cause naming an epistemic edge."
    return None


def _is_branch_merge(patch: Patch | None, writer: Writer) -> bool:
    return (
        patch is not None
        and writer.author == "agent"
        and writer.profile == "orchestrator"
        and patch.branch_merge is not None
    )


def _validate_standing_change_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
) -> str | None:
    if not isinstance(operation, ProposalStandingChangeOperation):
        return "a standing change requires one set_standing operation."
    if not _is_branch_merge(context_patch, writer):
        return "standing changes may be proposed only by a canonical branch merge."
    node = state.nodes.get(operation.node_id)
    if node is None or node.standing == operation.standing:
        return "a standing change must change the standing of one existing node."
    return None


def _validate_removal_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
) -> str | None:
    if not isinstance(operation, ProposalRemovalOperation):
        return "removal requires exactly one remove_nodes operation."
    node_ids = operation.node_ids
    if len(node_ids) != 1:
        return "removal must name exactly one node."
    if _existing_protected_node(state, context_patch, node_ids[0]) is None:
        node = state.nodes.get(node_ids[0])
        if (
            _is_branch_merge(context_patch, writer)
            and node is not None
            and node.standing == "accepted"
        ):
            return None
        return f"removal must target one existing {_protected_names(state)}."
    return None


def _validate_supersede_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
) -> str | None:
    if not isinstance(operation, ProposalSupersedeOperation):
        return "supersede requires exactly one supersede_nodes operation."
    items = operation.nodes
    if len(items) != 1:
        return "supersede must name exactly one predecessor and successor."
    item = items[0]
    if item.cause is not None:
        return "supersede accepts only id, superseded_by, and optional explanation."
    node_id = item.id
    successor_id = item.superseded_by
    predecessor = _existing_protected_node(state, context_patch, node_id)
    if predecessor is None:
        return f"supersede must retire one existing {_protected_names(state)}."
    if not isinstance(successor_id, str) or not successor_id or successor_id == node_id:
        return "supersede must name a distinct successor node."
    successor_type = _protected_node_type(state, context_patch, successor_id)
    if successor_type is None:
        return f"supersede must name a {_protected_names(state)} successor."
    if successor_type != predecessor.type:
        return "supersede predecessor and successor must be the same protected belief type."
    return None


def _validate_merge_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
) -> str | None:
    if not isinstance(operation, ProposalMergeOperation):
        return "merge requires exactly one merge_nodes operation."
    items = operation.merges
    if len(items) != 1:
        return "merge must name exactly one duplicate and canonical node."
    item = items[0]
    if item.cause is not None:
        return "merge accepts only duplicate, canonical, and optional explanation."
    duplicate_id = item.duplicate
    canonical_id = item.canonical
    duplicate = _existing_protected_node(state, context_patch, duplicate_id)
    if duplicate is None:
        return f"merge must fold one existing {_protected_names(state)}."
    if not isinstance(canonical_id, str) or not canonical_id or canonical_id == duplicate_id:
        return "merge must name a distinct canonical node."
    canonical_type = _protected_node_type(state, context_patch, canonical_id)
    if canonical_type is None:
        return f"merge must name a {_protected_names(state)} canonical node."
    if canonical_type != duplicate.type:
        return "merge duplicate and canonical must be the same protected belief type."
    return None


def _validate_protected_relation_change_intent(
    state: GraphState,
    context_patch: Patch | None,
    operation: ProposalOperation,
    writer: Writer,
    *,
    proposal: Proposal,
    operation_index: int,
) -> str | None:
    if not isinstance(operation, ProposalProtectedRelationOperation):
        return "a protected relation change requires one create_edges or remove_edges operation."
    name = operation.op
    if name == "create_edges":
        edges = operation.edges or []
        if len(edges) != 1:
            return "a protected relation change must create exactly one edge."
        edge = edges[0]
        endpoints = (edge.source, edge.target)
        if edge.relation not in PROTECTED_EPISTEMIC_RELATIONS:
            return "a protected relation change must use a protected relation."
        if edge.relation in {"supersedes", "duplicate_of"}:
            previous = state.edges.get(edge.id) if edge.id is not None else None
            preceding = proposal.ops[:operation_index]
            replaces_existing = (
                _is_branch_merge(context_patch, writer)
                and proposal.dependency_rule >= 3
                and previous is not None
                and (previous.source, previous.target, previous.relation)
                == (edge.source, edge.target, edge.relation)
                and any(
                    isinstance(item, ProposalProtectedRelationOperation)
                    and item.op == "remove_edges"
                    and edge.id in (item.edge_ids or [])
                    for item in preceding
                )
            )
            # Editing an existing relation's prose does not retire its node
            # again. New or retargeted retirement relations still require the
            # semantic operation that owns their lifecycle effects.
            if not replaces_existing:
                return "supersedes and duplicate_of must use their dedicated supersede or merge intent."
        if context_patch is not None and any(
            node_id in created_node_ids(context_patch) for node_id in endpoints
        ):
            return "connecting a node created in the outer Patch is direct, not a Proposal."
        if not any(
            _existing_protected_node(state, context_patch, node_id) for node_id in endpoints
        ):
            return "a protected relation change must touch an existing belief node."
        return None
    if name == "remove_edges":
        edge_ids = operation.edge_ids or []
        if len(edge_ids) != 1:
            return "a protected relation change must remove exactly one edge."
        edge_id = edge_ids[0]
        edge = state.edges.get(edge_id) if isinstance(edge_id, str) else None
        if edge is None or (
            context_patch is not None and edge_id in created_edge_ids(context_patch)
        ):
            return "a protected relation removal must target one existing edge."
        if edge.relation not in PROTECTED_EPISTEMIC_RELATIONS:
            return "a protected relation change must use a protected relation."
        if not any(
            _existing_protected_node(state, context_patch, node_id)
            for node_id in (edge.source, edge.target)
        ):
            return "a protected relation change must touch an existing belief node."
        return None
    return "a protected relation change requires one create_edges or remove_edges operation."


def _one_update(operation: ProposalOperation) -> tuple[NodeUpdate | None, str | None]:
    if not isinstance(operation, (ProposalContentChangeOperation, ProposalStatusChangeOperation)):
        return None, "the declared intent requires exactly one update_nodes operation."
    updates = operation.nodes
    if len(updates) != 1:
        return None, "the declared intent must update exactly one node."
    return updates[0], None


def _existing_protected_node(
    state: GraphState,
    context_patch: Patch | None,
    node_id: Any,
) -> ResearchQuestion | Hypothesis | None:
    if not isinstance(node_id, str):
        return None
    node = state.nodes.get(node_id)
    if (
        context_patch is not None
        and node_id in created_node_ids(context_patch)
        and node is not None
        and node.created_rev == context_patch.revision
    ):
        return None
    return node if isinstance(node, (ResearchQuestion, Hypothesis)) else None


def _protected_node_type(
    state: GraphState,
    context_patch: Patch | None,
    node_id: Any,
) -> str | None:
    node = state.nodes.get(node_id) if isinstance(node_id, str) else None
    if isinstance(node, (ResearchQuestion, Hypothesis)):
        return node.type
    if context_patch is None:
        return None
    for operation in context_patch.ops:
        if not isinstance(operation, CreateNodesOperation):
            continue
        for raw in operation.nodes:
            if raw.id != node_id:
                continue
            node_type = raw.type
            return node_type if node_type in {"research_question", "hypothesis"} else None
    return None


def _validate_proposal_ops(
    proposal: Proposal,
    state: GraphState,
    report: ValidationReport,
    revision: int | None,
    *,
    project_truth_scope: Iterable[str],
    repository_aliases: Iterable[str],
    machine_aliases: Iterable[str] | None,
    default_run_truth_scope: Iterable[str],
    state_repository: str | None,
    validation_mode: Literal["admission", "replay"],
    context_patch: Patch | None,
    writer: Writer,
) -> None:
    # Imported lazily because the operation registry reaches this module.
    from rcp.core.validation.context import OpContext
    from rcp.core.validation.patch import _validate_operations

    if validation_mode == "admission" and context_patch is not None:
        for operation in proposal.ops:
            if not isinstance(operation, ProposalContentChangeOperation):
                continue
            for update in operation.nodes:
                if update.changes.get("source_refs"):
                    validate_source_refs(
                        {"source_refs": update.changes["source_refs"]},
                        context_patch,
                        report,
                        writer=writer,
                    )

    synthetic_state = state.model_copy(
        update={
            "nodes": dict(state.nodes),
            "edges": dict(state.edges),
            "proposals": dict(state.proposals),
            "ambiguities": dict(state.ambiguities),
            "glossary": dict(state.glossary),
            "config_revisions": dict(state.config_revisions),
        }
    )
    synthetic_patch = Patch(
        revision=revision or state.revision + 1,
        kind="approval",
        author="human",
        summary=f"Validate replay operations for {proposal.id}.",
        ops=graph_operations_from_proposal(proposal.ops),
    )
    replay_report = ValidationReport()
    replay_context = OpContext(
        state=synthetic_state,
        initial_state=synthetic_state,
        patch=synthetic_patch,
        writer=Writer.from_patch(synthetic_patch),
        report=replay_report,
        revision=revision,
        project_truth_scope=set(project_truth_scope),
        repositories=set(repository_aliases),
        machines=set(machine_aliases) if machine_aliases is not None else None,
        default_run_truth_scope=set(default_run_truth_scope),
        state_repository=state_repository,
        mode=validation_mode,
        reference_patch=context_patch,
    )
    _validate_operations(replay_context)
    for diagnostic in replay_report.messages:
        if diagnostic.level == "reject":
            report.messages.append(
                diagnostic.model_copy(
                    update={
                        "message": f"Proposal {proposal.id}: {diagnostic.message}",
                        "rule_id": PROPOSAL_REPLAY_RULE_ID,
                        "operation_index": next(
                            (
                                index
                                for index, operation in enumerate(context_patch.ops)
                                if isinstance(operation, CreateProposalsOperation)
                                and any(item.id == proposal.id for item in operation.proposals)
                            ),
                            None,
                        )
                        if context_patch is not None
                        else None,
                    }
                )
            )
