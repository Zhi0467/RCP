"""Backend-owned graph presentation projections."""

from __future__ import annotations

import json
from typing import Any

from rcp.core.models import Blocker, Decision, GraphState, ProjectNode, Proposal, Standing
from rcp.core.operations import (
    ProposalContentChangeOperation,
    ProposalMergeOperation,
    ProposalProtectedRelationOperation,
    ProposalRemovalOperation,
    ProposalStatusChangeOperation,
    ProposalSupersedeOperation,
)
from rcp.core.transition_models import (
    GraphAttentionProjection,
    GraphMutationAvailability,
    ProjectCountsProjection,
    ProposalActionLine,
)


def decision_awaits_choice(node: ProjectNode) -> bool:
    """Whether one canonical Decision belongs in human attention."""

    return isinstance(node, Decision) and node.status in {"ready", "revisit"}


def project_graph_attention(state: GraphState) -> GraphAttentionProjection:
    """Project the exact canonical memberships used by Inbox and Runs."""

    for node_id, node in state.nodes.items():
        if node_id != node.id:
            raise ValueError(
                f"Graph node mapping key {node_id!r} does not match embedded id {node.id!r}."
            )
    for proposal_id, proposal in state.proposals.items():
        if proposal_id != proposal.id:
            raise ValueError(
                "Graph Proposal mapping key "
                f"{proposal_id!r} does not match embedded id {proposal.id!r}."
            )
    pending_proposal_ids = sorted(
        proposal_id
        for proposal_id, proposal in state.proposals.items()
        if proposal.status == "pending"
    )
    return GraphAttentionProjection(
        pending_proposal_ids=pending_proposal_ids,
        decisions_awaiting_choice_ids=sorted(
            node.id for node in state.nodes.values() if decision_awaits_choice(node)
        ),
        open_blocker_ids=sorted(
            node.id
            for node in state.nodes.values()
            if isinstance(node, Blocker)
            and node.status == "open"
            and node.standing == Standing.ASSERTED
        ),
        proposal_actions={
            proposal_id: _proposal_action(state.proposals[proposal_id], state)
            for proposal_id in pending_proposal_ids
        },
    )


def project_primary_question(state: GraphState) -> ProjectNode | None:
    questions = [node for node in state.nodes.values() if node.type == "research_question"]
    questions.sort(
        key=lambda node: (
            {Standing.ACCEPTED: 0, Standing.ASSERTED: 1, Standing.CONTESTED: 2}[node.standing],
            node.id,
        )
    )
    return questions[0] if questions else None


def project_counts(
    state: GraphState,
    attention: GraphAttentionProjection | None = None,
) -> ProjectCountsProjection:
    attention = attention or project_graph_attention(state)
    return ProjectCountsProjection(
        pending_proposals=len(attention.pending_proposal_ids),
        decisions_awaiting_choice=len(attention.decisions_awaiting_choice_ids),
        open_blockers=len(attention.open_blocker_ids),
        asserted=sum(node.standing == Standing.ASSERTED for node in state.nodes.values()),
        accepted=sum(node.standing == Standing.ACCEPTED for node in state.nodes.values()),
        contested=sum(node.standing == Standing.CONTESTED for node in state.nodes.values()),
    )


def project_graph_mutation_availability(state: GraphState) -> GraphMutationAvailability:
    if state.replay_status != "degraded":
        return GraphMutationAvailability(available=True)
    failure = state.replay_failure
    if failure is None:
        reason = "Replay is degraded. This is the last coherent graph."
    else:
        reason = (
            f"Replay stopped at revision {failure.revision} ({failure.code}): "
            f"{failure.message} This is the last coherent graph."
        )
    return GraphMutationAvailability(available=False, reason=reason)


