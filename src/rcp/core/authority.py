from __future__ import annotations

from hashlib import sha256
from typing import Literal

from rcp.core.models import Patch

AGENT_GRAPH_AUTHORITY_POLICY_VERSION = "s94-v1"

HYPOTHESIS_PROPOSAL_FIELDS = frozenset({"status"})
EVIDENCE_EDGE_CAUSE_KIND = "evidence_edge"
EVIDENCE_RELATIONS = frozenset({"supports", "weakens", "refutes", "inconclusive", "contradicts"})

DECIDE_DECISION = "decide_decision"
QUEUE_DECISION = "queue_decision"
DecisionAction = Literal["decide_decision", "queue_decision"]

_AGENT_GRAPH_AUTHORITY_BODY = """Assert directly:
- Ordinary legal graph structure and content are assertions, not Proposals. This includes creating
  or editing ordinary nodes; creating Evidence, Blockers, or replacement nodes; and adding or
  removing any legal edge, including an edge whose endpoint has accepted standing.
- Editing accepted node content applies directly and resets that node to asserted standing for
  ordinary review. Never preserve accepted standing on content the agent changed.
- Removing an asserted or contested node also removes all of its incident edges. Never remove an
  accepted node or an Experiment with a planned, submitted, or running bounded-loop attempt.
- Agents may create a Decision as `open` or `ready`, and may queue an existing Decision as `open`,
  `ready`, or `revisit`. Agents never write `selected_option` or set `status="decided"`; those
  writes require the `decide_decision` action. Every agent-created Hypothesis starts
  `status="proposed"`.

Proposal-only changes:
- A belief Proposal may change only `status` on exactly one Hypothesis, including one created
  earlier in the same outer Patch. It contains exactly one `update_nodes` operation for that
  Hypothesis and its update has a cause with
  `kind="evidence_edge"` naming a valid Evidence -> Hypothesis epistemic edge. No other cause kind
  is agent-authorized. Never directly change, merge, or supersede a Hypothesis when that would
  change its status.
- Do not put any other operation or semantic change in a Proposal.

Human-only authority:
- Agents never set `standing`, approve, or reject Proposals; they may withdraw any pending Proposal
  with `withdraw_proposals` when obsolete or duplicated. Withdrawal applies no semantic operations
  and preserves lifecycle provenance. Agents may not change project configuration or authorize an
  Experiment **Run**. Approval never launches or resumes an Experiment. Only the human pressing
  **Run** grants RCP permission to launch. A human request cannot delegate these actions."""

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


def permits(patch: Patch, action: DecisionAction) -> bool:
    """Temporary action predicate for the future actor-profile permission lookup."""

    if action == DECIDE_DECISION:
        return patch.author == "human"
    if action == QUEUE_DECISION:
        return True
    raise ValueError(f"Unknown Decision action {action!r}.")
