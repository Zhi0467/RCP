---
id: S53-truthful-attention-and-run-surfaces
status: implemented
tier: hermetic
driver: browser
covered_by:
  - tests/test_api.py
  - web/tests/attentionRunsOntology.test.mjs
  - web/tests/decisionChoice.test.mjs
  - browser 2026-08-08
invariants: [3, 10]
reported_by: human, 2026-08-03
last_passed: 2026-08-08 — isolated acceptance-agent browser drive covered all
  attention counts, Decision staging and Sync, Blocker judgment and lifecycle,
  Runs, project History, and a terminal task inspector with no console or server errors
---

# Attention and run surfaces tell one truthful story

Rewritten and confirmed by the human on 2026-08-08.

Human attention is awaiting a human act, not every unresolved condition in the
research graph. Every attention count includes pending Hypothesis-status
Proposals, Decisions whose status is `ready` or `revisit`, and open Blockers
whose standing remains `asserted`, regardless of Blocker subtype. Plain `open`
Decisions are framed but not yet choosable and do not enter the Inbox.

A Decision awaiting choice is a node, so its Inbox row shows only its title and
a **Ready** or **Revisit** state chip and opens the existing node card. The
existing inspector ballot is the only place a choice is made. A Proposal is not
a node and remains actionable inline. An asserted open Blocker remains a claim
to **Agree** with or **Contest**.

Runs is the research execution surface: ingestion runs, experiments, and graph
Blockers awaiting human judgment belong there; conversation and paper-coach
tasks remain inspectable in project History and the Agent task inspector without
becoming research runs. A task status remains explicit through its label and
icon, but task surfaces do not show an estimated progress bar, percentage, or
ETA.

## UI path

1. Open a project with a pending Hypothesis-status Proposal, Decisions in
   `open`, `ready`, and `revisit`, asserted open Blockers of multiple subtypes,
   accepted and contested open Blockers, a historical Ambiguity, failed chat
   tasks, and at least one Seed or Refresh task.
2. Compare the project card, Inbox badge, Inbox heading, and the three Inbox
   tiles.
3. Open the `ready` Decision row. Confirm its node card opens and choose an
   option in the existing inspector ballot. Close and reopen the card, then
   reload the page. The staged choice remains visible and the Decision remains
   in attention until Sync.
4. Sync the choice. The Decision leaves every attention count and remains
   readable as decided in its node card.
5. Change the `revisit` Decision to `open` with the ordinary node editor and
   Sync. It leaves attention without recording a new choice.
6. Agree with one asserted open Blocker, contest another, and Sync. Confirm both
   leave human attention without changing their independent operational status.
7. Resolve one judged Blocker and Sync, then reopen it and Sync. Confirm the
   lifecycle edit resets its standing to `asserted`; the resolved Blocker stays
   outside attention and the reopened one returns.
8. Open Runs, then project History, then inspect a terminal task.

## Assert

- The project card, Inbox badge, Inbox heading, and Inbox tile total all use
  `pending proposals + decisions awaiting choice + asserted open blockers`.
- The Inbox tiles are **Pending proposals**, **Decisions awaiting choice**, and
  **Blockers awaiting judgment**; their values sum to the heading.
- Only `ready` and `revisit` Decisions appear. A Decision row shows its title
  plus a Ready/Revisit chip and opens the existing node card; it has no inline
  ballot, options preview, or explanatory caption.
- A staged Decision choice remains in attention and survives card close, reopen,
  and page reload. Sync commits it through the existing direct-choice authority
  path and removes the decided node from attention.
- A human `ready` or `revisit` to `open` edit is the explicit "not yet" path and
  removes the Decision from attention after Sync.
- Legacy Ambiguities appear nowhere and contribute to no attention count.
- Agreeing with or contesting an asserted open Blocker and syncing removes it
  from human attention and Runs **Needs action** without changing its `open`
  status or its effect on research readiness.
- A staged Blocker judgment does not remove it early; until Sync makes the
  judgment canonical, it remains in the same attention surfaces.
- The Blocker editor offers Open, Resolved, and Superseded. Human and agent
  lifecycle edits reset accepted or contested standing to asserted. Resolved and
  superseded Blockers remain outside attention; reopening one returns it.
- Runs includes Seed and Refresh tasks, experiments, and asserted open Blockers,
  but excludes node chat, project chat, and paper-coach tasks. Excluded tasks
  remain reachable in project History and the Agent task inspector.
- Active, failed, succeeded, interrupted, and paused tasks show no progress bar,
  percentage, or ETA in the task inspector.
- No console, network, or server error occurs.

## Failure means

Two counters disagree about what needs attention; a plain open Decision enters
the Inbox; a ripe Decision cannot reach the existing ballot; a staged choice
vanishes or leaves attention early; a historical Ambiguity renders or counts; a
judged Blocker keeps asking for human attention; accepting or contesting one
silently resolves it; or completed work still looks live.