def _proposal_action(proposal: Proposal, state: GraphState) -> list[ProposalActionLine]:
    fallback = [
        ProposalActionLine(
            text=proposal.card.decision_needed or "Review the stored proposal action."
        )
    ]
    if len(proposal.ops) != 1:
        return fallback
    operation = proposal.ops[0]
    lines: list[ProposalActionLine] | None = None
    if isinstance(operation, ProposalContentChangeOperation):
        update = operation.nodes[0]
        node = state.nodes.get(update.id)
        if node is not None:
            lines = [ProposalActionLine(label="Node", text=node.title)]
            node_payload = node.model_dump(mode="json")
            for field, proposed in update.changes.items():
                label = _compact_label(field)
                lines.extend(
                    [
                        ProposalActionLine(
                            label=f"Current {label}", text=_display_value(node_payload.get(field))
                        ),
                        ProposalActionLine(
                            label=f"Proposed {label}", text=_display_value(proposed)
                        ),
                    ]
                )
    elif isinstance(operation, ProposalRemovalOperation):
        node_id = operation.node_ids[0]
        node = state.nodes.get(node_id)
        if node is not None:
            relations = [
                _relation_text(state, edge.source, edge.target, edge.relation)
                for edge in sorted(state.edges.values(), key=lambda edge: edge.id)
                if edge.source == node_id or edge.target == node_id
            ]
            if all(relations):
                lines = [ProposalActionLine(label="Remove", text=node.title)]
                lines.extend(
                    [ProposalActionLine(label="Also removes", text=text) for text in relations]
                    if relations
                    else [ProposalActionLine(label="Incident relations", text="None")]
                )
    elif isinstance(operation, ProposalSupersedeOperation):
        item = operation.nodes[0]
        before = state.nodes.get(item.id)
        after = state.nodes.get(item.superseded_by)
        if before is not None and after is not None:
            lines = [
                ProposalActionLine(label="Supersede", text=before.title),
                ProposalActionLine(label="With", text=after.title),
            ]
    elif isinstance(operation, ProposalMergeOperation):
        item = operation.merges[0]
        duplicate = state.nodes.get(item.duplicate)
        canonical = state.nodes.get(item.canonical)
        if duplicate is not None and canonical is not None:
            lines = [
                ProposalActionLine(label="Merge", text=duplicate.title),
                ProposalActionLine(label="Into", text=canonical.title),
            ]
    elif isinstance(operation, ProposalProtectedRelationOperation):
        if operation.op == "create_edges" and operation.edges:
            edge = operation.edges[0]
            text = _relation_text(state, edge.source, edge.target, edge.relation)
            if text:
                lines = [ProposalActionLine(label="Add relation", text=text)]
        elif operation.edge_ids:
            edge = state.edges.get(operation.edge_ids[0])
            if edge is not None:
                text = _relation_text(state, edge.source, edge.target, edge.relation)
                if text:
                    lines = [ProposalActionLine(label="Remove relation", text=text)]
    elif isinstance(operation, ProposalStatusChangeOperation):
        update = operation.nodes[0]
        node = state.nodes.get(update.id)
        proposed = update.changes.get("status")
        current = getattr(node, "status", None)
        if node is not None and isinstance(current, str) and isinstance(proposed, str):
            lines = [
                ProposalActionLine(label="Node", text=node.title),
                ProposalActionLine(label="Status", text=f"{current} → {proposed}"),
            ]
    return lines or fallback


def _relation_text(state: GraphState, source: str, target: str, relation: str) -> str | None:
    source_node = state.nodes.get(source)
    target_node = state.nodes.get(target)
    if source_node is None or target_node is None:
        return None
    return f"{source_node.title} — {_compact_label(relation)} → {target_node.title}"


def _compact_label(value: str) -> str:
    return value.replace("_", " ")


def _display_value(value: Any) -> str:
    if value is None:
        return "Not set"
    if isinstance(value, str):
        return f"“{value}”"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_display_value(item) for item in value)
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


__all__ = [
    "decision_awaits_choice",
    "project_counts",
    "project_graph_attention",
    "project_graph_mutation_availability",
    "project_primary_question",
]
