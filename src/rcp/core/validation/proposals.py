from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import ValidationError

from rcp.core.models import GraphState, Patch, Proposal
from rcp.core.validation.constants import IDENTIFIER_RE
from rcp.core.validation.report import ValidationReport


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
) -> None:
    # Imported lazily because the registry that owns this function's callers is
    # itself assembled from the operation rules that reach this module.
    from rcp.core.validation.registry import proposal_dependencies

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
    related_node_ids, related_config_keys = proposal_dependencies(state, proposal.ops)
    supplied_node_ids = set(proposal.related_node_ids)
    supplied_config_keys = set(proposal.related_config_keys)
    if proposal.base_rev != state.revision:
        report.reject(
            "proposal-base-revision",
            f"Proposal {proposal.id} must use the current graph revision {state.revision}.",
            revision,
        )
    if (
        supplied_node_ids != set(related_node_ids)
        or len(supplied_node_ids) != len(proposal.related_node_ids)
        or supplied_config_keys != set(related_config_keys)
        or len(supplied_config_keys) != len(proposal.related_config_keys)
    ):
        report.reject(
            "proposal-dependency-mismatch",
            f"Proposal {proposal.id} must declare exactly the affected nodes "
            f"{related_node_ids} and project settings {related_config_keys}.",
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
) -> None:
    # Imported lazily: a proposal's operations are checked by replaying them
    # through the very validator whose rules reach this module.
    from rcp.core.validation.patch import validate_patch

    control_ops = {
        str(op.get("op", ""))
        for op in proposal.ops
        if op.get("op") in {"create_proposals", "resolve_proposals", "set_standing"}
    }
    if control_ops:
        report.reject(
            "invalid-proposal-ops",
            f"Proposal {proposal.id} contains approval-control operations: "
            f"{', '.join(sorted(control_ops))}.",
            revision,
        )
        return

    synthetic_state = state.model_copy(deep=True)
    synthetic_state.proposals[proposal.id] = proposal
    synthetic_patch = Patch(
        revision=revision or state.revision + 1,
        kind="approval",
        author="human",
        summary=f"Validate replay operations for {proposal.id}.",
        ops=[
            *proposal.ops,
            {
                "op": "resolve_proposals",
                "resolutions": [{"id": proposal.id, "status": "approved"}],
            },
        ],
    )
    replay_report = validate_patch(
        synthetic_state,
        synthetic_patch,
        project_truth_scope,
        repository_aliases=repository_aliases,
        machine_aliases=machine_aliases,
        default_run_truth_scope=default_run_truth_scope,
        state_repository=state_repository,
        mode=validation_mode,
    )
    errors = [message.message for message in replay_report.messages if message.level == "reject"]
    if errors:
        report.reject(
            "invalid-proposal-ops",
            f"Proposal {proposal.id} contains invalid replay operations: {'; '.join(errors)}",
            revision,
        )
        return
    try:
        # Imported lazily because materialization itself imports this validator.
        from rcp.core.materialize import apply_valid_patch

        apply_valid_patch(synthetic_state, synthetic_patch)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        report.reject(
            "invalid-proposal-ops",
            f"Proposal {proposal.id} cannot be replayed atomically: {exc}.",
            revision,
        )
