"""Nonblocking authoring advice for issues introduced in the final candidate graph."""

from __future__ import annotations

from collections import defaultdict

from rcp.core.models import Evidence, Experiment, GraphState
from rcp.core.validation.report import ValidationReport


def flag_introduced_quality_issues(
    initial_state: GraphState,
    candidate: GraphState,
    report: ValidationReport,
    revision: int | None,
) -> None:
    # Atomic transitions may contain several separately validated source Patches.
    # Replace their intermediate advice with advice about the complete result.
    report.messages = [
        message
        for message in report.messages
        if not (
            message.level == "flag"
            and message.code
            in {
                "internal-evidence-without-experiment",
                "isolated-operational-node",
                "identical-node-title",
            }
        )
    ]
    # A rejected Patch has only a partially staged graph, not a valid candidate.
    if report.rejected:
        return

    previous_connected, previous_produced = _connections(initial_state)
    connected, produced = _connections(candidate)
    titles: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node_id, node in sorted(candidate.nodes.items()):
        previous = initial_state.nodes.get(node_id)
        if (
            isinstance(node, Evidence)
            and node.origin == "internal_run"
            and node_id not in produced
            and not (
                isinstance(previous, Evidence)
                and previous.origin == "internal_run"
                and node_id not in previous_produced
            )
        ):
            report.flag(
                "internal-evidence-without-experiment",
                f"Evidence {node_id} has origin internal_run but no producing Experiment; "
                "review which Experiment produced this result.",
                revision,
                related_node_ids=[node_id],
            )
        if (
            node.type in {"experiment", "evidence", "decision", "blocker"}
            and node_id not in connected
            and (previous is None or node_id in previous_connected)
        ):
            report.flag(
                "isolated-operational-node",
                f"{node_id} has no graph connections; review how it relates to the research.",
                revision,
                related_node_ids=[node_id],
            )
        title = _normalized_title(node.title)
        if title:
            titles[(node.type, title)].append(node_id)

    for (_node_type, title), node_ids in sorted(titles.items()):
        if len(node_ids) < 2:
            continue
        previous = [initial_state.nodes.get(node_id) for node_id in node_ids]
        previous_titles = {
            (node.type, _normalized_title(node.title)) for node in previous if node is not None
        }
        if (
            all(node is not None for node in previous)
            and len(previous_titles) == 1
            and next(iter(previous_titles))[1]
        ):
            continue
        report.flag(
            "identical-node-title",
            f"{', '.join(node_ids)} share the title {title!r}; review whether they are "
            "distinct nodes rather than merging automatically.",
            revision,
            related_node_ids=node_ids,
        )


def _normalized_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def _connections(state: GraphState) -> tuple[set[str], set[str]]:
    connected: set[str] = set()
    produced: set[str] = set()
    for edge in state.edges.values():
        source = state.nodes.get(edge.source)
        target = state.nodes.get(edge.target)
        if source is None or target is None:
            continue
        connected.update((edge.source, edge.target))
        if (
            edge.relation == "produces"
            and isinstance(source, Experiment)
            and isinstance(target, Evidence)
        ):
            produced.add(edge.target)
    return connected, produced
