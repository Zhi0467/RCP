---
id: S53-truthful-attention-and-run-surfaces
status: implemented
tier: hermetic
driver: browser
covered_by:
  - tests/test_api.py
  - tests/test_service_contracts.py
  - tests/test_transition_api.py
  - tests/test_transition_control_projection.py
  - tests/test_experiment_index.py
  - web/tests/attentionRunsOntology.test.mjs
  - web/tests/decisionChoice.test.mjs
  - web/tests/experimentRunDetail.test.mjs
  - web/tests/runDialog.test.mjs
  - web/tests/runProjection.test.mjs
  - web/tests/spaceRuns.test.mjs
  - web/tests/transitionAppIntegration.test.mjs
  - web/tests/transitionPresentation.test.mjs
invariants: [3, 10]
reported_by: human, 2026-08-03
last_passed: 2026-08-24 — served an isolated demo project, staged a Decision
  transition through backend preview, and observed the exact candidate Inbox
  membership and Experiment gate with a clean browser console and server log;
  the complete attention and task-surface behavior remains covered by the
  listed browser and regression checks
---

# Attention and run surfaces tell one truthful story

This implemented scenario owns the current **graph-attention** section. The
confirmed team-space design later adds project-membership invitations as a
separate non-graph Inbox item. That addition does not widen the node/status
predicates or change the three graph-attention tiles asserted here.

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

Runs is the episode ledger: Experiment-loop and Auto-research episode parents
belong there. Invocation rows, ingestion, and graph Blockers remain inspectable
in their owning History, episode detail, or Inbox surfaces without becoming the
primary Runs objects. A task status remains explicit through its label and icon,
but task surfaces do not show an estimated progress bar, percentage, or ETA.

## UI path

1. Open a project with a pending Hypothesis-status Proposal, Decisions in
   `open`, `ready`, and `revisit`, asserted open Blockers of multiple subtypes,
   accepted and contested open Blockers, a historical Ambiguity, failed chat
   tasks, and at least one Seed or Refresh task.
2. Compare the project card, Inbox badge, Inbox heading, and the three Inbox
   tiles.
3. Open the `ready` Decision row. Confirm its node card opens and choose an
   option in the existing inspector ballot. Close and reopen the card, then
   reload the page. The staged choice remains visible. The backend preview shows
   the candidate Decision as decided and removes it from candidate attention,
   while canonical state remains unchanged until Sync.
4. Sync the choice. The Decision remains outside every attention count and is
   readable as decided in its node card.
5. Change the `revisit` Decision to `open` with the ordinary node editor and
   Sync. It leaves attention without recording a new choice.
6. Agree with one asserted open Blocker, contest another, and Sync. Confirm both
   leave human attention without changing their independent operational status.
7. Resolve one judged Blocker and Sync, then reopen it and Sync. Confirm the
   lifecycle edit resets its standing to `asserted`; the resolved Blocker stays
   outside attention and the reopened one returns. While staged, confirm the
   backend preview already shows the rule-complete final state. After Sync,
   confirm the Blocker remains in canonical detail/history but disappears from
   the active Research flow, no longer gates its Experiment, and any affected
   summary or next action is explicitly stale rather than shown as current.
8. Open Runs with active and completed episodes of both modes, then project
   History, then inspect a terminal task. Return to the space project index and
   inspect its Runs ledger across projects.

## Assert

- The project card, Inbox badge, Inbox heading, and Inbox tile total all use
  `pending proposals + decisions awaiting choice + asserted open blockers`.
- The Inbox tiles are **Pending proposals**, **Decisions awaiting choice**, and
  **Blockers awaiting judgment**; their values sum to the heading.
- Only `ready` and `revisit` Decisions appear. A Decision row shows its title
  plus a Ready/Revisit chip and opens the existing node card; it has no inline
  ballot, options preview, or explanatory caption.
- A staged Decision choice survives card close, reopen, and page reload. Its
  backend preview supplies one coherent candidate graph and attention
  projection, so the candidate decided node leaves candidate attention before
  Sync without changing canonical state. Sync commits it through the existing
  direct-choice authority path.
- A human `ready` or `revisit` to `open` edit is the explicit "not yet" path and
  removes the Decision from attention after Sync.
- Legacy Ambiguities appear nowhere and contribute to no attention count.
- Agreeing with or contesting an asserted open Blocker and syncing removes it
  from human attention without changing its `open` status or its effect on
  research readiness. Blockers never become episode rows in Runs.
- A staged Blocker judgment follows the same candidate rule: backend preview
  removes the judged candidate from candidate attention, while canonical state
  and canonical attention remain unchanged until Sync.
- The Blocker editor offers Open, Resolved, and Superseded. Human and agent
  lifecycle edits reset accepted or contested standing to asserted. Resolved and
  superseded Blockers remain outside attention; reopening one returns it.
- A resolving Sync commits one transition or nothing. Its returned graph,
  Experiment control, guidance validity, and head identify the same revision.
  The resolved Blocker and relations remain canonical, while active views omit
  it, only `open` gates, and stale guidance is never presented as current.
- Runs has exactly **Needs Action** and **Completed**, in that order, and every
  row is an Experiment-loop or Auto-research episode parent.
- Needs Action mixes both episode modes in reverse chronological order and is
  never folded as a section. Completed groups episodes in foldable lists ordered
  **Experiment loop** then **Auto-research**.
- Each card leads with the owning Experiment name or Auto-research identity;
  start time is secondary metadata with no `Episode` prefix. Completed groups
  name the mode once, and collapsed cards have no muted recommendation or report
  commentary. One Experiment node contributes only the current episode named by
  its backend control. Older episodes stay in project History rather than
  duplicating that Experiment in Runs. An Experiment card's section and health
  use that same control projection as its expanded detail, even when the generic
  episode lifecycle projection differs.
- Seed, Refresh, Blockers, node chat, project chat, and paper-coach tasks do not
  become Runs rows. They remain reachable in Inbox, project History, episode
  detail, and the Agent task inspector as applicable.
- The space index Runs ledger mixes current Experiment-loop and Auto-research
  parents under the same Needs Action and folded Completed grammar, carries the
  owning project and exact Experiment route, and uses backend lifecycle answers.
  Each Auto-research row likewise opens its exact durable episode, including a
  completed or non-leading parent.
  Completed parents leave this space-only surface after seven days; episode
  records, project Runs, and project History remain unchanged.
- Active, failed, succeeded, interrupted, and paused tasks show no progress bar,
  percentage, or ETA in the task inspector.
- No console, network, or server error occurs.

## Failure means

Two counters disagree about what needs attention; a plain open Decision enters
the Inbox; a ripe Decision cannot reach the existing ballot; a staged choice
vanishes, changes canonical state before Sync, or disagrees with its candidate
attention projection; a historical Ambiguity renders or counts; a judged
Blocker keeps asking for human attention after the judgment is canonical;
accepting or contesting one silently resolves it; or completed work still looks
live.
