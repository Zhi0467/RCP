---
id: S53-truthful-attention-and-run-surfaces
status: implemented
tier: hermetic
driver: browser
covered_by: web/tests/attentionRunsOntology.test.mjs, web/tests/agentTaskProgress.test.mjs
invariants: [10]
reported_by: human, 2026-08-03
last_passed: 2026-08-03
---

# Attention and run surfaces tell one truthful story

Confirmed by the human on 2026-08-03.

Every attention count includes every open Blocker, regardless of its subtype.
Runs is the research execution surface: ingestion runs, experiments, and graph
Blockers belong there; conversation and paper-coach tasks remain inspectable in
project History and the Agent task inspector without becoming research runs.

A task status remains explicit through its label and icon, but task surfaces do
not show an estimated progress bar, percentage, or ETA.

## UI path

1. Open a project with pending Proposals, open Ambiguities, open Blockers of
   multiple subtypes, failed chat tasks, and at least one Seed or Refresh task.
2. Compare the project card, Inbox badge, Inbox heading, and the three Inbox
   tiles.
3. Open Runs, then project History, then inspect a terminal task.

## Assert

- The project card, Inbox badge, and Inbox heading use
  `pending proposals + open ambiguities + all open blockers`.
- The Inbox tiles are Pending proposals, Open ambiguities, and Open blockers;
  their values sum to the heading.
- Runs includes Seed and Refresh tasks, experiments, and blockers, but excludes
  node chat, project chat, and paper-coach tasks.
- Every excluded chat task remains reachable in project History and the Agent
  task inspector.
- Active, failed, succeeded, interrupted, and paused tasks show no progress bar,
  percentage, or ETA in the task inspector.
- No console, network, or server error occurs.

## Failure means

Two counters disagree about what needs attention, internal conversation failure
text becomes the headline of research execution, or completed work still looks
live.
