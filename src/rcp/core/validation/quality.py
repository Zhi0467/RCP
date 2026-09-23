"""Nonblocking authoring advice for issues introduced in the final candidate graph."""

from __future__ import annotations

from collections import defaultdict

from rcp.core.models import Evidence, Experiment, GraphState, Hypothesis
from rcp.core.validation.nodes import normalize_authoring_text
from rcp.core.validation.report import ValidationReport


def flag_introduced_quality_issues(
    initial_state: GraphState,
    candidate: GraphState,
    report: ValidationReport,
    revision: int | None,
) -> None:
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
        title = normalize_authoring_text(node.title)
        if title:
            titles[(node.type, title)].append(node_id)

    _flag_introduced_structure_issues(initial_state, candidate, report, revision)

    for (_node_type, title), node_ids in sorted(titles.items()):
        if len(node_ids) < 2:
            continue
        previous = [initial_state.nodes.get(node_id) for node_id in node_ids]
        previous_titles = {
            (node.type, normalize_authoring_text(node.title))
            for node in previous
            if node is not None
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


_EVIDENCE_HYPOTHESIS_RELATIONS = frozenset(
    {"supports", "weakens", "refutes", "inconclusive", "contradicts"}
)


def _structure_issues(state: GraphState) -> dict[tuple[str, ...], tuple[str, str, list[str]]]:
    """Name each common missing link, keyed so an issue already present is not repeated."""

    edges = [
        edge
        for edge in state.edges.values()
        if edge.source in state.nodes and edge.target in state.nodes
    ]
    parented = {
        edge.target
        for edge in edges
        if edge.relation == "has_hypothesis"
        and state.nodes[edge.source].type == "research_question"
    }
    tested: dict[str, set[str]] = defaultdict(set)
    producers: dict[str, set[str]] = defaultdict(set)
    bears_on: dict[str, set[str]] = defaultdict(set)
    outgoing: set[str] = set()
    for edge in edges:
        source, target = state.nodes[edge.source], state.nodes[edge.target]
        outgoing.add(edge.source)
        if edge.relation == "tests" and isinstance(source, Experiment):
            if isinstance(target, Hypothesis):
                tested[edge.source].add(edge.target)
        elif edge.relation == "produces" and isinstance(source, Experiment):
            if isinstance(target, Evidence):
                producers[edge.target].add(edge.source)
        elif edge.relation in _EVIDENCE_HYPOTHESIS_RELATIONS and isinstance(source, Evidence):
            bears_on[edge.source].add(edge.target)

    issues: dict[tuple[str, ...], tuple[str, str, list[str]]] = {}
    for node_id, node in state.nodes.items():
        if isinstance(node, Hypothesis) and node.status != "superseded" and node_id not in parented:
            issues[("hypothesis-without-question", node_id)] = (
                "hypothesis-without-question",
                f"Hypothesis {node_id} answers no ResearchQuestion; review which question it "
                "serves and connect it with `has_hypothesis`.",
                [node_id],
            )
        if not isinstance(node, Evidence) or node.validity == "superseded":
            continue
        unlinked = sorted(
            (experiment_id, hypothesis_id)
            for experiment_id in producers.get(node_id, ())
            for hypothesis_id in tested.get(experiment_id, ())
            if hypothesis_id not in bears_on.get(node_id, ())
        )
        for experiment_id, hypothesis_id in unlinked:
            issues[("evidence-not-linked-to-tested-hypothesis", node_id, hypothesis_id)] = (
                "evidence-not-linked-to-tested-hypothesis",
                f"Evidence {node_id} comes from {experiment_id}, which tests {hypothesis_id}, "
                "but has no edge to that Hypothesis; review how the result bears on it.",
                [node_id, hypothesis_id],
            )
        if not unlinked and node_id in producers and node_id not in outgoing:
            issues[("evidence-bears-on-nothing", node_id)] = (
                "evidence-bears-on-nothing",
                f"Evidence {node_id} is connected only to the Experiment that produced it; review "
                "which Hypothesis, Decision, or Blocker it bears on.",
                [node_id],
            )
    return issues


def _flag_introduced_structure_issues(
    initial_state: GraphState,
    candidate: GraphState,
    report: ValidationReport,
    revision: int | None,
) -> None:
    previous = _structure_issues(initial_state)
    for key, (code, message, node_ids) in sorted(_structure_issues(candidate).items()):
        if key not in previous:
            report.flag(code, message, revision, related_node_ids=node_ids)


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
