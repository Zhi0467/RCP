"""Patch-level validation: the rules that judge a patch as a whole.

Operation-level rules live in :mod:`rcp.core.validation.ops` and are reached
only through :data:`rcp.core.validation.registry.OP_RULES`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from rcp.core.models import GraphState, Patch
from rcp.core.validation.approval import validate_approval_shape
from rcp.core.validation.context import OpContext
from rcp.core.validation.nodes import older
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
) -> ValidationReport:
    report = ValidationReport()
    scope = set(project_truth_scope)
    ctx = OpContext(
        state=state,
        patch=patch,
        report=report,
        revision=patch.revision or None,
        project_truth_scope=scope,
        repositories=set(repository_aliases or scope),
        machines=set(machine_aliases) if machine_aliases is not None else None,
        default_run_truth_scope=set(default_run_truth_scope or ()),
        state_repository=state_repository,
        mode=mode,
    )

    if patch.revision and patch.revision != state.revision + 1:
        report.reject(
            "non-monotonic-revision",
            f"Patch revision {patch.revision} must follow graph revision {state.revision}.",
            ctx.revision,
        )
    _validate_authorship(ctx)
    _validate_declared_scope(ctx)

    op_names = [str(op.get("op", "")) for op in patch.ops]
    if any(name.startswith("delete") for name in op_names):
        report.reject("delete-forbidden", "Graph objects are never deleted.", ctx.revision)

    if patch.kind == "approval":
        validate_approval_shape(state, patch, report)

    oldest_ref = _validate_operations(ctx)

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
        if rule.structural_validate is not None:
            oldest_ref = older(oldest_ref, rule.structural_validate(op, ctx))
        if ctx.mode == "admission" and rule.authoring_validate is not None:
            oldest_ref = older(oldest_ref, rule.authoring_validate(op, ctx))
    return oldest_ref
