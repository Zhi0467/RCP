from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from rcp.core.authority import EVIDENCE_EDGE_CAUSE_KIND, EVIDENCE_RELATIONS
from rcp.core.models import (
    RELATION_SPEC,
    Experiment,
    ExperimentAttempt,
    ExperimentDecisionPin,
    GraphState,
    Patch,
)
from rcp.core.ontology import custom_relation
from rcp.core.validation.report import ValidationReport

_ATTEMPT_CLOSE_FIELDS = frozenset(
    {"status", "source_refs", "outcome", "failure_reason", "finished_at"}
)
_TERMINAL_ATTEMPT_STATUSES = frozenset({"failed", "completed", "cancelled", "superseded"})


def validate_experiment_loop_authority(
    state: GraphState,
    patch: Patch,
    report: ValidationReport,
    *,
    control_node_id: str | None,
    decision_bundle: Iterable[ExperimentDecisionPin],
) -> None:
    revision = patch.revision or None
    experiment = state.nodes.get(control_node_id) if control_node_id else None
    if not isinstance(experiment, Experiment):
        report.reject(
            "experiment-loop-control-node",
            "An experiment-loop patch requires an RCP-bound Experiment node.",
            revision,
            related_node_ids=[control_node_id] if control_node_id else [],
        )
        return

    pinned = list(decision_bundle)
    pinned_ids = [item.decision_id for item in pinned]
    if len(pinned_ids) != len(set(pinned_ids)):
        report.reject(
            "experiment-loop-decision-bundle",
            "The RCP-bound governing decision bundle contains duplicate decisions.",
            revision,
            related_node_ids=[experiment.id, *pinned_ids],
        )

    has_proposals = any(
        op.get("op") == "create_proposals" and bool(op.get("proposals")) for op in patch.ops
    )
    created_types = _created_node_types(patch.ops)
    for op in patch.ops:
        name = op.get("op")
        if name == "update_nodes":
            _validate_updates(op, experiment, pinned, has_proposals, report, revision)
        elif name == "create_nodes":
            _validate_created_nodes(op, experiment.id, report, revision)
        elif name == "create_edges":
            _validate_created_edges(state, op, experiment.id, created_types, report, revision)
        elif name == "create_proposals":
            _validate_proposals(
                op,
                experiment.id,
                set(pinned_ids),
                _tested_hypothesis_ids(state, experiment.id),
                _grounding_edge_ids(state, patch.ops, created_types),
                report,
                revision,
            )
        else:
            report.reject(
                "experiment-loop-operation",
                f"Experiment loop {experiment.id} cannot use operation {name!r}.",
                revision,
                related_node_ids=[experiment.id],
            )


def _validate_updates(
    op: dict[str, Any],
    experiment: Experiment,
    pinned: list[ExperimentDecisionPin],
    has_proposals: bool,
    report: ValidationReport,
    revision: int | None,
) -> None:
    for update in op.get("nodes", []):
        node_id = update.get("id")
        changes = update.get("changes")
        if node_id != experiment.id:
            report.reject(
                "experiment-loop-foreign-update",
                f"Experiment loop {experiment.id} cannot update node {node_id!r}.",
                revision,
                related_node_ids=[
                    item for item in (experiment.id, node_id) if isinstance(item, str)
                ],
            )
            continue
        if not isinstance(changes, dict):
            continue
        forbidden = sorted(set(changes) - {"attempts", "status"})
        if forbidden:
            report.reject(
                "experiment-loop-experiment-field",
                f"Experiment loop {experiment.id} may update only status and attempts; "
                f"refused: {', '.join(forbidden)}.",
                revision,
                related_node_ids=[experiment.id],
            )
        if "attempts" in changes:
            _validate_attempts(
                experiment,
                changes["attempts"],
                pinned,
                has_proposals,
                report,
                revision,
            )


