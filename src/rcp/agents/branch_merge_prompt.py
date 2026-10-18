"""Closed answer contracts for the graph-only branch merge agent."""

from __future__ import annotations

import json
from typing import Literal

from rcp.agents.continuation_prompt import MASTER_OVERLAY_RULE
from rcp.agents.prompts import PROVIDER_NATIVE_SUBAGENT_LIFETIME
from rcp.runs.merge_contract import MergeAnswers, MergePlan, merge_contract_digest

# Bumped when the branch-merge task contract's stable policy prose changes.
BRANCH_MERGE_POLICY_VERSION = "branch-merge-v5"


def branch_merge_task_contract(
    *,
    plan: MergePlan,
    plan_path: str,
    answers_path: str,
    answers_schema_path: str,
    validator_command: str,
    retained_answers: MergeAnswers | None = None,
    code_block: str = "",
) -> str:
    """Render questions and enforcement facts from the exact checked plan."""

    if not all((plan_path, answers_path, answers_schema_path, validator_command)):
        raise ValueError("branch merge contract requires exact plan, output, schema, and check")
    retained = retained_answers or MergeAnswers(answers={}, cards={})
    questions = {
        "conflicts": [
            conflict.model_dump(mode="json")
            for conflict in plan.conflicts
            if conflict.conflict_id not in retained.answers
        ],
        "review_groups": [
            group.model_dump(mode="json")
            for group in plan.review_groups
            if group.subject_key not in retained.cards
        ],
    }
    binding = {
        "plan_id": plan.plan_id,
        "task_id": plan.merge_task_id,
        "profile": plan.profile,
        "source_head": plan.source_head.model_dump(mode="json"),
        "destination_head": plan.destination_head.model_dump(mode="json"),
        "preparation_ruleset": plan.preparation_contract.ruleset_tag,
        "preparation_contract_digest": merge_contract_digest(
            plan.preparation_contract.model_dump(mode="json")
        ),
    }
    return f"""# RCP episode merge

{PROVIDER_NATIVE_SUBAGENT_LIFETIME}

Answer the graph merge questions below. RCP builds the complete Patch from this plan and
checks it through core authority and transition rules. You author answers and review-card
prose only. Protected effects become pending destination Proposals for later human approval.
{code_block}

Exact files and command:
- immutable merge plan and snapshots: `{plan_path}`
- answers schema: `{answers_schema_path}`
- only graph output: `{answers_path}`
- check command: `{validator_command}`

The checked plan binds these enforcement facts:
```json
{json.dumps(binding, indent=2, sort_keys=True)}
```

Questions still needing judgment:
```json
{json.dumps(questions, indent=2, sort_keys=True)}
```

Each conflict supplies its base, branch, and destination values and allowed answer kinds.
Choose a permitted side or provide a merged field value where permitted. Missing values
and present null values are distinct. A node-group conflict is one coupled choice; a merged
answer maps node IDs to field names to field answers. The nested descriptors give each
field's allowed kinds. Preserve compatible changes and source provenance.

Each review group supplies its changes, approval dependencies/effects, and effect digest.
Write its card using the schema's situation_cold, why_human_now, consequences, and
decision_needed fields. Cards describe the proposed effect and the human's decision; they
do not approve it. Key cards by subject_key, never by Proposal ID.

Retained answers and cards already match this plan; preserve them in the complete output:
```json
{retained.model_dump_json(indent=2)}
```

Write one JSON object matching the answers schema. Its answers keys are exactly the plan's
conflict IDs and its cards keys are the review subject keys needing cards. Do not write
operations, a Patch, authority claims, or canonical state. Read repositories and write
repository files only where the code merge section explicitly permits it. The graph task
adds no project configuration, ontology, membership, or Proposal approval authority.

Run the exact check command before finishing. It dry-prepares this plan and answers without
committing. Exit 0 records a successful check bound to this task, turn, plan, source and
destination heads, preparation contract, and exact output bytes. Exit 1 supplies an answer
error to fix and check again in this turn. Exit 2 means the check is unavailable. Merge
checks have no count budget. A check result with outcome `promotion` means RCP must add
coupled questions: end this turn so RCP can stage the promoted plan and continue your session.
An outcome `refused` is terminal; end the turn with its diagnostic. Otherwise finish only
with the exact successfully checked answers bytes; changing the file after a successful
check requires another check. Missing or mismatched
successful checks fail the task; there is no correction turn after you finish. RCP performs
live preparation again and commits atomically, or commits nothing.

{MASTER_OVERLAY_RULE}
"""


def code_merge_task_contract(*, code_block: str) -> str:
    """Describe a merge turn that lands code alone; RCP carries any graph itself."""

    if not code_block:
        raise ValueError("a code merge contract requires its code merge section")
    return f"""# RCP episode merge

{PROVIDER_NATIVE_SUBAGENT_LIFETIME}

You are the dedicated merge agent for one human-dispatched episode merge. This turn merges
the episode's code only. The graph needs no judgment, so RCP carries it itself: write no
graph output. The turn carries the one repository write boundary in the code merge section
below, and no graph authority and no authority over project configuration, ontology,
membership, or Proposal approval.
{code_block}

When the merge commit is in place, reply with a short summary of how you resolved each
conflict.
"""


def branch_merge_rebase_parts(
    *,
    previous_plan_id: str,
    plan_id: str,
    cause: Literal["promotion", "destination_moved"],
    code: bool = False,
) -> list[str]:
    """Continue the native session with replacement questions and reusable answers."""

    if not previous_plan_id or not plan_id or previous_plan_id == plan_id:
        raise ValueError("branch merge rebase requires a new plan identity")
    reason = {
        "promotion": (
            "Dry preparation found a hidden clash. RCP promoted the related nodes into a "
            "coupled conflict in the replacement plan. Answer that group together."
        ),
        "destination_moved": (
            "The destination advanced. RCP rebuilt the plan against its new head; answer "
            "the remaining questions using the replacement destination values."
        ),
    }[cause]
    parts = [
        "# RCP graph-branch merge rebase\n\n"
        + reason
        + " The previous candidate was not committed. Preserve the supplied retained answers and "
        "cards, answer only the remaining questions, and write the complete answers file. "
        "Run the check again: the previous plan's successful receipt cannot check this plan."
    ]
    if code:
        parts.append("Keep the code merge you already landed; do not redo it.")
    return parts
