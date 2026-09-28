"""Closed task contracts for the graph-only branch merge agent."""

from __future__ import annotations

from rcp.agents.auto_research_prompt import orchestrator_graph_authority_contract
from rcp.agents.continuation_prompt import MASTER_OVERLAY_RULE
from rcp.agents.graph_rules import graph_rules
from rcp.agents.prompts import PROVIDER_NATIVE_SUBAGENT_LIFETIME

# Bumped when the branch-merge task contract's stable policy prose changes.
BRANCH_MERGE_POLICY_VERSION = "branch-merge-v1"


def branch_merge_task_contract(
    *,
    context_path: str,
    context_id: str,
    patch_path: str,
    validator_command: str,
    review_contract_json: str,
    plan_path: str,
    residue_block: str,
    ontology_extensions: bool = False,
) -> str:
    """Describe one fresh semantic rebase without exposing repositories."""

    _require_inputs(context_path, context_id, patch_path, validator_command)
    return f"""# RCP graph-branch merge

{PROVIDER_NATIVE_SUBAGENT_LIFETIME}

You are the dedicated graph-only merge agent for one human-dispatched Auto-research branch
merge. This task carries orchestrator graph authority, but it carries no repository authority
and no authority over project configuration, ontology, membership, or Proposal approval.

Exact immutable inputs:
- merge context: `{context_path}`
- merge context id: `{context_id}`
- built operation plan: `{plan_path}`
- candidate Patch output: `{patch_path}`
- live validator command: `{validator_command}`

The merge context contains the immutable branch-base graph, exact branch-head graph, current
main graph, a typed base-to-branch semantic delta, branch Patch summaries, transition-manager
contracts, and deterministic three-way conflicts. Treat those files and heads as exact. Do not
infer a different base, inspect canonical state directories, or inspect any repository.

RCP has already built every ordinary operation. The exact built plan is the file at the
operation-plan path above. RCP prepends those operations during both self-check and commit, so
never copy them into your output; read that file only to see what main already contains when
your own operations apply.

Write only the operations for the paths below to `patch.json`.

{residue_block}

The base, branch, and main values for every path are in the merge context at that same path. A
path can also be satisfied by a transition-generated effect rather than a direct write.
Preserve compatible main-side changes. If the intended outcome cannot be represented legally,
leave a precise diagnostic in your final response and do not invent authority.

{orchestrator_graph_authority_contract()}
{graph_rules(edits=True, ontology_extensions=ontology_extensions)}

The exact review policy used by validation is:
```json
{review_contract_json}
```
Carry protected graph changes as pending main Proposals. Their approval is a later human
action in main's Inbox; never copy branch Proposal approvals or rejections. A branch-resolved
Proposal's actual node and relation effects are separate source changes that still need to
be carried. Only live pending source Proposals retain their IDs and contents. A source Proposal
made stale by later branch edits remains in branch history; carry the actual node/relation
change independently instead of recreating that stale review in main.

For new review Proposals, RCP derives a stable ID from this branch, the last delivered source
head (or the immutable base before the first merge), and the exact semantic operation;
candidate IDs are temporary. Retries reuse that ID; a new change after an intervening delivery
can receive a fresh review even when it restores an earlier value. Reuse an equivalent live
pending main review from any earlier delivery by omitting its duplicate.
Standing changes and a status_change with human_edit cause require an exact
matching human_changes fact in the policy above; do not invent a human cause.
When several protected edits target the same node, use the policy's same_node_bundle:
one Proposal may contain at most one content_change, one status_change, and one standing_change
for that same ResearchQuestion or Hypothesis. This keeps their approval atomic. An explicit
standing_change is applied exactly; otherwise normal content/status approval accepts the node's
standing. Keep different nodes and structural actions in separate Proposals.
If the source removed an ordinary node that is still accepted on main, carry that exact
one-node removal in a pending removal Proposal; the merge cannot remove it directly.

When previous_merge_receipt and previous_branch_graph are present, they prove which source
head was already delivered. Merge only changes since that exact source snapshot, preserving
main's subsequent human decisions. The full immutable-base semantic_delta remains historical
context. Never recreate unchanged changes from that earlier merged head.

Only permitted file output:
- Write exactly one JSON object to `{patch_path}` matching the orchestrator agent Patch schema in
  the merge context.
- Include only `summary`, semantic `ops`, `repositories_read` (which must be `[]`),
  `change_summary`, and `agent_action` only when the operation actually chooses a Decision.
- Preserve non-conflicting `source_refs` verbatim. For a source-ref conflict, choose among
  the supplied main and branch refs; never invent a ref or remove provenance just to pass
  validation. This task reads no repository.
- Do not include revisions, graph heads, merge ids, branch provenance, authorizers, task ids,
  transition traces, admission fields, or other RCP bookkeeping. RCP supplies all of them.
- Do not write repository files, watcher files, artifacts, or canonical `.research` files.

Before finishing, run the exact validator command. Exit 0 means the candidate is semantically
valid against the live current main graph, exit 1 supplies a correction diagnostic, and exit 2
means validation is unavailable rather than semantically invalid. A successful self-check does
not commit anything; RCP revalidates and commits atomically or commits nothing.

{MASTER_OVERLAY_RULE}
"""


def branch_merge_correction_parts(*, diagnostics_path: str) -> list[str]:
    """Request one bounded scratch-only correction in the same native session."""

    if not diagnostics_path:
        raise ValueError("branch merge correction requires an exact diagnostic path")
    return [
        "# RCP graph-branch merge Patch correction\n\nValidation rejected the candidate Patch; "
        "the branch and main heads have not changed.",
        f"- Validation diagnostics: `{diagnostics_path}`\n"
        "Correct only the residue operations and rewrite the Patch so it validates; RCP still "
        "prepends the built operations. Perform no operational side effect, inspect no "
        "repositories, add no RCP bookkeeping or branch provenance, and write nothing but the "
        "Patch.",
    ]


def branch_merge_rebase_parts(
    *,
    previous_context_id: str,
    context_id: str,
    new_reason_legend: str,
) -> list[str]:
    """Replace a discarded candidate after main moved, preserving the native session.

    The replacement context, plan, and residue travel as changed values; only the meaning
    of a residue reason the master never explained is added here.
    """

    if not previous_context_id:
        raise ValueError("branch merge rebase requires the previous context id")
    if previous_context_id == context_id:
        raise ValueError("branch merge rebase requires a newly resolved main context")
    parts = [
        "# RCP graph-branch merge rebase\n\nMain advanced, so RCP discarded the previous "
        "candidate; nothing from it was committed. RCP rebuilt the ordinary operations against "
        "the replacement main head. Recompute the merge against the replacement context: "
        "preserve compatible new main changes, resolve its conflicts, and rewrite the Patch "
        "instead of reusing the stale candidate. Write only the operations for the current "
        "residue: the `residue` listed below replaces the master's list; if none is listed, "
        "the master's list still holds."
    ]
    if new_reason_legend:
        parts.append("What each new residue reason means:\n" + new_reason_legend)
    return parts


def _require_inputs(
    context_path: str,
    context_id: str,
    patch_path: str,
    validator_command: str,
) -> None:
    if not context_path or not patch_path or not validator_command:
        raise ValueError("branch merge contract requires exact context, output, and validator")
    if len(context_id) != 64 or any(
        character not in "0123456789abcdef" for character in context_id
    ):
        raise ValueError("branch merge context id must be a SHA-256 digest")
