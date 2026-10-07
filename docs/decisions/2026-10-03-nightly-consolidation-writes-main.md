# Nightly graph consolidation writes main

**Status:** accepted with the human on 2026-10-02. Current behavior is in
[graph consolidation and lessons](../specs/graph-consolidation-and-lessons.md).

## Decision

A project member may authorize a nightly graph consolidation. Once a day, at the
member's chosen local time, RCP starts one ordinary Work turn in the project's
dedicated consolidation chat. The turn audits the main graph, consolidates it
under ordinary Work authority, applies its Patch inside the turn, and writes one
HTML report. The report and any failure appear in the Inbox.

The authorization names the member and expires. A night with no new main
revision since the last consolidation starts nothing. A missed time runs once
when RCP next can.

## Why a scheduled launch is allowed at all

Before this, RCP scheduled autonomous work only inside a human-authorized
Auto-research episode. Consolidation is the "dreaming" phase of agent memory:
the graph is the project's memory, and it decays when nobody tidies it. Humans
do not remember to tidy. A standing authorization makes the human decision
once, and the expiry makes it lapse unless renewed.

Each run is one bounded Work turn, so the budget is one invocation a night. A
new episode kind with its own budget, Stop fence, and report lifecycle would
cost far more than the work it bounds.

## Why main, not a branch

A branch would need a new branch kind and a human-dispatched merge every
morning. Consolidation edits are what an ordinary Work turn may already make on
main: merging and superseding ordinary nodes, editing text, relations, and
glossary. A change to an existing ResearchQuestion or Hypothesis is still a
Proposal, and an accepted node still cannot be removed directly. History is
append-only, so every edit stays inspectable and reversible by a later Patch.

An open Auto-research or Experiment branch that touched the same nodes is not
locked out. Its semantic merge already routes conflicting nodes to the merge
agent's residue, so consolidation adds no new conflict path.

## Why full Work authority

The human chose not to narrow the turn's operation set. The workflow tells the
agent to consolidate rather than invent, and the report lists every change. A
narrower code-enforced profile remains possible later without changing the
launch, report, or Inbox contracts.

## Why the turn applies its own Patch

The report must describe what actually changed. Applying inside the turn
returns the committed revision before the agent writes the report, as the
Auto-research orchestrator already does. The consolidation owner is the second
owner granted keyed `apply`; the grant is named in code for that owner only.

## Why the report sits in the Inbox

The Inbox has held only items awaiting judgment. A nightly report is the first
item that only needs reading, but it does ask for one act: Keep it with the
project's artifacts or Dismiss it. A failure row can only be dismissed. Neither
row grants authority; Proposals the turn raised appear in the Inbox as usual.
