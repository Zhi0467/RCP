"""Research node types' legacy wire and persisted-shape adapters.

Persisted Patches and graph snapshots written by older releases carry research
node shapes the current models no longer accept: Evidence ``strength`` before
``role``/``legacy_strength``, and the retired Experiment ``blocked`` status.
:mod:`rcp.core.operations` owns the generic persisted-document adaptation and
calls these per-type adapters, which never change persisted bytes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def adapt_wire_node_datetimes(
    document: dict[str, Any],
    *,
    wire_datetime: Callable[[Any], Any],
    adapt_source_ref_datetimes: Callable[[Any], None],
) -> None:
    """Parse the JSON datetimes nested in a node's own records, such as Experiment attempts."""

    if document.get("type") == "experiment":
        attempts = document.get("attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                for field in ("started_at", "finished_at"):
                    if field in attempt:
                        attempt[field] = wire_datetime(attempt[field])
                adapt_source_ref_datetimes(attempt.get("source_refs"))


def reject_current_generation_legacy_node(node: Any) -> None:
    """Refuse compatibility metadata a schema-generation 2 Patch cannot create."""

    if isinstance(node, dict) and node.get("type") == "evidence" and "legacy_strength" in node:
        raise ValueError(
            "schema-generation 2 patches cannot create Evidence with "
            "legacy_strength compatibility metadata"
        )


def adapt_legacy_snapshot_node(node: dict[str, Any]) -> None:
    """Adapt one node record of a materialized or cached graph snapshot."""

    if node.get("type") == "evidence":
        _adapt_legacy_evidence_record(node, assume_default_strength=True)
    elif node.get("type") == "experiment":
        _adapt_legacy_experiment_record(node)


def adapt_legacy_created_node(node: Any) -> None:
    """Adapt one node a schema-generation 1 Patch created."""

    if isinstance(node, dict) and node.get("type") == "evidence":
        _adapt_legacy_evidence_record(node, assume_default_strength=True)
    if isinstance(node, dict) and node.get("type") == "experiment":
        _adapt_legacy_experiment_record(node)


def adapt_legacy_node_changes(changes: Any) -> None:
    """Adapt one node update's changes from a schema-generation 1 Patch."""

    if isinstance(changes, dict) and "strength" in changes:
        _adapt_legacy_evidence_record(changes, assume_default_strength=False)
    if isinstance(changes, dict):
        _adapt_legacy_experiment_record(changes)


def is_legacy_status_change(changes: Any, cause: Any) -> bool:
    """Whether a legacy Proposal update was a belief status change an evidence edge caused."""

    return (
        isinstance(changes, dict)
        and set(changes) == {"status"}
        and isinstance(cause, dict)
        and cause.get("kind") == "evidence_edge"
    )


def _adapt_legacy_evidence_record(record: dict[str, Any], *, assume_default_strength: bool) -> None:
    strength = record.pop("strength", None)
    if strength is None and assume_default_strength and "role" not in record:
        strength = "preliminary"
    if strength is None:
        return
    record["role"] = "diagnostic" if strength == "diagnostic" else "result"
    record["legacy_strength"] = strength


def _adapt_legacy_experiment_record(record: dict[str, Any]) -> None:
    if record.get("status") == "blocked":
        record["status"] = "unspecified"
        if record.get("current_summary"):
            record["current_summary_stale"] = True
        if record.get("next_action"):
            record["next_action_stale"] = True
