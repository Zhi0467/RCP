---
id: S76-graph-condition-wake
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_graph_condition_watchers.py
  - tests/test_watchers.py::test_graph_condition_column_migrates_before_its_index_is_created
  - tests/test_experiment_loop_agent_io.py::test_staged_graph_watcher_state_uses_condition_fields_without_shell_telemetry
  - tests/test_prompts.py
  - web/tests/runProjection.test.mjs
  - web/tests/experimentRunDetail.test.mjs
invariants: [8, 10g]
last_passed: 2026-08-12 — focused S76 backend, API, and web checks; full baselines passed before concurrent authority work
---

# An agent can wait on the graph

**Confirmed by the human 2026-08-12**, including the closed two-condition
vocabulary: waiting on a new node attaching, and waiting on a standing change,
were both offered and both declined. Add a third condition only when something
concretely needs it. The design is settled in
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
7. Arm after an older Proposal was resolved while a newer Proposal is pending;
   confirm only the newer Proposal's later resolution wakes the loop.
8. Settle callbacks for two accepted revisions in reverse order; confirm the
   watcher still observes their canonical order.

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
- `initially_satisfied_node_status_condition_is_immediately_ready`
- `proposal_resolution_must_happen_after_arming`
- `accepted_boundaries_are_applied_in_canonical_order`

## Boundary

The waker and the wakee are the **same conversation**. This scenario promises no
addressing, no cross-agent delivery, and no orchestrator.

Only two conditions exist: a node reaching a status, and a Proposal being
resolved. A graph condition fires on canonical state only — never on a staged
but unsynced draft. Each wait is based at its durable arming revision. A node
status already true there is ready immediately; an earlier Proposal resolution
does not satisfy a newly armed wait. Accepted revisions are reconciled in
canonical order even when task callbacks settle out of order.

`watch-graph` on the staged agent client is orchestrator-only and is out of
scope here; Experiment loops arm by file.
