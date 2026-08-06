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
    """A governing decision that moved since the loop episode pinned it."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    pinned_option: str
    pinned_revision: int
    current_option: str | None = None
    current_status: str | None = None
    proposed: bool = False


class ExperimentSessionBinding(BaseModel):
    """What the human can see of the episode's pinned execution and session.

    The native session id itself never leaves the backend; whether one is bound
    is the only part of it the operational surface needs.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    execution_host: str | None = None
    run_truth_scope: list[str] | None = None
    native_session_bound: bool = False
    diagnostic: str | None = None


class ExperimentOperationalState(BaseModel):
    """Live loop lifecycle for the Runs surface, separate from graph meaning."""

    model_config = ConfigDict(extra="forbid")

    task_active: bool = False
    detached_work_active: bool = False
    watcher_degraded: bool = False
    watcher_completion_pending: bool = False
    episode_exited: bool = False
    stop_requested: bool = False
    stop_settled: bool = False
    chat_id: str | None = None
    current_operation_id: str | None = None
    current_status: str | None = None
    current_phase: str | None = None
    current_status_message: str | None = None
    current_last_activity_at: str | None = None
    current_invocation: int | None = Field(default=None, ge=1)
    session: ExperimentSessionBinding = Field(default_factory=ExperimentSessionBinding)

    @property
    def stopping(self) -> bool:
        return self.stop_requested and not self.stop_settled


class ExperimentControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    reasons: list[str] = Field(default_factory=list)
    invocations_used: int = Field(ge=0)
    invocation_ceiling: int = Field(ge=1)
    invocations_remaining: int = Field(ge=0)
    episode_id: str | None = None
    paused: bool
    active: bool
    governing_decisions: list[ExperimentDecisionPin] = Field(default_factory=list)
    decision_drift: list[DecisionDrift] = Field(default_factory=list)
    operational: ExperimentOperationalState = Field(default_factory=ExperimentOperationalState)


class ExperimentInvocationAdmission(BaseModel):
    """The current episode binding for one newly admitted watcher invocation."""

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    invocation: int = Field(ge=1)
    invocation_ceiling: int = Field(ge=1)
    decision_bundle: list[ExperimentDecisionPin] = Field(default_factory=list)


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
    *,
    episode_id: str | None = None,
    invocations_used: int = 0,
    invocation_ceiling: int | None = None,
    paused: bool = False,
    detached_work_active: bool = False,
    episode_decision_bundle: Iterable[ExperimentDecisionPin] | None = None,
    operational: ExperimentOperationalState | None = None,
) -> ExperimentControlState:
    node = state.nodes.get(experiment_id)
    if not isinstance(node, Experiment):
        raise ValueError(f"Node {experiment_id!r} is not an Experiment.")

    reasons = experiment_graph_precondition_reasons(state, experiment_id)
    governing = governing_decision_bundle(state, experiment_id)

    active = experiment_id in set(active_control_node_ids)
    if active:
        reasons.append("An experiment loop is already active.")
    if operational is not None and operational.stopping:
        # Stop is graceful: the already-authorized turn finishes, and only then
        # may a human cross the authority boundary with a fresh Run.
        reasons.append("A graceful stop is finishing the current loop turn.")

    if episode_id is None:
        if (
            invocation_ceiling is not None
            or invocations_used != 0
            or episode_decision_bundle is not None
        ):
            raise ValueError("Loop runtime fields require an episode id.")
        ceiling = node.invocation_ceiling
        pins: list[ExperimentDecisionPin] = []
    else:
        if invocation_ceiling is None:
            raise ValueError("An active loop episode must retain its pinned invocation ceiling.")
        if invocations_used < 1:
            raise ValueError("An active loop episode must have at least one invocation.")
        if episode_decision_bundle is None:
            raise ValueError("An active loop episode must retain its pinned decision bundle.")
        ceiling = invocation_ceiling
        pins = list(episode_decision_bundle)
    if detached_work_active and invocations_used >= ceiling:
        reasons.append("Detached Experiment work is still running.")
    return ExperimentControlState(
        ready=not reasons,
        reasons=reasons,
        invocations_used=invocations_used,
        invocation_ceiling=ceiling,
        invocations_remaining=max(ceiling - invocations_used, 0),
        episode_id=episode_id,
        paused=paused,
        active=active,
        governing_decisions=governing,
        decision_drift=decision_drift(state, pins),
        operational=operational or ExperimentOperationalState(),
    )


def experiment_graph_precondition_reasons(state: GraphState, experiment_id: str) -> list[str]:
    """Return only graph facts that gate a new or automatic loop invocation."""

    node = state.nodes.get(experiment_id)
    if not isinstance(node, Experiment):
        raise ValueError(f"Node {experiment_id!r} is not an Experiment.")

    reasons: list[str] = []
    for decision_id in _related_targets(state, experiment_id, "governed_by"):
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
    return reasons


def admit_experiment_watcher_invocation(
    state: GraphState,
    experiment_id: str,
    *,
    episode_id: str | None,
    invocations_used: int,
    invocation_ceiling: int | None,
    decision_bundle: Iterable[ExperimentDecisionPin] | None,
    task_active: bool = False,
    episode_exited: bool = False,
    stop_requested: bool = False,
) -> ExperimentInvocationAdmission | None:
    """Admit the next automatic wake, or leave its watcher completion pending."""

    if episode_id is None:
        raise ValueError("An Experiment watcher cannot wake without an authorized episode.")
    if invocation_ceiling is None:
        raise ValueError("An Experiment watcher cannot wake without its pinned ceiling.")
    if invocations_used < 1:
        raise ValueError("An Experiment watcher cannot wake before invocation 1.")
    if decision_bundle is None:
        raise ValueError("An Experiment watcher cannot wake without pinned decisions.")
    if task_active or episode_exited or stop_requested:
        return None
    if invocations_used >= invocation_ceiling:
        return None
    if experiment_graph_precondition_reasons(state, experiment_id):
        return None
    return ExperimentInvocationAdmission(
        episode_id=episode_id,
        invocation=invocations_used + 1,
        invocation_ceiling=invocation_ceiling,
        decision_bundle=list(decision_bundle),
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