def _validate_attempts(
    experiment: Experiment,
    raw_attempts: Any,
    pinned: list[ExperimentDecisionPin],
    has_proposals: bool,
    report: ValidationReport,
    revision: int | None,
) -> None:
    if not isinstance(raw_attempts, list):
        return
    try:
        attempts = [ExperimentAttempt.model_validate(item) for item in raw_attempts]
    except ValidationError:
        return

    previous = experiment.attempts
    if len(attempts) < len(previous):
        report.reject(
            "experiment-loop-attempt-removal",
            f"Experiment loop {experiment.id} cannot remove attempt records.",
            revision,
            related_node_ids=[experiment.id],
        )
        return

    if len(attempts) > len(previous) and len(attempts) > experiment.attempt_ceiling:
        report.reject(
            "experiment-loop-attempt-ceiling",
            f"Experiment {experiment.id} is limited to {experiment.attempt_ceiling} attempts.",
            revision,
            related_node_ids=[experiment.id],
        )

    if len(attempts) - len(previous) > 1:
        report.reject(
            "experiment-loop-multiple-attempts",
            f"Experiment loop {experiment.id} may append at most one attempt per patch.",
            revision,
            related_node_ids=[experiment.id],
        )

    for before, after in zip(previous, attempts, strict=False):
        before_fixed = before.model_dump(mode="python", exclude=_ATTEMPT_CLOSE_FIELDS)
        after_fixed = after.model_dump(mode="python", exclude=_ATTEMPT_CLOSE_FIELDS)
        if before_fixed != after_fixed:
            report.reject(
                "experiment-loop-attempt-mutation",
                f"Experiment loop {experiment.id} cannot rewrite attempt {before.id!r} or "
                "its pinned decision bundle.",
                revision,
                related_node_ids=[experiment.id],
            )
            continue
        if before != after and (
            before.status in _TERMINAL_ATTEMPT_STATUSES
            or after.status not in _TERMINAL_ATTEMPT_STATUSES
        ):
            report.reject(
                "experiment-loop-attempt-close",
                f"Experiment loop {experiment.id} may only close a nonterminal attempt.",
                revision,
                related_node_ids=[experiment.id],
            )

    expected_bundle = [item.model_dump(mode="python") for item in pinned]
    appended = attempts[len(previous) :]
    for attempt in appended:
        actual_bundle = [item.model_dump(mode="python") for item in attempt.decision_bundle]
        if actual_bundle != expected_bundle:
            report.reject(
                "experiment-loop-pinned-bundle",
                f"New attempt {attempt.id!r} must copy the RCP-pinned governing decisions.",
                revision,
                related_node_ids=[experiment.id, *[item.decision_id for item in pinned]],
            )
        if attempt.attempt_kind == "proposal_only" and attempt.job_refs:
            report.reject(
                "experiment-loop-proposal-job",
                f"Proposal-only attempt {attempt.id!r} cannot name an external job.",
                revision,
                related_node_ids=[experiment.id],
            )
        if (
            attempt.attempt_kind == "proposal_only"
            and attempt.status not in _TERMINAL_ATTEMPT_STATUSES
        ):
            report.reject(
                "experiment-loop-proposal-status",
                f"Proposal-only attempt {attempt.id!r} must be terminal in the turn that creates it.",
                revision,
                related_node_ids=[experiment.id],
            )

    if any(attempt.attempt_kind == "proposal_only" for attempt in appended) and not has_proposals:
        report.reject(
            "experiment-loop-proposal-attempt",
            f"Proposal-only attempt in experiment {experiment.id} requires a proposal.",
            revision,
            related_node_ids=[experiment.id],
        )

    ids = [attempt.id for attempt in attempts]
    if len(ids) != len(set(ids)):
        report.reject(
            "experiment-loop-duplicate-attempt",
            f"Experiment {experiment.id} cannot contain duplicate attempt ids.",
            revision,
            related_node_ids=[experiment.id],
        )


def _validate_created_nodes(
    op: dict[str, Any],
    experiment_id: str,
    report: ValidationReport,
    revision: int | None,
) -> None:
    for node in op.get("nodes", []):
        if node.get("type") not in {"evidence", "blocker"}:
            report.reject(
                "experiment-loop-created-node",
                f"Experiment loop {experiment_id} may create only Evidence or Blocker nodes.",
                revision,
                related_node_ids=[experiment_id],
            )


def _created_node_types(ops: Iterable[dict[str, Any]]) -> dict[str, str]:
    created: dict[str, str] = {}
    for op in ops:
        if op.get("op") != "create_nodes":
            continue
        for node in op.get("nodes", []):
            node_id = node.get("id")
            node_type = node.get("type")
            if isinstance(node_id, str) and isinstance(node_type, str):
                created[node_id] = node_type
    return created


# The loop may attach its own output to its own experiment, and nothing else.
# `produces` is provenance and `blocked_by` is self-blocking, so neither widens
# its authority; both targets must be nodes this same patch created.
_SELF_ATTACHMENT_RELATIONS = {"produces": "evidence", "blocked_by": "blocker"}


def _validate_created_edges(
    state: GraphState,
    op: dict[str, Any],
    experiment_id: str,
    created_types: dict[str, str],
    report: ValidationReport,
    revision: int | None,
) -> None:
    for edge in op.get("edges", []):
        relation_name = edge.get("relation")
        if relation_name in _SELF_ATTACHMENT_RELATIONS:
            expected_type = _SELF_ATTACHMENT_RELATIONS[relation_name]
            target = edge.get("target")
            if edge.get("source") == experiment_id and created_types.get(target) == expected_type:
                continue
            report.reject(
                "experiment-loop-self-attachment",
                f"Experiment loop {experiment_id} may use {relation_name!r} only from its own "
                f"experiment to a {expected_type} node this patch creates.",
                revision,
                related_node_ids=[
                    item for item in (experiment_id, target) if isinstance(item, str)
                ],
            )
            continue
        base = RELATION_SPEC.get(relation_name)
        relation = custom_relation(state.ontology, relation_name)
        layer = base.layer if base is not None else relation.layer if relation is not None else None
        if layer != "epistemic":
            report.reject(
                "experiment-loop-edge-layer",
                f"Experiment loop {experiment_id} may assert only epistemic edges, or attach its "
                "own evidence and blockers to its experiment.",
                revision,
                related_node_ids=[experiment_id],
            )


