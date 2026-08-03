"""One rule function per operation name.

Each ``validate_*`` checks a single operation and returns the oldest source
reference it cited (or ``None``); each ``depends_*`` reports the existing graph
and project-config objects that operation would touch. The registry pairs them
up — see :mod:`rcp.core.validation.registry`.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from rcp.core.models import (
    ACTIVE_EXPERIMENT_ATTEMPT_STATUSES,
    RELATION_SPEC,
    Decision,
    Edge,
    Experiment,
    GraphState,
    Hypothesis,
    Standing,
)
from rcp.core.ontology import (
    custom_relation,
    edge_matches_relation,
    parse_ontology_operation,
    validate_ontology_change,
)
from rcp.core.validation.constants import IMMUTABLE_NODE_UPDATE_FIELDS, NODE_ADAPTER
from rcp.core.validation.context import OpContext
from rcp.core.validation.nodes import (
    created_node_id,
    older,
    oldest_source_ref,
    requires_proposal,
    validate_extension_update,
    validate_new_node,
    validate_new_node_authoring,
    validate_updated_node_authoring,
)
from rcp.core.validation.proposals import validate_proposal


def validate_create_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    oldest = None
    for raw in op.get("nodes", []):
        validate_new_node(ctx.state, ctx.patch, raw, ctx.report)
        oldest = older(oldest, oldest_source_ref(raw, ctx.patch, ctx.report))
    return oldest


def author_create_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    for raw in op.get("nodes", []):
        validate_new_node_authoring(ctx.state, ctx.patch, raw, ctx.report)
    return None


def validate_update_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    oldest = None
    for update in op.get("nodes", []):
        node_id = update.get("id")
        node = ctx.state.nodes.get(node_id)
        if node is None:
            ctx.report.reject(
                "unknown-node", f"Cannot update missing node {node_id!r}.", ctx.revision
            )
            continue
        changes = update.get("changes", {})
        immutable = sorted(set(changes) & IMMUTABLE_NODE_UPDATE_FIELDS)
        if immutable:
            ctx.report.reject(
                "immutable-node-field",
                f"Update to {node_id} cannot change system fields: {', '.join(immutable)}.",
                ctx.revision,
            )
            continue
        candidate = node.model_dump(mode="python")
        candidate.update(changes)
        try:
            NODE_ADAPTER.validate_python(candidate)
        except ValidationError as exc:
            ctx.report.reject(
                "invalid-node-update",
                f"Update to {node_id} is invalid: {exc.errors()[0]['msg']}.",
                ctx.revision,
            )
        validate_extension_update(
            ctx.state,
            ctx.patch,
            node,
            changes,
            ctx.report,
            authoring=False,
        )
        is_control_update = (
            ctx.patch.kind == "experiment_loop"
            and node_id == ctx.experiment_control_node_id
            and set(changes) <= {"attempts", "status"}
        )
        if (
            ctx.patch.kind != "approval"
            and not is_control_update
            and requires_proposal(node, changes)
        ):
            ctx.report.reject(
                "gated-transition",
                f"Update to {node_id} requires a Proposal and human approval.",
                ctx.revision,
            )
        oldest = older(
            oldest,
            oldest_source_ref(
                {"source_refs": changes.get("source_refs", [])}, ctx.patch, ctx.report
            ),
        )
    return oldest


def author_update_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    for update in op.get("nodes", []):
        node = ctx.state.nodes.get(update.get("id"))
        changes = update.get("changes", {})
        if node is None or not isinstance(changes, dict):
            continue
        is_direct_human_edit = ctx.patch.kind == "approval" and not any(
            op.get("op") == "resolve_proposals" for op in ctx.patch.ops
        )
        if not is_direct_human_edit:
            validate_updated_node_authoring(node, changes, ctx.report, ctx.revision)
        validate_extension_update(
            ctx.state,
            ctx.patch,
            node,
            changes,
            ctx.report,
            authoring=True,
        )
        if (
            isinstance(node, Hypothesis)
            and "status" in changes
            and changes["status"] != node.status
        ):
            _validate_belief_cause(ctx, node.id, update.get("cause"))
    return None


def depends_update_nodes(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    return [update.get("id") for update in op.get("nodes", [])], []


def validate_create_edges(op: dict[str, Any], ctx: OpContext) -> Any:
    created_nodes = [
        raw
        for patch_op in ctx.patch.ops
        if patch_op.get("op") == "create_nodes"
        for raw in patch_op.get("nodes", [])
    ]
    for edge in op.get("edges", []):
        source_id = edge.get("source")
        target_id = edge.get("target")
        if source_id not in ctx.state.nodes and not created_node_id(ctx.patch, source_id):
            ctx.report.reject(
                "unknown-edge-source",
                f"Unknown edge source {source_id!r}.",
                ctx.revision,
            )
        if target_id not in ctx.state.nodes and not created_node_id(ctx.patch, target_id):
            ctx.report.reject(
                "unknown-edge-target",
                f"Unknown edge target {target_id!r}.",
                ctx.revision,
            )
        data = dict(edge)
        relation = data.get("relation")
        custom = custom_relation(ctx.state.ontology, relation)
        if relation not in RELATION_SPEC:
            if custom is None:
                ctx.report.reject(
                    "invalid-edge",
                    f"Edge {data.get('id')!r} uses unknown relation {relation!r}.",
                    ctx.revision,
                    related_node_ids=[
                        node_id for node_id in (source_id, target_id) if isinstance(node_id, str)
                    ],
                )
                continue
            else:
                data["layer"] = custom.layer
                if not edge_matches_relation(
                    ctx.state,
                    source_id,
                    target_id,
                    custom,
                    created_nodes=created_nodes,
                ):
                    ctx.report.reject(
                        "custom-relation-type-mismatch",
                        f"Relation {custom.name!r} does not allow the semantic endpoint types "
                        f"for {source_id!r} -> {target_id!r}.",
                        ctx.revision,
                        related_node_ids=[
                            node_id
                            for node_id in (source_id, target_id)
                            if isinstance(node_id, str)
                        ],
                    )
        if "id" not in data and source_id is not None and target_id is not None:
            data["id"] = f"{source_id}::{data.get('relation')}::{target_id}"
        try:
            Edge.model_validate(data)
        except ValidationError as exc:
            ctx.report.reject(
                "invalid-edge",
                f"Edge {data.get('id')!r} is invalid: {exc.errors()[0]['msg']}.",
                ctx.revision,
                related_node_ids=[
                    node_id for node_id in (source_id, target_id) if isinstance(node_id, str)
                ],
                related_edge_ids=[data["id"]] if isinstance(data.get("id"), str) else [],
            )
    return None


def author_create_edges(op: dict[str, Any], ctx: OpContext) -> Any:
    for raw in op.get("edges", []):
        relation = raw.get("relation")
        custom = custom_relation(ctx.state.ontology, relation)
        if custom is not None and custom.deprecated:
            ctx.report.reject(
                "deprecated-custom-relation",
                f"Custom relation {custom.name!r} is deprecated and cannot author new edges.",
                ctx.revision,
            )
        spec = RELATION_SPEC.get(relation)
        if spec is None:
            continue
        source_id = raw.get("source")
        target_id = raw.get("target")
        source_type = _node_type(ctx, source_id)
        target_type = _node_type(ctx, target_id)
        if source_type is None or target_type is None:
            continue
        type_mismatch = source_type not in spec.source_types or target_type not in spec.target_types
        same_type_mismatch = spec.same_type and source_type != target_type
        if not type_mismatch and not same_type_mismatch:
            continue
        edge_id = raw.get("id") or f"{source_id}::{relation}::{target_id}"
        allowed_sources = ", ".join(sorted(spec.source_types))
        allowed_targets = ", ".join(sorted(spec.target_types))
        same_type = "; source and target must have the same type" if spec.same_type else ""
        ctx.report.flag(
            "relation-type-mismatch",
            f"Edge {edge_id!r} uses {relation!r} from {source_type} to {target_type}; "
            f"allowed source types are [{allowed_sources}] and target types are "
            f"[{allowed_targets}]{same_type}.",
            ctx.revision,
            related_node_ids=[source_id, target_id],
            related_edge_ids=[edge_id],
        )
    return None


def depends_create_edges(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    candidates: list[Any] = []
    for edge in op.get("edges", []):
        candidates.append(edge.get("source"))
        candidates.append(edge.get("target"))
    return candidates, []


def validate_remove_edges(op: dict[str, Any], ctx: OpContext) -> Any:
    return None


def depends_remove_edges(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    candidates: list[Any] = []
    for edge_id in op.get("edge_ids", []):
        edge = state.edges.get(edge_id)
        if edge is not None:
            candidates.append(edge.source)
            candidates.append(edge.target)
    return candidates, []


def validate_remove_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    if set(op) != {"op", "node_ids"}:
        ctx.report.reject(
            "invalid-remove-nodes-operation",
            "A remove_nodes operation may contain only 'op' and 'node_ids'.",
            ctx.revision,
        )
        return None
    node_ids = op.get("node_ids")
    if (
        not isinstance(node_ids, list)
        or not node_ids
        or any(not isinstance(node_id, str) or not node_id for node_id in node_ids)
    ):
        ctx.report.reject(
            "invalid-remove-nodes-operation",
            "A remove_nodes operation requires at least one non-empty node id.",
            ctx.revision,
        )
        return None
    if len(node_ids) != len(set(node_ids)):
        ctx.report.reject(
            "invalid-remove-nodes-operation",
            "A remove_nodes operation cannot name the same node more than once.",
            ctx.revision,
        )
        return None

    for node_id in node_ids:
        node = ctx.state.nodes.get(node_id)
        if node is None:
            ctx.report.reject(
                "unknown-node",
                f"Cannot remove missing node {node_id!r}.",
                ctx.revision,
                related_node_ids=[node_id],
            )
            continue

        initial_node = ctx.initial_state.nodes.get(node_id, node)
        if initial_node.standing == Standing.ACCEPTED:
            ctx.report.reject(
                "accepted-node-removal",
                f"Cannot remove accepted node {node_id!r}; clear or contest it in an earlier "
                "human Sync first.",
                ctx.revision,
                related_node_ids=[node_id],
            )
        experiment_versions = (
            candidate for candidate in (initial_node, node) if isinstance(candidate, Experiment)
        )
        if any(
            attempt.status in ACTIVE_EXPERIMENT_ATTEMPT_STATUSES
            for experiment in experiment_versions
            for attempt in experiment.attempts
        ):
            ctx.report.reject(
                "active-experiment-removal",
                f"Cannot remove Experiment {node_id!r} while its bounded loop has an active "
                "attempt.",
                ctx.revision,
                related_node_ids=[node_id],
            )
    return None


def depends_remove_nodes(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    return list(op.get("node_ids", [])), []


def validate_supersede_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    for item in op.get("nodes", []):
        node_id = item.get("id")
        node = ctx.state.nodes.get(node_id)
        if node is None:
            ctx.report.reject(
                "unknown-node",
                f"Cannot supersede missing node {node_id!r}.",
                ctx.revision,
            )
        target_id = item.get("superseded_by")
        if (
            target_id
            and target_id not in ctx.state.nodes
            and not created_node_id(ctx.patch, target_id)
        ):
            ctx.report.reject(
                "unknown-node",
                f"Cannot supersede {node_id!r} with missing node {target_id!r}.",
                ctx.revision,
            )
    return None


def author_supersede_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    for item in op.get("nodes", []):
        node = ctx.state.nodes.get(item.get("id"))
        if isinstance(node, (Decision, Hypothesis)) and node.status != "superseded":
            if ctx.patch.kind != "approval":
                ctx.report.reject(
                    "gated-transition",
                    f"Superseding {node.type.replace('_', ' ').title()} {node.id} requires a "
                    "Proposal and human approval.",
                    ctx.revision,
                    related_node_ids=[node.id],
                )
                continue
            if isinstance(node, Hypothesis):
                _validate_belief_cause(ctx, node.id, item.get("cause"))
    return None


def depends_supersede_nodes(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    candidates: list[Any] = []
    for item in op.get("nodes", []):
        candidates.append(item.get("id"))
        candidates.append(item.get("superseded_by"))
    return candidates, []


def validate_merge_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    for item in op.get("merges", []):
        duplicate_id = item.get("duplicate")
        canonical_id = item.get("canonical")
        duplicate = ctx.state.nodes.get(duplicate_id)
        canonical = ctx.state.nodes.get(canonical_id)
        if duplicate is None:
            ctx.report.reject(
                "unknown-node",
                f"Cannot merge missing duplicate node {duplicate_id!r}.",
                ctx.revision,
            )
        if canonical is None:
            ctx.report.reject(
                "unknown-node",
                f"Cannot merge into missing canonical node {canonical_id!r}.",
                ctx.revision,
            )
    return None


def author_merge_nodes(op: dict[str, Any], ctx: OpContext) -> Any:
    for item in op.get("merges", []):
        duplicate = ctx.state.nodes.get(item.get("duplicate"))
        if isinstance(duplicate, (Decision, Hypothesis)) and duplicate.status != "superseded":
            if ctx.patch.kind != "approval":
                ctx.report.reject(
                    "gated-transition",
                    f"Merging {duplicate.type.replace('_', ' ').title()} {duplicate.id} requires "
                    "a Proposal and human approval.",
                    ctx.revision,
                    related_node_ids=[duplicate.id],
                )
                continue
            if isinstance(duplicate, Hypothesis):
                _validate_belief_cause(ctx, duplicate.id, item.get("cause"))
    return None


def depends_merge_nodes(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    candidates: list[Any] = []
    for item in op.get("merges", []):
        candidates.append(item.get("duplicate"))
        candidates.append(item.get("canonical"))
    return candidates, []


def depends_create_ambiguities(
    op: dict[str, Any], state: GraphState
) -> tuple[list[Any], list[str]]:
    candidates: list[Any] = []
    for ambiguity in op.get("ambiguities", []):
        candidates.extend(ambiguity.get("related_node_ids", []))
    return candidates, []


def validate_resolve_ambiguities(op: dict[str, Any], ctx: OpContext) -> Any:
    for resolution in op.get("resolutions", []):
        ambiguity_id = resolution.get("id")
        if ambiguity_id not in ctx.state.ambiguities:
            ctx.report.reject(
                "unknown-ambiguity",
                f"Cannot resolve missing ambiguity {ambiguity_id!r}.",
                ctx.revision,
            )
        if resolution.get("status") not in {"resolved", "dismissed"}:
            ctx.report.reject(
                "invalid-ambiguity-resolution",
                "Ambiguities may only be resolved or dismissed.",
                ctx.revision,
            )
    return None


def depends_resolve_ambiguities(
    op: dict[str, Any], state: GraphState
) -> tuple[list[Any], list[str]]:
    candidates: list[Any] = []
    for resolution in op.get("resolutions", []):
        ambiguity = state.ambiguities.get(resolution.get("id"))
        if ambiguity is not None:
            candidates.extend(ambiguity.related_node_ids)
    return candidates, []


def validate_create_proposals(op: dict[str, Any], ctx: OpContext) -> Any:
    for raw in op.get("proposals", []):
        validate_proposal(
            raw,
            ctx.state,
            ctx.report,
            ctx.revision,
            project_truth_scope=ctx.project_truth_scope,
            repository_aliases=ctx.repositories,
            machine_aliases=ctx.machines,
            default_run_truth_scope=ctx.default_run_truth_scope,
            state_repository=ctx.state_repository,
            context_patch=ctx.patch,
        )
    return None


def author_create_proposals(op: dict[str, Any], ctx: OpContext) -> Any:
    for raw in op.get("proposals", []):
        validate_proposal(
            raw,
            ctx.state,
            ctx.report,
            ctx.revision,
            project_truth_scope=ctx.project_truth_scope,
            repository_aliases=ctx.repositories,
            machine_aliases=ctx.machines,
            default_run_truth_scope=ctx.default_run_truth_scope,
            state_repository=ctx.state_repository,
            validation_mode="admission",
            include_card_flags=True,
            context_patch=ctx.patch,
        )
    return None


def validate_resolve_proposals(op: dict[str, Any], ctx: OpContext) -> Any:
    if ctx.mode == "admission" and ctx.patch.kind != "approval":
        ctx.report.reject(
            "agent-resolved-proposal",
            "Only a human approval patch may resolve or withdraw a Proposal.",
            ctx.revision,
        )
    for resolution in op.get("resolutions", []):
        proposal_id = resolution.get("id")
        proposal = ctx.state.proposals.get(proposal_id)
        if proposal is None:
            ctx.report.reject(
                "unknown-proposal",
                f"Cannot resolve missing proposal {proposal_id!r}.",
                ctx.revision,
            )
        elif proposal.status != "pending":
            ctx.report.reject(
                "proposal-not-pending",
                f"Proposal {proposal_id!r} is not pending.",
                ctx.revision,
            )
    return None


def validate_set_standing(op: dict[str, Any], ctx: OpContext) -> Any:
    if ctx.patch.kind != "approval":
        ctx.report.reject("agent-set-standing", "Only the human UI may set standing.", ctx.revision)
    node_id = op.get("node_id")
    if node_id not in ctx.state.nodes:
        ctx.report.reject("unknown-node", f"Cannot review missing node {node_id!r}.", ctx.revision)
    if op.get("standing") not in {"asserted", "accepted", "contested"}:
        ctx.report.reject(
            "invalid-standing",
            "Human review may set a node asserted, accepted, or contested.",
            ctx.revision,
        )
    return None


def validate_set_project_truth_scope(op: dict[str, Any], ctx: OpContext) -> Any:
    if ctx.patch.kind != "approval":
        ctx.report.reject(
            "agent-set-project-scope",
            "Project truth-scope membership requires human approval.",
            ctx.revision,
        )
    proposed = set(op.get("truth_scope", []))
    descriptor = op.get("repository")
    if descriptor:
        alias = descriptor.get("alias")
        machine = descriptor.get("machine")
        if not alias or not machine or not descriptor.get("path"):
            ctx.report.reject(
                "incomplete-repository",
                "A new repository descriptor needs alias, machine, and path.",
                ctx.revision,
            )
        else:
            if ctx.machines is not None and machine not in ctx.machines:
                ctx.report.reject(
                    "unknown-repository-machine",
                    f"Repository {alias!r} uses unknown machine {machine!r}.",
                    ctx.revision,
                )
            if alias not in ctx.repositories:
                ctx.repositories.add(alias)
    unknown = proposed - ctx.repositories
    if unknown:
        ctx.report.reject(
            "unknown-project-repository",
            f"Project truth scope names unknown repositories: {sorted(unknown)}.",
            ctx.revision,
        )
    if ctx.state_repository and ctx.state_repository not in proposed:
        ctx.report.reject(
            "remove-state-repository",
            "The canonical state repository must remain in project truth scope in v1.",
            ctx.revision,
        )
    removed_defaults = ctx.default_run_truth_scope - proposed
    if removed_defaults:
        ctx.report.reject(
            "remove-default-run-repository",
            "Project truth scope must retain every repository in the default run scope: "
            f"{sorted(removed_defaults)}.",
            ctx.revision,
        )
    return None


def depends_set_project_truth_scope(
    op: dict[str, Any], state: GraphState
) -> tuple[list[Any], list[str]]:
    return [], ["project_truth_scope"]


def validate_set_ontology(op: dict[str, Any], ctx: OpContext) -> Any:
    ontology = parse_ontology_operation(op, ctx.report, ctx.revision)
    if ctx.patch.kind != "approval":
        ctx.report.reject(
            "agent-set-ontology",
            "Only the human Settings and Sync paths may change ontology.",
            ctx.revision,
        )
    if ontology is not None and ctx.mode == "admission":
        validate_ontology_change(ctx.state, ontology, ctx.report, ctx.revision)
    return None


def depends_set_ontology(op: dict[str, Any], state: GraphState) -> tuple[list[Any], list[str]]:
    return [], ["ontology"]


def _validate_belief_cause(ctx: OpContext, hypothesis_id: str, raw: Any) -> None:
    related = [hypothesis_id]
    if not isinstance(raw, dict):
        ctx.report.reject(
            "missing-belief-cause",
            f"Changing Hypothesis {hypothesis_id!r} status requires a cause object.",
            ctx.revision,
            related_node_ids=related,
        )
        return
    kind = raw.get("kind")
    expected_keys = {"kind"} if kind == "human_edit" else {"kind", "ref_id"}
    if kind not in {"evidence_edge", "decision", "proposal_resolution", "human_edit"}:
        ctx.report.reject(
            "invalid-belief-cause",
            f"Hypothesis {hypothesis_id!r} has unknown belief cause kind {kind!r}.",
            ctx.revision,
            related_node_ids=related,
        )
        return
    if set(raw) != expected_keys or (
        "ref_id" in expected_keys and not isinstance(raw.get("ref_id"), str)
    ):
        required = "kind only" if kind == "human_edit" else "exactly kind and string ref_id"
        ctx.report.reject(
            "invalid-belief-cause",
            f"Belief cause {kind!r} for {hypothesis_id!r} requires {required}.",
            ctx.revision,
            related_node_ids=related,
        )
        return

    ref_id = raw.get("ref_id")
    if kind == "human_edit":
        if ctx.patch.kind != "approval" or ctx.patch.author != "human":
            ctx.report.reject(
                "invalid-belief-cause",
                f"human_edit cause for {hypothesis_id!r} is legal only on human approval.",
                ctx.revision,
                related_node_ids=related,
            )
        return
    if kind == "decision":
        node_type = _node_type(ctx, ref_id)
        if node_type != "decision":
            ctx.report.reject(
                "invalid-belief-cause",
                f"Decision cause {ref_id!r} for {hypothesis_id!r} does not name a Decision.",
                ctx.revision,
                related_node_ids=[hypothesis_id, ref_id],
            )
        return
    if kind == "proposal_resolution":
        resolved = {
            item.get("id")
            for op in ctx.patch.ops
            if op.get("op") == "resolve_proposals"
            for item in op.get("resolutions", [])
        }
        if ref_id not in resolved:
            ctx.report.reject(
                "invalid-belief-cause",
                f"Proposal-resolution cause {ref_id!r} for {hypothesis_id!r} is not resolved in this patch.",
                ctx.revision,
                related_node_ids=related,
            )
        return

    edge = _edge_in_context(ctx, ref_id)
    if (
        edge is None
        or edge.target != hypothesis_id
        or edge.relation not in {"supports", "weakens", "refutes", "inconclusive", "contradicts"}
        or _node_type(ctx, edge.source) != "evidence"
    ):
        ctx.report.reject(
            "invalid-belief-cause",
            f"Evidence-edge cause {ref_id!r} must be an evidence relation targeting {hypothesis_id!r}.",
            ctx.revision,
            related_node_ids=related,
            related_edge_ids=[ref_id] if isinstance(ref_id, str) else [],
        )


def _node_type(ctx: OpContext, node_id: Any) -> str | None:
    existing = ctx.state.nodes.get(node_id)
    if existing is not None:
        return existing.type
    for patch in (ctx.patch, ctx.reference_patch):
        if patch is None:
            continue
        for op in patch.ops:
            if op.get("op") != "create_nodes":
                continue
            for raw in op.get("nodes", []):
                if raw.get("id") == node_id:
                    node_type = raw.get("type")
                    return node_type if isinstance(node_type, str) else None
    return None


def _edge_in_context(ctx: OpContext, edge_id: Any) -> Edge | None:
    existing = ctx.state.edges.get(edge_id)
    if existing is not None:
        return existing
    for patch in (ctx.patch, ctx.reference_patch):
        if patch is None:
            continue
        for op in patch.ops:
            if op.get("op") != "create_edges":
                continue
            for raw in op.get("edges", []):
                candidate_id = raw.get("id") or (
                    f"{raw.get('source')}::{raw.get('relation')}::{raw.get('target')}"
                )
                if candidate_id != edge_id:
                    continue
                data = dict(raw)
                data["id"] = candidate_id
                try:
                    return Edge.model_validate(data)
                except ValidationError:
                    return None
    return None
