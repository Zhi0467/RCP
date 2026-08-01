from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rcp.core.models import (
    Blocker,
    Decision,
    Experiment,
    ExperimentDecisionPin,
    GraphState,
    Proposal,
)


class DecisionDrift(BaseModel):
    """A governing decision that moved since an attempt pinned it."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    pinned_option: str
    pinned_revision: int
    current_option: str | None = None
    current_status: str | None = None
    proposed: bool = False


class ExperimentControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    reasons: list[str] = Field(default_factory=list)
    attempts_used: int = Field(ge=0)
    attempt_ceiling: int = Field(ge=1)
    active: bool
    governing_decisions: list[ExperimentDecisionPin] = Field(default_factory=list)
    decision_drift: list[DecisionDrift] = Field(default_factory=list)


def decision_drift(state: GraphState, pins: Iterable[ExperimentDecisionPin]) -> list[DecisionDrift]:
    """Report which pinned decisions no longer match the graph.

    Drift never gates anything. It is the fact RCP hands to a woken loop turn and
    shows beside a finished experiment, so nobody has to notice it themselves.
    """

    drifted: list[DecisionDrift] = []
    for pin in pins:
        node = state.nodes.get(pin.decision_id)
        decision = node if isinstance(node, Decision) else None
        proposed = _has_pending_proposal(state, pin.decision_id)
        moved = (
            decision is None
            or decision.status != "decided"
            or decision.selected_option != pin.selected_option
            or decision.updated_rev != pin.decision_revision
        )
        if not moved and not proposed:
            continue
        drifted.append(
            DecisionDrift(
                decision_id=pin.decision_id,
                pinned_option=pin.selected_option,
                pinned_revision=pin.decision_revision,
                current_option=decision.selected_option if decision else None,
                current_status=decision.status if decision else None,
                proposed=proposed,
            )
        )
    return drifted


def governing_decision_bundle(state: GraphState, experiment_id: str) -> list[ExperimentDecisionPin]:
    """Return the experiment's decided governing choices in stable order."""

    bundle: list[ExperimentDecisionPin] = []
    for decision_id in _related_targets(state, experiment_id, "governed_by"):
        node = state.nodes.get(decision_id)
        if not isinstance(node, Decision) or node.status != "decided" or not node.selected_option:
            continue
        bundle.append(
            ExperimentDecisionPin(
                decision_id=node.id,
                decision_revision=node.updated_rev,
                selected_option=node.selected_option,
            )
        )
    return bundle


def derive_experiment_control_state(
    state: GraphState,
    experiment_id: str,
    active_control_node_ids: Iterable[str] = (),
) -> ExperimentControlState:
    node = state.nodes.get(experiment_id)
    if not isinstance(node, Experiment):
        raise ValueError(f"Node {experiment_id!r} is not an Experiment.")

    reasons: list[str] = []
    governing_ids = _related_targets(state, experiment_id, "governed_by")
    governing = governing_decision_bundle(state, experiment_id)

    for decision_id in governing_ids:
        decision = state.nodes.get(decision_id)
        if not isinstance(decision, Decision):
            reasons.append(f"Governing decision {decision_id} is missing.")
        elif decision.status != "decided" or not decision.selected_option:
            reasons.append(f"Decision {decision_id} is not decided with a selected option.")
        if _has_pending_proposal(state, decision_id):
            reasons.append(f"Decision {decision_id} has a pending proposal.")

    for blocker_id in _related_targets(state, experiment_id, "blocked_by"):
        blocker = state.nodes.get(blocker_id)
        if not isinstance(blocker, Blocker):
            reasons.append(f"Blocker {blocker_id} is missing.")
        elif blocker.status == "open":
            reasons.append(f"Blocker {blocker_id} is open.")

    attempts_used = len(node.attempts)
    if attempts_used >= node.attempt_ceiling:
        reasons.append(
            f"Attempt ceiling reached: {attempts_used} of {node.attempt_ceiling} attempts used."
        )

    nonterminal = {"planned", "submitted", "running"}
    active = experiment_id in set(active_control_node_ids) or any(
        attempt.status in nonterminal for attempt in node.attempts
    )
    if active:
        reasons.append("An experiment loop is already active.")

    # Drift is reported against the newest attempt's pins, so a finished
    # experiment still says its result was produced under an older decision.
    latest = node.attempts[-1] if node.attempts else None
    return ExperimentControlState(
        ready=not reasons,
        reasons=reasons,
        attempts_used=attempts_used,
        attempt_ceiling=node.attempt_ceiling,
        active=active,
        governing_decisions=governing,
        decision_drift=decision_drift(state, latest.decision_bundle) if latest else [],
    )


def _related_targets(state: GraphState, source_id: str, relation: str) -> list[str]:
    return sorted(
        {
            edge.target
            for edge in state.edges.values()
            if edge.source == source_id and edge.relation == relation
        }
    )


def _has_pending_proposal(state: GraphState, decision_id: str) -> bool:
    """True when a pending proposal would actually change this decision.

    `related_node_ids` is a see-also list — a seed proposal may name a decision it
    merely referenced. Only the operations a proposal would replay decide whether
    approving it changes the decision, so only those gate the experiment.
    """

    return any(
        proposal.status == "pending" and _proposal_changes(proposal, decision_id)
        for proposal in state.proposals.values()
    )


def _proposal_changes(proposal: Proposal, decision_id: str) -> bool:
    return any(decision_id in _op_target_ids(op) for op in proposal.ops)


def _op_target_ids(op: dict[str, Any]) -> set[str]:
    name = op.get("op")
    targets: set[str] = set()
    if name in {"create_nodes", "update_nodes", "supersede_nodes"}:
        for item in op.get("nodes", []):
            if isinstance(item, dict):
                targets.update(
                    value
                    for value in (item.get("id"), item.get("superseded_by"))
                    if isinstance(value, str)
                )
    elif name == "merge_nodes":
        for item in op.get("merges", []):
            if isinstance(item, dict):
                targets.update(
                    value
                    for value in (item.get("duplicate"), item.get("canonical"))
                    if isinstance(value, str)
                )
    return targets