def _tested_hypothesis_ids(state: GraphState, experiment_id: str) -> set[str]:
    return {
        edge.target
        for edge in state.edges.values()
        if edge.source == experiment_id and edge.relation == "tests"
    }


def _grounding_edge_ids(
    state: GraphState,
    ops: Iterable[dict[str, Any]],
    created_types: dict[str, str],
) -> dict[str, set[str]]:
    """Same-patch Evidence -> Hypothesis edge ids grouped by target.

    A belief proposal must rest on evidence the same turn recorded, so the human
    is never asked to move a belief the patch supplies no reason for.
    """

    grounded: dict[str, set[str]] = {}
    for op in ops:
        if op.get("op") != "create_edges":
            continue
        for edge in op.get("edges", []):
            if not isinstance(edge, dict):
                continue
            target = edge.get("target")
            source = edge.get("source")
            existing_source = state.nodes.get(source)
            source_type = (
                existing_source.type if existing_source is not None else created_types.get(source)
            )
            if (
                edge.get("relation") in EVIDENCE_RELATIONS
                and source_type == "evidence"
                and isinstance(target, str)
            ):
                edge_id = edge.get("id") or f"{source}::{edge.get('relation')}::{target}"
                grounded.setdefault(target, set()).add(edge_id)
    return grounded


def _validate_proposals(
    op: dict[str, Any],
    experiment_id: str,
    governing_decision_ids: set[str],
    tested_hypothesis_ids: set[str],
    grounding_edge_ids: dict[str, set[str]],
    report: ValidationReport,
    revision: int | None,
) -> None:
    """Admit the two proposal shapes a loop may raise, and nothing else.

    A decision proposal asks the human to change a pinned governing choice. A
    belief proposal asks the human to accept the belief change its own evidence
    implies — the loop may never apply that change itself, which is why the edge
    is asserted while the status move waits in Inbox.
    """

    for proposal in op.get("proposals", []):
        replay_ops = proposal.get("ops")
        target_ids = _proposal_update_targets(replay_ops)

        if target_ids and target_ids <= tested_hypothesis_ids:
            _validate_belief_proposal(
                proposal,
                experiment_id,
                target_ids,
                grounding_edge_ids,
                report,
                revision,
            )
            continue

        if not target_ids or not target_ids <= governing_decision_ids:
            report.reject(
                "experiment-loop-proposal-operations",
                f"Experiment loop {experiment_id} proposals may update only pinned governing "
                "decisions.",
                revision,
                related_node_ids=[experiment_id, *sorted(target_ids)],
            )


def _proposal_update_targets(replay_ops: Any) -> set[str]:
    """Node ids a proposal's replay would update, or empty when it is malformed."""

    if not isinstance(replay_ops, list) or not replay_ops:
        return set()
    targets: set[str] = set()
    for replay_op in replay_ops:
        if not isinstance(replay_op, dict) or replay_op.get("op") != "update_nodes":
            return set()
        updates = replay_op.get("nodes")
        if not isinstance(updates, list) or not updates:
            return set()
        ids = {item.get("id") for item in updates if isinstance(item, dict)}
        if len(ids) != len(updates) or not all(isinstance(item, str) for item in ids):
            return set()
        targets.update(item for item in ids if isinstance(item, str))
    return targets


def _validate_belief_proposal(
    proposal: dict[str, Any],
    experiment_id: str,
    target_ids: set[str],
    grounding_edge_ids: dict[str, set[str]],
    report: ValidationReport,
    revision: int | None,
) -> None:
    def refuse(code: str, message: str) -> None:
        report.reject(
            code,
            message,
            revision,
            related_node_ids=[experiment_id, *sorted(target_ids)],
        )

    if len(target_ids) != 1:
        refuse(
            "experiment-loop-belief-proposal-scope",
            f"Experiment loop {experiment_id} may propose one belief change at a time.",
        )
        return
    hypothesis_id = next(iter(target_ids))
    if hypothesis_id not in grounding_edge_ids:
        refuse(
            "experiment-loop-belief-grounding",
            f"A belief proposal from {experiment_id} must rest on an evidence edge this patch "
            f"asserts into {hypothesis_id}.",
        )

    updates = [
        item
        for replay_op in proposal.get("ops", [])
        for item in replay_op.get("nodes", [])
        if isinstance(item, dict)
    ]
    for update in updates:
        changes = update.get("changes")
        if not isinstance(changes, dict) or set(changes) != {"status"}:
            refuse(
                "experiment-loop-belief-proposal-operations",
                f"A belief proposal from {experiment_id} may change only {hypothesis_id}'s status.",
            )
            continue
        cause = update.get("cause")
        if (
            not isinstance(cause, dict)
            or cause.get("kind") != EVIDENCE_EDGE_CAUSE_KIND
            or cause.get("ref_id") not in grounding_edge_ids.get(hypothesis_id, set())
        ):
            refuse(
                "experiment-loop-belief-cause",
                f"A belief proposal from {experiment_id} must name one of this patch's "
                f"Evidence edges into {hypothesis_id} as its cause.",
            )
