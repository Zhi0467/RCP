---
id: S76-graph-condition-wake
status: pending — not human-confirmed
tier: hermetic
driver: pytest
covered_by: none
invariants: [8, 10g]
---

# An agent can wait on the graph

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[the wake handoff](../handoffs/handoff-2026-08-07-graph-condition-wake.md).

An agent that arms a graph condition sleeps until that fact becomes canonical,
then wakes exactly once. Nothing about this promise lives in the browser: it is
arming, firing, restart recovery, and failing closed.

## Setup

A project with an Experiment loop, an open Blocker, and a pending
Hypothesis-status Proposal. A fake provider that writes a `watch.json` carrying
both an external watcher and a graph condition.

## Drive — proposal

1. Run one loop invocation whose handoff arms a graph condition on the Blocker
   reaching `resolved`, alongside an external watcher.
2. Apply an unrelated patch. Confirm nothing wakes.
3. Sync a patch that resolves the Blocker. Confirm exactly one wake, naming the
   condition that fired.
4. Repeat with RCP stopped: satisfy the condition, then start RCP and confirm
   the wake still happens.
5. Halt replay on a bad revision, then satisfy a condition, and confirm no wake.
6. Complete an external watcher and a graph condition together.

## Assert

- `graph_condition_wakes_the_arming_conversation_exactly_once`
- `unrelated_canonical_movement_does_not_fire_a_condition`
- `condition_satisfied_while_rcp_was_down_fires_at_startup`
- `degraded_or_halted_replay_never_fires_a_graph_condition`
- `condition_on_a_removed_node_is_terminally_retired`
- `every_graph_wake_spends_one_budget_unit_including_after_a_human_sync`
- `external_and_graph_completions_coalesce_into_one_wake`
- `watch_json_carries_two_named_lists_validated_all_or_none`
- `both_lists_empty_requires_a_success_proposal_or_blocker_in_the_same_patch`

## Boundary

The waker and the wakee are the **same conversation**. This scenario promises no
addressing, no cross-agent delivery, and no orchestrator.

Only two conditions exist: a node reaching a status, and a Proposal being
resolved. A graph condition fires on canonical state only — never on a staged
but unsynced draft.

`watch-graph` on the staged agent client is orchestrator-only and is out of
scope here; Experiment loops arm by file.
