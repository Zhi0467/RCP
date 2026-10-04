"""Research node types' own validation and materialization rules.

Kernel validation asks the project type (:mod:`rcp.core.project_types`) which
node types play which part. The rules here read one research node type's own
fields (a Decision's ballot, an Experiment's attempts, Evidence compatibility
metadata) or name research relations, so they belong to the research layer and
the kernel calls them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from rcp.core.models import (
    ACTIVE_EXPERIMENT_ATTEMPT_STATUSES,
    EXPERIMENT_COMPATIBILITY_STATUSES,
    Decision,
    Evidence,
    Experiment,
)

if TYPE_CHECKING:
    from rcp.core.models import ProjectNode
    from rcp.core.validation.report import ValidationReport

# Relations that may carry an Experiment's expected-outcome judgment.
EXPECTATION_RELATIONS = frozenset({"produces"})

# Decision statuses that queue it for a choice.
_DECISION_QUEUE_STATUSES = frozenset({"open", "ready", "revisit"})
# Queued Decision statuses whose ballot must offer a real choice.
_DECISION_BALLOT_STATUSES = frozenset({"ready", "revisit"})


def reject_live_legacy_new_node(
    node: ProjectNode, report: ValidationReport, revision: int | None
) -> None:
    """Refuse compatibility-only fields on a node a live Patch creates."""

    if isinstance(node, Experiment) and node.status in EXPERIMENT_COMPATIBILITY_STATUSES:
        report.reject(
            "live-legacy-experiment-phase",
            f"New Experiment {node.id!r} cannot author compatibility-only phase 'unspecified'.",
            revision,
            related_node_ids=[node.id],
        )
    if isinstance(node, Evidence) and "legacy_strength" in node.model_fields_set:
        report.reject(
            "live-legacy-evidence-strength",
            f"New Evidence {node.id!r} cannot set compatibility-only legacy_strength.",
            revision,
            related_node_ids=[node.id],
        )


def reject_live_legacy_update(
    node: ProjectNode,
    changes: Mapping[str, Any],
    report: ValidationReport,
    revision: int | None,
    *,
    admission: bool,
) -> tuple[bool, bool]:
    """Refuse compatibility-only fields in a live update.

    Returns whether the update authored a legacy Experiment phase and whether it
    set legacy Evidence strength.
    """

    live_legacy_phase = (
        admission
        and isinstance(node, Experiment)
        and changes.get("status") in EXPERIMENT_COMPATIBILITY_STATUSES
    )
    if live_legacy_phase:
        report.reject(
            "live-legacy-experiment-phase",
            f"Update to Experiment {node.id!r} cannot author compatibility-only phase "
            "'unspecified'.",
            revision,
            related_node_ids=[node.id],
        )
    live_legacy_strength = admission and isinstance(node, Evidence) and "legacy_strength" in changes
    if live_legacy_strength:
        report.reject(
            "live-legacy-evidence-strength",
            f"Update to Evidence {node.id!r} cannot set compatibility-only legacy_strength.",
            revision,
            related_node_ids=[node.id],
        )
    return live_legacy_phase, live_legacy_strength


def queues_decision(changes: Mapping[str, Any]) -> bool:
    """Whether a Decision update queues it for a choice."""

    return changes.get("status") in _DECISION_QUEUE_STATUSES


def chooses_decision(changes: Mapping[str, Any]) -> bool:
    """Whether a Decision update records its outcome."""

    return changes.get("status") == "decided" or changes.get("selected_option") is not None


def reject_incomplete_decision_ballot(
    node: ProjectNode | None, report: ValidationReport, revision: int | None
) -> None:
    """Refuse a queued Decision that does not offer two distinct options."""

    if (
        isinstance(node, Decision)
        and node.status in _DECISION_BALLOT_STATUSES
        and len(set(node.options)) < 2
    ):
        report.reject(
            "incomplete-decision-ballot",
            f"Decision {node.id} must have at least two distinct options before it can be "
            f"queued as {node.status}.",
            revision,
            related_node_ids=[node.id],
        )


def evidence_relation_endpoint_error(
    relation: str, source_type: str | None, target_type: str | None
) -> str | None:
    """Why an evidence relation's endpoints are wrong, or ``None`` when they fit."""

    if target_type == "hypothesis" and (
        source_type == "evidence" or (relation == "contradicts" and source_type == "hypothesis")
    ):
        return None
    return (
        f"Relation {relation!r} requires Evidence -> Hypothesis"
        + (" or Hypothesis -> Hypothesis" if relation == "contradicts" else "")
        + f" endpoints, not {source_type} -> {target_type}."
    )


def expectation_applies(relation: str, source_type: str | None, target_type: str | None) -> bool:
    """Whether an edge may carry an expectation (unknown endpoints are not judged)."""

    return (
        relation in EXPECTATION_RELATIONS
        and (source_type is None or source_type == "experiment")
        and (target_type is None or target_type == "evidence")
    )


def has_active_experiment_attempt(*nodes: ProjectNode) -> bool:
    """Whether any of these versions of a node is an Experiment with an active attempt."""

    experiment_versions = (candidate for candidate in nodes if isinstance(candidate, Experiment))
    return any(
        attempt.status in ACTIVE_EXPERIMENT_ATTEMPT_STATUSES
        for experiment in experiment_versions
        for attempt in experiment.attempts
    )
