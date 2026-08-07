"""Patch-level validation: the rules that judge a patch as a whole.

Operation-level rules live in :mod:`rcp.core.validation.ops` and are reached
only through :data:`rcp.core.validation.registry.OP_RULES`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from rcp.core.models import ExperimentDecisionPin, GraphState, Patch
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
    proposal_ids = {
        raw.get("id")
        for operation in ctx.patch.ops
        if operation.get("op") == "create_proposals"
        for raw in operation.get("proposals", [])
        if isinstance(raw, dict) and isinstance(raw.get("id"), str)
    }
    for proposal_id in sorted(proposal_ids):
        proposal = ctx.state.proposals.get(proposal_id)
        if proposal is not None and proposal_is_stale(ctx.state, proposal):
            ctx.report.reject(
                "stale-created-proposal",
                f"Proposal {proposal_id!r} is already stale after applying its outer patch.",
                ctx.revision,
                related_node_ids=list(proposal.related_node_ids),
            )


def _validate_authorship(ctx: OpContext) -> None:
    expected_author = "human" if ctx.patch.kind == "approval" else "agent"
    if ctx.patch.author != expected_author:
        ctx.report.reject(
            "wrong-author",
            f"{ctx.patch.kind} patches must be authored by {expected_author}.",
            ctx.revision,
        )


def _validate_declared_scope(ctx: OpContext) -> None:
    patch = ctx.patch
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
        rejects_before = sum(message.level == "reject" for message in ctx.report.messages)
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
