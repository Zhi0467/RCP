---
id: S53-truthful-attention-and-run-surfaces
status: implemented
tier: hermetic
driver: browser
covered_by: tests/test_api.py, web/tests/attentionRunsOntology.test.mjs,
  web/tests/agentTaskProgress.test.mjs, browser 2026-08-06
invariants: [10]
reported_by: human, 2026-08-03
last_passed: 2026-08-06 — isolated browser drive covered staged and synced Agree,
  Contest, Resolve, and Reopen across Inbox, Runs, DAG, and lifecycle editing
---

# Attention and run surfaces tell one truthful story

Updated and confirmed by the human on 2026-08-06.

Human attention is awaiting a human judgment, not every unresolved condition in
the research graph. Every attention count includes pending Proposals, open
Ambiguities, and open Blockers whose standing remains `asserted`, regardless of
Blocker subtype. After Sync records either **Agree** or **Contest**, that Blocker
leaves human-attention surfaces without changing its independent operational
status. It remains in the graph, and `status="open"` continues to block related
research until a later graph update resolves or supersedes it. The Blocker node
editor exposes that lifecycle status directly to the human; graph-capable agents
may update it directly too, without creating a Proposal.

Runs is the research execution surface: ingestion runs, experiments, and graph
Blockers awaiting human judgment belong there; conversation and paper-coach
tasks remain inspectable in project History and the Agent task inspector without
becoming research runs.

A task status remains explicit through its label and icon, but task surfaces do
not show an estimated progress bar, percentage, or ETA.

## UI path

1. Open a project with pending Proposals, open Ambiguities, asserted open
   Blockers of multiple subtypes, accepted and contested open Blockers, failed
   chat tasks, and at least one Seed or Refresh task.
2. Compare the project card, Inbox badge, Inbox heading, and the three Inbox
   tiles.
3. Agree with one asserted open Blocker, contest another, and Sync. Confirm both
   remain in human attention while the judgments are staged, then leave the
   project card attention count, Inbox, and Runs **Needs action** after Sync while
   remaining open and visible in the research graph.
4. Edit one judged Blocker, change its Status from Open to Resolved, and Sync.
   Confirm the lifecycle change is explicit, resets the prior standing to
   asserted, and does not create a Proposal. Because the Blocker is resolved, it
   remains outside human attention.
5. Change that Blocker back to Open and Sync. It is now asserted and returns to
   human attention for a fresh judgment.
6. Open Runs, then project History, then inspect a terminal task.

## Assert

- The project card, Inbox badge, and Inbox heading use
  `pending proposals + open ambiguities + asserted open blockers`.
- The Inbox tiles are Pending proposals, Open ambiguities, and Blockers awaiting
  judgment; their values sum to the heading.
- Agreeing with or contesting an asserted open Blocker and syncing removes it
  from every human-attention count and from Runs **Needs action**, without
  changing its `open` status or its effect on research readiness.
- A staged judgment does not remove the Blocker early; until Sync makes the
  judgment canonical, it remains available in the same attention surfaces like
  a Proposal with a staged decision.
- The Blocker editor offers the closed lifecycle choices Open, Resolved, and
  Superseded. Sync applies the selected status as a direct human graph edit.
- A graph-capable agent may directly update Blocker status; unlike governed
  Decision and evidence-grounded Hypothesis transitions, this does not create or
  require a Proposal.
- A human or agent lifecycle edit resets accepted or contested standing to
  asserted because the judged record changed. Resolved and superseded Blockers
  remain outside attention; reopening one returns it for a fresh judgment.
- Runs includes Seed and Refresh tasks, experiments, and asserted open blockers,
  but excludes node chat, project chat, and paper-coach tasks.
- Every excluded chat task remains reachable in project History and the Agent
  task inspector.
- Active, failed, succeeded, interrupted, and paused tasks show no progress bar,
  percentage, or ETA in the task inspector.
- No console, network, or server error occurs.

## Failure means

Two counters disagree about what needs attention, a judged Blocker keeps asking
for human attention, accepting or contesting one silently resolves it, the human
cannot explicitly edit its lifecycle, ordinary Blocker resolution is forced
through a Proposal, internal conversation failure text becomes the headline of
research execution, or completed work still looks live.
