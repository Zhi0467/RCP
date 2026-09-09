# A refusal explains itself, and says what it did not undo

Date: 2026-09-09
Status: not implemented. The human confirmed this work on 2026-08-15 as
acceptance scenario S121; when the acceptance system was retired on 2026-09-09
the confirmed contract moved here unchanged so it would not be lost. The four
decisions below are settled. Nothing has been built: refusals still reach the
human as the raw gate string, and a refused Apply still ends its task as
`failed`.

Close this handoff when a refused Apply reaches the human as `refused`, with
the plain-language explanation described below, in both the Agent task
inspector and the Runs run detail.

## The problem

RCP refuses agent actions correctly and then explains them badly.

A refusal today reaches the human as the string the gate raised, for example
`Authority refused action 'apply': Patch profile does not match the dispatch
binding.` That sentence is written for whoever is reading `core/authority.py`.
It names an internal action id and an internal binding and no person. It does
not say who authorized the work, which agent profile ran it, what the agent was
trying to change, or that the repository writes and the answer are still there
and nothing was rolled back.

The task carrying it ends as `failed`. When Apply refuses a Work patch, the
operational work succeeded: the experiment ran, the files were written, the
answer came back. Only the graph reflection was refused. Calling that "failed"
tells the human the opposite of what happened. The two permission gates in
[authority and proposals](../specs/authority-and-proposals.md#two-permission-gates)
already promise that a refusal is not a retraction; the interface is the half
of that promise nobody built.

## Decided on 2026-08-15

1. **A refused Apply gets its own terminal task state, `refused`.** `failed` is
   what Retry keys off, and retrying a turn whose operational work already
   landed is the one thing that must not be invited. This is a new literal in
   `AgentTaskStatus`, a shared contract, so it lands serially and first, across
   [`storage/models.py`](../../src/rcp/storage/models.py),
   [`web/src/types.ts`](../../web/src/types.ts), and every status projection.
   `ACTIVE_AGENT_TASK_STATUSES` does not change: `refused` is terminal.
2. **The human reads it in the existing Agent task inspector and the Runs run
   detail.** No new destination, banner, or toast. The card carries its state;
   an explicit control opens the read-only explanation.
3. **A refused dispatch leaves no record.** Nothing launched, no budget was
   spent, no scratch exists, and writing a row to describe an absence would
   contradict the gate contract. The refusal is feedback at the point of the
   click and nowhere else.
4. **Provenance is the human authorizer, the agent profile, the action in
   ordinary words, and what still stands.** Not the operation id, the dispatch
   binding, or the scope tuple; those stay in the diagnostic receipt tier the
   inspector already has.

## What the human sees

A refused task carries the word *Refused* in the position every other task
state occupies, using the needs-attention rail color rather than the failure
color, with no caption beneath it. Its explanation holds four things in
ordinary language: who authorized the work, which agent ran it, what the agent
tried to change and why RCP would not let it, and what still stands. The last
is a sentence, not a warning: the files it wrote and the answer it gave are
unchanged, and nothing was undone.

Deliberately not possible: Retry on a refused Apply, any "force apply", and a
refusal appearing in Inbox. Inbox is human authority over Proposals, and a
refusal is not a decision anyone is being asked to make.

## Closure criteria

- A refused dispatch is reported where the human clicked and leaves no task
  row, usage entry, or scratch.
- A refused Apply is not reported as a failure and offers no Retry.
- The explanation names the authorizer, the profile, and the action, and says
  the repository effect and answer still stand, using no internal action id,
  binding name, or scope tuple.
- The answer is still readable and the repository file written before the
  refusal is untouched.
- The internal diagnostic remains available in the receipt tier.
- The same explanation appears in Runs and in the Agent task inspector and
  survives a project reopen.
- No refusal record enters canonical history.

## Boundary

This changes how a refusal is told, not when one happens. The gates, their
order, and what they refuse are settled in the authority spec and unchanged
here. A refusal is operational, not canonical: it lives in SQLite and the UI,
never in `.research/`. Deleting the project deletes every refusal record, and
replay neither reads nor needs one. Refusals arising from project membership
are added here when membership refusals exist, rather than written fresh.
