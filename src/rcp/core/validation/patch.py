"""Patch-level validation: the rules that judge a patch as a whole.

Operation-level rules live in :mod:`rcp.core.validation.ops` and are reached
only through :data:`rcp.core.validation.registry.OP_RULES`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from rcp.core.authority import operation_actions, permits
from rcp.core.models import Decision, ExperimentDecisionPin, GraphState, Patch
from rcp.core.validation.approval import validate_approval_shape
from rcp.core.validation.context import OpContext
from rcp.core.validation.experiment_loop import validate_experiment_loop_authority
from rcp.core.validation.nodes import older
from rcp.core.validation.proposals import proposal_is_stale
from rcp.core.validation.registry import OP_RULES
from rcp.core.validation.report import ValidationReport


def validate_patch(
    state: GraphState,
    patch: Patch,
    project_truth_scope: Iterable[str],
    repository_aliases: Iterable[str] | None = None,
    machine_aliases: Iterable[str] | None = None,
    default_run_truth_scope: Iterable[str] | None = None,
    state_repository: str | None = None,
    mode: Literal["admission", "replay"] = "admission",
    *,
    experiment_control_node_id: str | None = None,
    experiment_decision_bundle: Iterable[ExperimentDecisionPin] | None = None,
) -> ValidationReport:
    try:
        return _validate_patch(
            state,
            patch,
            project_truth_scope,
            repository_aliases=repository_aliases,
            machine_aliases=machine_aliases,
            default_run_truth_scope=default_run_truth_scope,
            state_repository=state_repository,
            mode=mode,
            experiment_control_node_id=experiment_control_node_id,
            experiment_decision_bundle=experiment_decision_bundle,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        report = ValidationReport()
        report.reject(
            "malformed-operation",
            f"Patch operations are malformed: {exc}.",
            patch.revision or None,
        )
        return report


def _validate_patch(
    state: GraphState,
    patch: Patch,
    project_truth_scope: Iterable[str],
    repository_aliases: Iterable[str] | None = None,
    machine_aliases: Iterable[str] | None = None,
    default_run_truth_scope: Iterable[str] | None = None,
    state_repository: str | None = None,
    mode: Literal["admission", "replay"] = "admission",
    *,
    experiment_control_node_id: str | None = None,
    experiment_decision_bundle: Iterable[ExperimentDecisionPin] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    scope = set(project_truth_scope)
    control_node_id = experiment_control_node_id or patch.experiment_control_node_id
    decision_bundle = tuple(
        experiment_decision_bundle
        if experiment_decision_bundle is not None
        else patch.experiment_decision_bundle
    )
    ctx = OpContext(
        state=state,
        initial_state=state,
        patch=patch,
        report=report,
        revision=patch.revision or None,
        project_truth_scope=scope,
        repositories=set(repository_aliases or scope),
        machines=set(machine_aliases) if machine_aliases is not None else None,
        default_run_truth_scope=set(default_run_truth_scope or ()),
        state_repository=state_repository,
        mode=mode,
        experiment_control_node_id=control_node_id,
    )

    if patch.revision and patch.revision != state.revision + 1:
        report.reject(
            "non-monotonic-revision",
            f"Patch revision {patch.revision} must follow graph revision {state.revision}.",
            ctx.revision,
        )
    _validate_authorship(ctx)
    _validate_identity_shape(ctx)
    _validate_attribution_shape(ctx)
    _validate_declared_agent_action(ctx)
    _validate_declared_scope(ctx)

    if patch.kind == "experiment_loop":
        validate_experiment_loop_authority(
            state,
            patch,
            report,
            control_node_id=control_node_id,
            decision_bundle=decision_bundle,
        )
    elif control_node_id or decision_bundle:
        report.reject(
            "unexpected-experiment-control",
            "Experiment control metadata is legal only on experiment-loop patches.",
            ctx.revision,
        )

    op_names = [str(op.get("op", "")) for op in patch.ops]
    if any(name.startswith("delete") for name in op_names):
        report.reject("delete-forbidden", "Graph objects are never deleted.", ctx.revision)

    if patch.kind == "approval":
        validate_approval_shape(state, patch, report, mode=mode)

    oldest_ref = _validate_operations(ctx)
    _validate_queued_decision_options(ctx)
    _validate_created_proposal_liveness(ctx)

    if (
        mode == "admission"
        and oldest_ref is not None
        and state.coverage.earliest_timestamp is not None
        and oldest_ref < state.coverage.earliest_timestamp
        and "set_coverage" not in op_names
    ):
        report.flag(
            "coverage-mismatch",
            "This patch cites history older than the graph's coverage boundary without updating coverage.",
            ctx.revision,
        )

    return report


def _validate_created_proposal_liveness(ctx: OpContext) -> None:
    proposal_positions = {
        raw.get("id"): index
        for index, operation in enumerate(ctx.patch.ops)
        if operation.get("op") == "create_proposals"
        for raw in operation.get("proposals", [])
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    for proposal_id, position in sorted(proposal_positions.items()):
        proposal = ctx.state.proposals.get(proposal_id)
        moved_nodes: set[str] = set()
        moved_edges: set[str] = set()
        moved_config: set[str] = set()
        if ctx.mode == "admission" and proposal is not None:
            moved_nodes, moved_edges, moved_config = _later_dependency_mutations(
                ctx,
                position,
                proposal.related_node_ids,
                proposal.related_edge_ids,
                proposal.related_config_keys,
            )
        if moved_nodes or moved_edges or moved_config:
            moved = sorted(moved_nodes | moved_edges | moved_config)
            ctx.report.reject(
                "stale-created-proposal",
                f"Proposal {proposal_id!r} is already stale because a snapshotted dependency "
                f"moved later in its outer patch: {', '.join(moved)}.",
                ctx.revision,
                related_node_ids=sorted(moved_nodes),
                related_edge_ids=sorted(moved_edges),
            )
            continue
        if proposal is not None and proposal_is_stale(ctx.state, proposal):
            ctx.report.reject(
                "stale-created-proposal",
                f"Proposal {proposal_id!r} is already stale after applying its outer patch.",
                ctx.revision,
                related_node_ids=list(proposal.related_node_ids),
            )


def _later_dependency_mutations(
    ctx: OpContext,
    position: int,
    related_node_ids: Iterable[str],
    related_edge_ids: Iterable[str],
    related_config_keys: Iterable[str],
) -> tuple[set[str], set[str], set[str]]:
    related_nodes = set(related_node_ids)
    related_edges = set(related_edge_ids)
    related_config = set(related_config_keys)
    present_nodes = set(ctx.initial_state.nodes)
    present_edges = {
        edge.id: (edge.source, edge.target) for edge in ctx.initial_state.edges.values()
    }
    for operation in ctx.patch.ops[:position]:
        _update_resource_presence(present_nodes, present_edges, operation)

    later = ctx.patch.ops[position + 1 :]
    created_nodes = {
        raw.get("id")
        for operation in later
        if operation.get("op") == "create_nodes"
        for raw in operation.get("nodes", [])
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    changed_nodes = {
        raw.get("id")
        for operation in later
        if operation.get("op") in {"update_nodes", "supersede_nodes"}
        for raw in operation.get("nodes", [])
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    changed_nodes.update(
        raw.get("duplicate")
        for operation in later
        if operation.get("op") == "merge_nodes"
        for raw in operation.get("merges", [])
        if isinstance(raw, dict) and isinstance(raw.get("duplicate"), str)
    )
    changed_nodes.update(
        node_id
        for operation in later
        if operation.get("op") == "remove_nodes"
        for node_id in operation.get("node_ids", [])
        if isinstance(node_id, str)
    )
    changed_nodes.update(
        operation.get("node_id")
        for operation in later
        if operation.get("op") == "set_standing" and isinstance(operation.get("node_id"), str)
    )
    created_edges = {edge_id for operation in later for edge_id in _created_edges(operation)}
    removed_edges = {
        edge_id
        for operation in later
        if operation.get("op") == "remove_edges"
        for edge_id in operation.get("edge_ids", [])
        if isinstance(edge_id, str)
    }
    moved_nodes = related_nodes & (changed_nodes | (created_nodes & present_nodes))
    moved_edges = related_edges & (removed_edges | (created_edges & set(present_edges)))
    moved_config: set[str] = set()
    if "ontology" in related_config and any(op.get("op") == "set_ontology" for op in later):
        moved_config.add("ontology")
    if "project_truth_scope" in related_config and any(
        op.get("op") == "set_project_truth_scope" for op in later
    ):
        moved_config.add("project_truth_scope")
    return moved_nodes, moved_edges, moved_config


def _update_resource_presence(
    nodes: set[str], edges: dict[str, tuple[str, str]], operation: dict
) -> None:
    name = operation.get("op")
    if name == "create_nodes":
        nodes.update(
            raw["id"]
            for raw in operation.get("nodes", [])
            if isinstance(raw, dict) and isinstance(raw.get("id"), str)
        )
    elif name == "remove_nodes":
        removed = {node_id for node_id in operation.get("node_ids", []) if isinstance(node_id, str)}
        nodes.difference_update(removed)
        for edge_id, endpoints in list(edges.items()):
            if any(node_id in removed for node_id in endpoints):
                edges.pop(edge_id)
    elif name == "remove_edges":
        for edge_id in operation.get("edge_ids", []):
            if isinstance(edge_id, str):
                edges.pop(edge_id, None)
    edges.update(_created_edges(operation))


def _created_edges(operation: dict) -> dict[str, tuple[str, str]]:
    created: dict[str, tuple[str, str]] = {}
    name = operation.get("op")
    if name == "create_edges":
        raw_edges = operation.get("edges", [])
        relation_key = "relation"
        source_key = "source"
        target_key = "target"
    elif name == "supersede_nodes":
        raw_edges = operation.get("nodes", [])
        relation_key = None
        source_key = "id"
        target_key = "superseded_by"
    elif name == "merge_nodes":
        raw_edges = operation.get("merges", [])
        relation_key = None
        source_key = "duplicate"
        target_key = "canonical"
    else:
        return created
    for raw in raw_edges:
        if not isinstance(raw, dict):
            continue
        source = raw.get(source_key)
        target = raw.get(target_key)
        relation = (
            raw.get(relation_key)
            if relation_key
            else ("supersedes" if name == "supersede_nodes" else "duplicate_of")
        )
        if not all(isinstance(value, str) for value in (source, relation, target)):
            continue
        edge_id = raw.get("id") if name == "create_edges" else None
        if not isinstance(edge_id, str):
            edge_id = f"{source}::{relation}::{target}"
        created[edge_id] = (source, target)
    return created


def _validate_queued_decision_options(ctx: OpContext) -> None:
    """Check queued ballots after the Patch's written-order staging has finished."""

    if ctx.mode != "admission":
        return
    touched_ids = {
        raw.get("id")
        for operation in ctx.patch.ops
        if operation.get("op") in {"create_nodes", "update_nodes", "supersede_nodes"}
        for raw in operation.get("nodes", [])
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    touched_ids.update(
        raw.get("duplicate")
        for operation in ctx.patch.ops
        if operation.get("op") == "merge_nodes"
        for raw in operation.get("merges", [])
        if isinstance(raw, dict) and isinstance(raw.get("duplicate"), str)
    )
    touched_ids.update(
        operation.get("node_id")
        for operation in ctx.patch.ops
        if operation.get("op") == "set_standing" and isinstance(operation.get("node_id"), str)
    )
    for node_id in sorted(touched_ids):
        node = ctx.state.nodes.get(node_id)
        if (
            isinstance(node, Decision)
            and node.status in {"ready", "revisit"}
            and len(set(node.options)) < 2
        ):
            ctx.report.reject(
                "incomplete-decision-ballot",
                f"Decision {node.id} must have at least two distinct options before it can be "
                f"queued as {node.status}.",
                ctx.revision,
                related_node_ids=[node.id],
            )


def _validate_authorship(ctx: OpContext) -> None:
    if ctx.patch.kind == "identity":
        if ctx.patch.producer != "system":
            ctx.report.reject(
                "wrong-producer",
                "Identity patches must be produced by RCP's system producer.",
                ctx.revision,
            )
        if ctx.patch.author is not None:
            ctx.report.reject(
                "wrong-author",
                "Identity patches have no human or agent author.",
                ctx.revision,
            )
        return

    expected_author = "human" if ctx.patch.kind == "approval" else "agent"
    if ctx.patch.author != expected_author:
        ctx.report.reject(
            "wrong-author",
            f"{ctx.patch.kind} patches must be authored by {expected_author}.",
            ctx.revision,
        )
    if ctx.patch.producer == "system":
        ctx.report.reject(
            "system-producer-forbidden",
            "The system producer is reserved for identity patches.",
            ctx.revision,
        )
    elif ctx.patch.producer != ctx.patch.author:
        ctx.report.reject(
            "producer-author-mismatch",
            "Human and agent patches must retain the same producer and legacy author role.",
            ctx.revision,
        )


def _validate_identity_shape(ctx: OpContext) -> None:
    patch = ctx.patch
    if patch.kind != "identity":
        if patch.project_identity is not None:
            ctx.report.reject(
                "unexpected-project-identity",
                "Project identity is legal only on identity patches.",
                ctx.revision,
            )
        return

    if patch.project_identity is None:
        ctx.report.reject(
            "missing-project-identity",
            "Identity patches require exactly one project identity payload.",
            ctx.revision,
        )
    if patch.ops:
        ctx.report.reject(
            "identity-has-operations",
            "Identity patches cannot carry graph operations.",
            ctx.revision,
        )
    if patch.run_truth_scope or patch.repositories_read:
        ctx.report.reject(
            "identity-has-run-scope",
            "Identity patches cannot carry raw repository scope.",
            ctx.revision,
        )
    if patch.processed_cursors:
        ctx.report.reject(
            "identity-has-cursors",
            "Identity patches cannot carry coverage cursors.",
            ctx.revision,
        )
    if (
        patch.source_operation_id is not None
        or patch.source_effect_id is not None
        or patch.source_effect_sha256 is not None
    ):
        ctx.report.reject(
            "identity-has-operation-id",
            "Identity patches cannot carry an operation or effect id.",
            ctx.revision,
        )
    if patch.human_action is not None:
        ctx.report.reject(
            "identity-has-human-action",
            "Identity patches cannot carry a human authority action.",
            ctx.revision,
        )
    if patch.agent_action is not None:
        ctx.report.reject(
            "identity-has-agent-action",
            "Identity patches cannot carry an agent authority action.",
            ctx.revision,
        )
    if patch.experiment_control_node_id is not None or patch.experiment_decision_bundle:
        ctx.report.reject(
            "identity-has-experiment-control",
            "Identity patches cannot carry experiment control metadata.",
            ctx.revision,
        )
    if patch.authorized_by is not None or patch.profile is not None or patch.task_id is not None:
        ctx.report.reject(
            "identity-has-attribution",
            "Identity patches cannot carry human or task attribution.",
            ctx.revision,
        )


def _validate_attribution_shape(ctx: OpContext) -> None:
    patch = ctx.patch
    if patch.kind == "identity":
        return
    if (patch.source_effect_id is None) != (patch.source_effect_sha256 is None):
        ctx.report.reject(
            "invalid-source-effect",
            "A source effect id and its exact Patch digest must be present together.",
            ctx.revision,
        )
    if patch.source_effect_id is not None and (
        patch.author != "agent" or not patch.source_operation_id
    ):
        ctx.report.reject(
            "invalid-source-effect",
            "A source effect id requires one direct agent task source operation.",
            ctx.revision,
        )
    has_attribution = (
        patch.authorized_by is not None or patch.profile is not None or patch.task_id is not None
    )
    if not has_attribution:
        return

    if patch.kind == "approval":
        if patch.authorized_by is None or patch.profile is not None or patch.task_id is not None:
            ctx.report.reject(
                "invalid-human-attribution",
                "An attributed human approval requires authorized_by and cannot carry an "
                "agent profile or task id.",
                ctx.revision,
            )
        return

    if (
        patch.authorized_by is None
        or patch.profile not in {"ordinary", "orchestrator"}
        or not patch.task_id
    ):
        ctx.report.reject(
            "invalid-agent-attribution",
            "An attributed agent patch requires authorized_by, one known agent profile, and a "
            "non-empty direct task id.",
            ctx.revision,
        )


def _validate_declared_agent_action(ctx: OpContext) -> None:
    patch = ctx.patch
    has_decision_outcome = _has_declared_decision_outcome(ctx)
    if patch.human_action is not None and patch.agent_action is not None:
        ctx.report.reject(
            "conflicting-authority-actions",
            "A Patch cannot declare both a human and an agent authority action.",
            ctx.revision,
        )
        return
    if patch.human_action is not None and (patch.kind != "approval" or patch.author != "human"):
        ctx.report.reject(
            "invalid-human-action",
            "Only a human approval Patch may declare a human authority action.",
            ctx.revision,
        )
    if (
        patch.author == "agent"
        and patch.profile == "orchestrator"
        and has_decision_outcome
        and patch.agent_action != "decision_choice"
    ):
        ctx.report.reject(
            "missing-decision-action",
            "An orchestrator Decision outcome requires agent_action='decision_choice'.",
            ctx.revision,
        )
    if patch.agent_action is None:
        return
    if patch.author != "agent" or patch.profile != "orchestrator":
        ctx.report.reject(
            "invalid-agent-action",
            "Only the orchestrator profile may declare an agent authority action.",
            ctx.revision,
        )
        return
    if not has_decision_outcome:
        ctx.report.reject(
            "unused-agent-action",
            "agent_action='decision_choice' must name a real Decision outcome in this Patch.",
            ctx.revision,
        )


def _has_declared_decision_outcome(ctx: OpContext) -> bool:
    decision_ids = {
        node.id for node in ctx.initial_state.nodes.values() if isinstance(node, Decision)
    }
    decision_ids.update(
        raw.get("id")
        for operation in ctx.patch.ops
        if operation.get("op") == "create_nodes"
        for raw in operation.get("nodes", [])
        if isinstance(raw, dict)
        and raw.get("type") == "decision"
        and isinstance(raw.get("id"), str)
    )
    return any(
        isinstance(update, dict)
        and update.get("id") in decision_ids
        and isinstance(update.get("changes"), dict)
        and (
            update["changes"].get("status") == "decided"
            or update["changes"].get("selected_option") is not None
        )
        for operation in ctx.patch.ops
        if operation.get("op") == "update_nodes"
        for update in operation.get("nodes", [])
    )


def _validate_declared_scope(ctx: OpContext) -> None:
    patch = ctx.patch
    if patch.kind == "identity":
        return
    if patch.kind == "approval":
        if patch.run_truth_scope or patch.repositories_read:
            ctx.report.reject(
                "approval-has-run-scope",
                "Human approval patches cannot carry raw repository scope.",
                ctx.revision,
            )
        return

    run_scope = set(patch.run_truth_scope)
    if not run_scope:
        ctx.report.reject(
            "empty-run-scope", "Agent patches require a non-empty run truth scope.", ctx.revision
        )
    outside = run_scope - ctx.project_truth_scope
    if outside:
        ctx.report.reject(
            "run-scope-outside-project",
            f"Run scope contains repositories outside project truth scope: {sorted(outside)}.",
            ctx.revision,
        )
    read_outside = set(patch.repositories_read) - run_scope
    if read_outside:
        ctx.report.reject(
            "read-outside-run-scope",
            f"Patch read repositories outside its run scope: {sorted(read_outside)}.",
            ctx.revision,
        )


def _validate_operations(ctx: OpContext):
    """Run each operation's rule, returning the oldest source reference cited."""
    oldest_ref = None
    for op in ctx.patch.ops:
        name = op.get("op")
        if not name:
            ctx.report.reject(
                "missing-op-name", "Every operation requires an 'op' field.", ctx.revision
            )
            continue
        rule = OP_RULES.get(name)
        if rule is None:
            ctx.report.reject("unknown-operation", f"Unknown operation {name!r}.", ctx.revision)
            continue
        if ctx.mode == "admission" and rule.legacy_only:
            ctx.report.reject(
                "legacy-only-operation",
                f"Operation {name!r} is retained for historical replay and cannot be admitted "
                "in a new patch.",
                ctx.revision,
            )
            continue
        rejects_before = sum(message.level == "reject" for message in ctx.report.messages)
        _validate_proposal_intent_location(ctx, op)
        _validate_operation_authority(ctx, op)
        if rule.structural_validate is not None:
            oldest_ref = older(oldest_ref, rule.structural_validate(op, ctx))
        if ctx.mode == "admission" and rule.authoring_validate is not None:
            oldest_ref = older(oldest_ref, rule.authoring_validate(op, ctx))
        rejects_after = sum(message.level == "reject" for message in ctx.report.messages)
        if rejects_after != rejects_before:
            continue
        try:
            # Imported lazily because materialization imports this validator.
            from rcp.core.materialize import apply_valid_operation

            ctx.state = apply_valid_operation(ctx.state, ctx.patch, op)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            ctx.report.reject(
                "malformed-operation",
                f"Operation {name!r} could not be staged: {exc}.",
                ctx.revision,
            )
    return oldest_ref


def _validate_operation_authority(ctx: OpContext, operation: dict) -> None:
    """Check live producer permission once, before an operation is staged."""

    if ctx.mode != "admission":
        return
    try:
        actions = operation_actions(ctx.initial_state, ctx.patch, operation)
    except ValueError:
        # The operation registry reports missing and unknown operation names.
        return
    for action in sorted(actions):
        if not permits(ctx.patch, action):
            ctx.report.reject(
                "graph-action-refused",
                f"Action {action!r} is not permitted for this Patch producer.",
                ctx.revision,
            )


def _validate_proposal_intent_location(ctx: OpContext, operation: dict) -> None:
    """Keep declared intent on stored Proposal semantics, never ordinary ops."""

    if "intent" not in operation or ctx.mode != "admission":
        return
    semantic_names = {
        "update_nodes",
        "remove_nodes",
        "supersede_nodes",
        "merge_nodes",
        "create_edges",
        "remove_edges",
    }
    is_proposal_dry_run = ctx.patch.kind == "approval" and ctx.reference_patch is not None
    is_proposal_approval = ctx.patch.kind == "approval" and any(
        op.get("op") == "resolve_proposals" for op in ctx.patch.ops
    )
    if operation.get("op") not in semantic_names or (
        not is_proposal_dry_run and not is_proposal_approval
    ):
        ctx.report.reject(
            "unexpected-proposal-intent",
            "Declared intent is legal only inside a Proposal's stored semantic operation.",
            ctx.revision,
        )
