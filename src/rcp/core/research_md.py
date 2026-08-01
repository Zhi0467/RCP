from __future__ import annotations

from collections import defaultdict

from rcp.core.models import Decision, GraphState, Hypothesis, ResearchQuestion, Standing


def render_research_md(state: GraphState) -> str:
    accepted = [node for node in state.nodes.values() if node.standing == Standing.ACCEPTED]
    sections: dict[str, list[str]] = defaultdict(list)

    for node in sorted(accepted, key=lambda item: item.id):
        if isinstance(node, ResearchQuestion):
            details = node.question
            if node.scope:
                details += f" Scope: {node.scope}"
            sections["Research questions"].append(f"- **{node.title}** — {details}")
        elif isinstance(node, Hypothesis):
            scope = f" Scope: {node.scope}" if node.scope else ""
            sections["Hypotheses"].append(
                f"- **{node.title}** (`{node.status}`) — {node.statement}{scope}"
            )
        elif isinstance(node, Decision):
            if node.status == "decided":
                selected = node.selected_option or "Decision recorded without a selected option"
                rationale = f" {node.rationale}" if node.rationale else ""
                line = f"- **{node.title}** — Decided: {selected}.{rationale}"
            else:
                line = f"- **{node.title}** — **Open:** {node.question}"
            sections["Decisions"].append(line)

    if not sections:
        return ""

    lines = ["# Accepted research", "", f"Generated from graph revision {state.revision}.", ""]
    for heading in ("Research questions", "Hypotheses", "Decisions"):
        entries = sections.get(heading)
        if not entries:
            continue
        lines.extend((f"## {heading}", "", *entries, ""))
    return "\n".join(lines).rstrip() + "\n"
