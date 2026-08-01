from __future__ import annotations

from typing import Any

from rcp.core.models import HUMAN_EDITABLE_NODE_FIELDS, GraphState, Patch
from rcp.core.validation.report import ValidationReport


def validate_approval_shape(state: GraphState, patch: Patch, report: ValidationReport) -> None:
    revision = patch.revision or None
    resolution_ops = [op for op in patch.ops if op.get("op") == "resolve_proposals"]
    if not resolution_ops:
        names = [op.get("op") for op in patch.ops]
        if len(patch.ops) == 1 and names[0] in {
            "set_standing",
            "resolve_ambiguities",
            "set_ontology",
        }:
            return
        if len(patch.ops) == 1 and names[0] == "create_nodes":
            nodes = patch.ops[0].get("nodes")
            if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
                report.reject(
                    "invalid-direct-node-create",
                    "A confirmed staged New node patch must create exactly one node.",
                    revision,
                )
                return
            node = nodes[0]
            if not isinstance(node.get("extension_type"), str):
                report.reject(
                    "invalid-direct-node-create",
                    "The standalone New-node path creates exactly one custom ontology node.",
                    revision,
                )
            if node.get("standing", "asserted") != "asserted" or node.get("source_refs", []):
                report.reject(
                    "invalid-direct-node-create",
                    "A human-created custom node starts asserted and cannot claim source records.",
                    revision,
                )
            return
        if names and set(names) <= {"update_nodes", "set_standing"}:
            update_ops = [op for op in patch.ops if op.get("op") == "update_nodes"]
            standing_ops = [op for op in patch.ops if op.get("op") == "set_standing"]
            if len(update_ops) != 1 or len(standing_ops) > 1:
                report.reject(
                    "invalid-standalone-review",
                    "One staged node patch may edit and review exactly one node.",
                    revision,
                )
                return
            _validate_direct_node_edit(state, update_ops[0], report, revision)
            edits = update_ops[0].get("nodes", [])
            if standing_ops and edits and standing_ops[0].get("node_id") != edits[0].get("id"):
                report.reject(
                    "invalid-standalone-review",
                    "A staged node edit and review must target the same node.",
                    revision,
                )
            return
        report.reject(
            "invalid-standalone-review",
            "A standalone human patch must review one node or edit one node's prose.",
            revision,
        )
        return

    if len(resolution_ops) != 1:
        report.reject(
            "invalid-proposal-resolution", "Resolve one proposal per approval patch.", revision
        )
        return
    resolutions = resolution_ops[0].get("resolutions", [])
    if len(resolutions) != 1:
        report.reject(
            "invalid-proposal-resolution", "Resolve one proposal per approval patch.", revision
        )
        return
    resolution = resolutions[0]
    proposal = state.proposals.get(resolution.get("id"))
    if proposal is None or proposal.status != "pending":
        report.reject("proposal-not-pending", "The referenced proposal is not pending.", revision)
        return
    stale_nodes = [
        node_id
        for node_id in proposal.related_node_ids
        if node_id not in state.nodes or state.nodes[node_id].updated_rev > proposal.base_rev
    ]
    stale_config = [
        key
        for key in proposal.related_config_keys
        if state.config_revisions.get(key, 0) > proposal.base_rev
    ]
    status = resolution.get("status")
    is_stale = bool(stale_nodes or stale_config)
    if is_stale and status != "withdrawn":
        report.reject(
            "stale-proposal",
            "The underlying node or project setting changed after this proposal was written.",
            revision,
        )
    semantic_ops = [
        op for op in patch.ops if op.get("op") not in {"resolve_proposals", "set_standing"}
    ]
    if status == "approved" and semantic_ops != proposal.ops:
        report.reject(
            "proposal-replay-mismatch",
            "Approval must replay the proposal's stored operations verbatim.",
            revision,
        )
    if status in {"rejected", "withdrawn"} and semantic_ops:
        report.reject(
            "rejected-proposal-has-ops",
            "Rejected or withdrawn proposals cannot apply semantic operations.",
            revision,
        )
    if status == "withdrawn" and not is_stale:
        report.reject(
            "invalid-stale-withdrawal",
            "The human UI may withdraw a proposal here only when its base state is stale.",
            revision,
        )
    elif status not in {"approved", "rejected", "withdrawn"}:
        report.reject(
            "invalid-human-resolution",
            "The human UI may approve or reject a pending proposal.",
            revision,
        )


def _validate_direct_node_edit(
    state: GraphState,
    operation: dict[str, Any],
    report: ValidationReport,
    revision: int | None,
) -> None:
    if set(operation) != {"op", "nodes"}:
        report.reject(
            "invalid-direct-node-edit",
            "A direct node edit operation may contain only 'op' and 'nodes'.",
            revision,
        )
    updates = operation.get("nodes", [])
    if not isinstance(updates, list) or len(updates) != 1 or not isinstance(updates[0], dict):
        report.reject(
            "invalid-direct-node-edit",
            "A direct node edit must update exactly one existing node.",
            revision,
        )
        return
    update = updates[0]
    if set(update) != {"id", "base_updated_rev", "changes"}:
        report.reject(
            "invalid-direct-node-edit",
            "A direct node edit requires exactly id, base_updated_rev, and changes.",
            revision,
        )
    node = state.nodes.get(update.get("id"))
    if node is None:
        report.reject(
            "unknown-node",
            f"Cannot update missing node {update.get('id')!r}.",
            revision,
        )
        return
    base_updated_rev = update.get("base_updated_rev")
    if (
        not isinstance(base_updated_rev, int)
        or isinstance(base_updated_rev, bool)
        or base_updated_rev != node.updated_rev
    ):
        report.reject(
            "stale-node-edit",
            f"{node.id} changed after this editor opened; reload it before saving.",
            revision,
        )
    changes = update.get("changes")
    if not isinstance(changes, dict) or not changes:
        report.reject(
            "empty-node-edit",
            "A direct node edit must change at least one prose field.",
            revision,
        )
        return
    allowed = HUMAN_EDITABLE_NODE_FIELDS[node.type] | {"extension_fields"}
    disallowed = sorted(set(changes) - allowed)
    if disallowed:
        report.reject(
            "non-prose-node-edit",
            f"Direct edits to {node.id} cannot change: {', '.join(disallowed)}.",
            revision,
        )
    if all(getattr(node, field, object()) == value for field, value in changes.items()):
        report.reject(
            "empty-node-edit",
            "The submitted node wording is unchanged.",
            revision,
        )
