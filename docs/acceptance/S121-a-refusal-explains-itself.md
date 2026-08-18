---
id: S121-a-refusal-explains-itself
status: pending
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [1, 3, 10b]
---

# A refusal explains itself, and says what it did not undo

**Confirmed by the human 2026-08-15.**

It owns the user-visible refusal boundary in
[Two permission gates](../specs/authority-and-proposals.md#two-permission-gates).
It waits on no additional design decision:
the two gates in [S100](S100-permission-is-checked-twice.md) run today, on a
personal space, against every agent.

## The problem

RCP refuses agent actions correctly and then explains them badly.

A refusal today reaches the human as the string the gate raised:

> Authority refused action 'apply': Patch profile does not match the dispatch binding.

That sentence is written for whoever is reading `core/authority.py`. It names an
internal action id, an internal binding, and no person. It does not say who
authorized the work, which agent profile ran it, what the agent was trying to
change, or — the part that actually matters — that the repository writes and the
answer are still there and nothing was rolled back.

The task carrying it ends as `failed`, which is the second problem. When Apply
refuses a Work patch, the operational work succeeded: the experiment ran, the
files were written, the answer came back. Only the graph reflection was refused.
Calling that "failed" tells the human the opposite of what happened, and
[S100](S100-permission-is-checked-twice.md) already promises that a refusal is
not a retraction — the interface is the half of that promise nobody built.

## Decided 2026-08-15

1. **A refused Apply gets its own terminal task state, `refused`.** `failed` is
   what Retry keys off, and retrying a turn whose operational work already landed
   is the one thing that must not be invited. This is a new literal in
   `AgentTaskStatus` — a shared contract — so it lands serially, before anything
   else in this scenario, across
   [`storage/models.py`](../../src/rcp/storage/models.py),
   [`web/src/types.ts`](../../web/src/types.ts), and every status projection.
   `ACTIVE_AGENT_TASK_STATUSES` does not change: `refused` is terminal.
2. **The human reads it in the existing Agent task inspector and the Runs run
   detail.** No new destination. The card carries its state; an explicit control
   opens the read-only explanation.
3. **A refused dispatch leaves no record.** Nothing launched, no budget was
   spent, no scratch exists — [S100](S100-permission-is-checked-twice.md) asserts
   exactly that, and writing a row to describe an absence would contradict it.
   The refusal is feedback at the point of the click and nowhere else.
4. **Provenance is the human authorizer, the agent profile, the action in
   ordinary words, and what still stands.** Not the operation id, the dispatch
   binding, or the scope tuple — those stay in the diagnostic receipt tier the
   inspector already has.

## Setup

A project with an accepted Hypothesis, a deterministic Work agent that writes one
observable repository file and returns an answer, and a pending human-gated
removal Proposal for that Hypothesis.

## Drive — proposal

1. Attempt a dispatch the profile does not permit. Read what the human is shown
   at the point of the attempt, then look for a task row, a usage entry, and a
   scratch folder.
2. Dispatch a legitimate Work turn. Let it write its file and return its answer,
   then hold its Patch immediately before Apply.
3. While it is held, approve the removal Proposal so the Hypothesis is gone.
4. Release the turn and let Apply refuse it.
5. Open the task in the Agent tasks drawer. Read its state and its explanation.
6. Open the same task from Runs.
7. Look for a Retry control.
8. Read the repository file and the answer.
9. Open the task's diagnostic receipt.
10. Sync, then reopen the project and read the task again.

## Assert — proposal

- `a_refused_dispatch_is_reported_where_the_human_asked_for_it`
- `a_refused_dispatch_leaves_no_task_row_usage_entry_or_scratch`
- `a_refused_apply_is_not_reported_as_a_failure`
- `the_explanation_names_the_authorizer_the_profile_and_the_action`
- `the_explanation_says_the_repository_effect_and_answer_still_stand`
- `the_explanation_uses_no_internal_action_id_binding_name_or_scope_tuple`
- `the_answer_is_still_readable_after_the_refusal`
- `the_repository_file_written_before_the_refusal_is_untouched`
- `a_refused_apply_offers_no_retry`
- `the_internal_diagnostic_remains_available_in_the_receipt_tier`
- `the_same_explanation_appears_in_runs_and_in_the_agent_task_inspector`
- `the_refusal_survives_a_project_reopen`
- `no_refusal_record_enters_canonical_history`

## UI path (proposal)

**Where.** The Agent tasks drawer and the Runs run detail. No new destination,
no banner, no toast.

**The card.** A refused task carries the state word itself — *Refused* — in the
same position every other task state occupies, with the state rail color RCP
already uses for needs-attention rather than for failure. No caption beneath it.

**The explanation.** An explicit control on the card opens the read-only
inspector RCP already has for a task. It holds four things in ordinary language:
who authorized the work, which agent ran it, what the agent tried to change and
why RCP would not let it, and what still stands. The last one is a sentence, not
a warning: the files it wrote and the answer it gave are unchanged, and nothing
was undone.

**Deliberately not possible.** No Retry on a refused Apply — the operational work
already happened and must not run twice. No "force apply" anywhere. No refusal
appears in Inbox: Inbox is human authority over Proposals, and a refusal is not
a decision anyone is being asked to make.

**Not in this scenario.** Refusals arising from project membership, which does
not exist. When it lands, the membership case is added here rather than written
fresh.

## Boundary

This scenario changes how a refusal is *told*, not when one happens. The gates,
their order, and what they refuse are settled in
[S100](S100-permission-is-checked-twice.md) and unchanged here.

A refusal is operational, not canonical. It appears in SQLite and the UI, never
in `.research/`. Deleting the project deletes every refusal record, and replay
neither reads nor needs one.
