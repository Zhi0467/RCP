from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any, Literal

from pydantic import ValidationError

from rcp.core.authority import (
    DECIDE_DECISION,
    EVIDENCE_EDGE_CAUSE_KIND,
    HYPOTHESIS_PROPOSAL_FIELDS,
)
from rcp.core.models import Decision, GraphState, Hypothesis, Patch, Proposal
from rcp.core.validation.constants import IDENTIFIER_RE
from rcp.core.validation.report import ValidationReport


def decision_transition_error(decision: Decision, changes: dict[str, Any]) -> str | None:
    """Return the authority-coherence error for a proposed Decision transition."""

    selected_option = changes.get("selected_option", decision.selected_option)
    status = changes.get("status", decision.status)
    if (
        changes.get("selected_option") is not None
        and changes["selected_option"] not in decision.options
    ):
        return f"Decision {decision.id} can select only an option listed in its current options."
    if status == "decided" and (selected_option is None or selected_option not in decision.options):
        return (
            f"Decision {decision.id} can be decided only with a selected option listed in its "
            "current options."
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


def normalized_decision_proposal_ops(state: GraphState, proposal: Proposal) -> list[dict[str, Any]]:
    """Add the one implied field accepted for legacy Decision Proposal approval."""

    operations = deepcopy(proposal.ops)
    for operation in operations:
        if operation.get("op") != "update_nodes":
            continue
        for update in operation.get("nodes", []):
            if not isinstance(update, dict):
                continue
            node = state.nodes.get(update.get("id"))
            changes = update.get("changes")
            if (
                isinstance(node, Decision)
                and isinstance(changes, dict)
                and changes.get("selected_option") is not None
                and "status" not in changes
                and node.status != "decided"
            ):
                changes["status"] = "decided"
    return operations


def proposal_updates_node(proposal: Proposal, node_id: str) -> bool:
    """Whether any semantic node update in a Proposal targets ``node_id``."""

    return any(
        update.get("id") == node_id
        for operation in proposal.ops
        if operation.get("op") == "update_nodes"
        for update in operation.get("nodes", [])
        if isinstance(update, dict)
    )


def proposal_is_stale(state: GraphState, proposal: Proposal) -> bool:
    """Whether the state a pending Proposal depends on has moved or disappeared."""

    dependency_revision = proposal.raised_rev or proposal.base_rev
    if any(
        node_id not in state.nodes or state.nodes[node_id].updated_rev > dependency_revision
        for node_id in proposal.related_node_ids
    ):
        return True
    if any(
        state.config_revisions.get(key, 0) > dependency_revision
        for key in proposal.related_config_keys
    ):
        return True

    edge_ids, decision_ids = _belief_cause_dependencies(proposal)
    cause_revision = proposal.raised_rev or proposal.base_rev
    if any(
        edge_id not in state.edges or state.edges[edge_id].created_rev > cause_revision
        for edge_id in edge_ids
    ):
        return True
    return any(
        not isinstance(state.nodes.get(decision_id), Decision)
        or state.nodes[decision_id].updated_rev > cause_revision
        for decision_id in decision_ids
    )


def _belief_cause_dependencies(proposal: Proposal) -> tuple[set[str], set[str]]:
    edge_ids: set[str] = set()
    decision_ids: set[str] = set()
    for op in proposal.ops:
        if op.get("op") != "update_nodes":
            continue
        for update in op.get("nodes", []):
            if not isinstance(update, dict):
                continue
            cause = update.get("cause")
            if not isinstance(cause, dict) or not isinstance(cause.get("ref_id"), str):
                continue
            if cause.get("kind") == "evidence_edge":
                edge_ids.add(cause["ref_id"])
            elif cause.get("kind") == "decision":
                decision_ids.add(cause["ref_id"])
    return edge_ids, decision_ids


def validate_proposal(
    raw: dict[str, Any],
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
) -> None:
    try:
        proposal = Proposal.model_validate(raw)
    except ValidationError as exc:
        report.reject(
            "invalid-proposal", f"Proposal is malformed: {exc.errors()[0]['msg']}.", revision
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
    if validation_mode == "admission":
        _validate_agent_proposal_boundary(
            proposal,
            state,
            report,
            revision,
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
    )


def _validate_agent_proposal_boundary(
    proposal: Proposal,
    state: GraphState,
    report: ValidationReport,
    revision: int | None,
) -> None:
    def refuse(message: str) -> None:
        report.reject("invalid-agent-proposal-shape", message, revision)

    if len(proposal.ops) != 1 or proposal.ops[0].get("op") != "update_nodes":
        refuse(f"Proposal {proposal.id} must contain exactly one Hypothesis status update.")
        return
    updates = proposal.ops[0].get("nodes")
    if not isinstance(updates, list) or len(updates) != 1 or not isinstance(updates[0], dict):
        refuse(f"Proposal {proposal.id} must update exactly one node in the staged graph.")
        return
    update = updates[0]
    node_id = update.get("id")
    node = state.nodes.get(node_id)
    changes = update.get("changes")
    if node is None:
        refuse(
            f"Proposal {proposal.id} must target a node already in the graph or created by an "
            "earlier operation in this Patch."
        )
        return
    if not isinstance(changes, dict) or not changes:
        refuse(f"Proposal {proposal.id} must contain an actual authority transition.")
        return
    if isinstance(node, Decision):
        refuse(
            f"Proposal {proposal.id} targets Decision {node.id!r}; new Decision Proposals are "
            f"not admitted, and Decision outcomes require the {DECIDE_DECISION} action."
        )
        return
    if isinstance(node, Hypothesis):
        if set(changes) != HYPOTHESIS_PROPOSAL_FIELDS or changes["status"] == node.status:
            refuse(f"Hypothesis Proposal {proposal.id} must change exactly the hypothesis status.")
            return
        cause = update.get("cause")
        if (
            not isinstance(cause, dict)
            or cause.get("kind") != EVIDENCE_EDGE_CAUSE_KIND
            or not isinstance(cause.get("ref_id"), str)
        ):
            refuse(
                f"Hypothesis Proposal {proposal.id} requires an evidence_edge cause naming a "
                "valid Evidence-to-Hypothesis epistemic edge."
            )
        return
    refuse(
        f"Proposal {proposal.id} targets {node_id!r}; agents may propose only Hypothesis status "
        "transitions."
    )


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
) -> None:
    # Imported lazily because the operation registry reaches this module.
    from rcp.core.validation.context import OpContext
    from rcp.core.validation.patch import _validate_operations

    control_ops = {
        str(op.get("op", ""))
        for op in proposal.ops
        if op.get("op")
        in {
            "create_proposals",
            "resolve_proposals",
            "withdraw_proposals",
            "set_standing",
        }
    }
    if control_ops:
        report.reject(
            "invalid-proposal-ops",
            f"Proposal {proposal.id} contains approval-control operations: "
            f"{', '.join(sorted(control_ops))}.",
            revision,
        )
        return

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
        ops=list(proposal.ops),
    )
    replay_report = ValidationReport()
    replay_context = OpContext(
        state=synthetic_state,
        initial_state=synthetic_state,
        patch=synthetic_patch,
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
    errors = [message.message for message in replay_report.messages if message.level == "reject"]
    if errors:
        report.reject(
            "invalid-proposal-ops",
            f"Proposal {proposal.id} contains invalid replay operations: {'; '.join(errors)}",
            revision,
        )
        return
