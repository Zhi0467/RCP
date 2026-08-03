from __future__ import annotations

from hashlib import sha256
from typing import Any

from rcp.core.models import Experiment, GraphState, Patch

AGENT_GRAPH_AUTHORITY_POLICY_VERSION = "s50-v1"

DECISION_PROPOSAL_FIELDS = frozenset({"selected_option", "status"})
HYPOTHESIS_PROPOSAL_FIELDS = frozenset({"status"})
EVIDENCE_EDGE_CAUSE_KIND = "evidence_edge"
EXPERIMENT_GOVERNING_RELATION = "governed_by"
EVIDENCE_RELATIONS = frozenset({"supports", "weakens", "refutes", "inconclusive", "contradicts"})

_AGENT_GRAPH_AUTHORITY_BODY = """Assert directly:
- Ordinary legal graph structure and content are assertions, not Proposals. This includes creating
  or editing ordinary nodes; creating Evidence, Blockers, or replacement nodes; and adding or
  removing any legal edge, including an edge whose endpoint has accepted standing.
- Editing accepted node content applies directly and resets that node to asserted standing for
  ordinary review. Never preserve accepted standing on content the agent changed.
- Every agent-created Decision starts `status="open"` with `selected_option=null`. Every
  agent-created Hypothesis starts `status="proposed"`.

Proposal-only changes:
- A Decision Proposal may change only `selected_option` and/or `status` on exactly one Decision
  that is an Experiment input through an Experiment -> Decision `governed_by` edge in the current
  graph or the same outer Patch. That Decision may also be created earlier in the outer Patch. The
  Proposal contains exactly one `update_nodes` operation for it. Never directly select, change,
  reopen, merge, or supersede a Decision when that would change its choice or status.
- A belief Proposal may change only `status` on exactly one Hypothesis, including one created
  earlier in the same outer Patch. It contains exactly one `update_nodes` operation for that
  Hypothesis and its update has a cause with
  `kind="evidence_edge"` naming a valid Evidence -> Hypothesis epistemic edge. No other cause kind
  is agent-authorized. Never directly change, merge, or supersede a Hypothesis when that would
  change its status.
- Do not put any other operation or semantic change in a Proposal.

Human-only authority:
- Agents never set `standing`; resolve, approve, reject, or withdraw Proposals; change project
  configuration such as ontology or project truth scope; or authorize an Experiment **Run**.
  Proposal approval never launches or resumes an Experiment. Only the human pressing **Run** grants
  RCP permission to launch a separate operational turn. A human request cannot delegate these
  actions."""

AGENT_GRAPH_AUTHORITY_POLICY_DIGEST = sha256(
    _AGENT_GRAPH_AUTHORITY_BODY.encode("utf-8")
).hexdigest()[:16]


def render_agent_graph_authority_contract() -> str:
    """Return the one model-facing authority block shared by graph-capable tasks."""

    return (
        "Agent graph authority contract:\n"
        f"- Policy version: `{AGENT_GRAPH_AUTHORITY_POLICY_VERSION}`\n"
        f"- Policy digest: `{AGENT_GRAPH_AUTHORITY_POLICY_DIGEST}`\n"
        f"{_AGENT_GRAPH_AUTHORITY_BODY}"
    )


def decision_is_experiment_input(
    state: GraphState,
    decision_id: str,
    context_patch: Patch | None = None,
) -> bool:
    """Whether a Decision is governed by an Experiment after this Patch's edge operations."""

    experiment_ids = {
        node_id for node_id, node in state.nodes.items() if isinstance(node, Experiment)
    }
    active_edges = {
        edge_id
        for edge_id, edge in state.edges.items()
        if edge.relation == EXPERIMENT_GOVERNING_RELATION
        and edge.target == decision_id
        and edge.source in experiment_ids
    }
    if context_patch is None:
        return bool(active_edges)

    for operation in context_patch.ops:
        if operation.get("op") != "create_nodes":
            continue
        for raw in operation.get("nodes", []):
            if isinstance(raw, dict) and raw.get("type") == "experiment":
                node_id = raw.get("id")
                if isinstance(node_id, str):
                    experiment_ids.add(node_id)
    for operation in context_patch.ops:
        name = operation.get("op")
        if name == "create_edges":
            for raw in operation.get("edges", []):
                if not isinstance(raw, dict):
                    continue
                source = raw.get("source")
                target = raw.get("target")
                relation = raw.get("relation")
                if (
                    relation == EXPERIMENT_GOVERNING_RELATION
                    and target == decision_id
                    and source in experiment_ids
                ):
                    active_edges.add(_edge_id(raw))
        elif name == "remove_edges":
            active_edges.difference_update(
                edge_id for edge_id in operation.get("edge_ids", []) if isinstance(edge_id, str)
            )

    return bool(active_edges)


def _edge_id(raw: dict[str, Any]) -> str:
    explicit = raw.get("id")
    if isinstance(explicit, str):
        return explicit
    return f"{raw.get('source')}::{raw.get('relation')}::{raw.get('target')}"
