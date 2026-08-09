---
id: S78-one-budget-one-stop
status: pending — not human-confirmed
tier: hermetic
driver: browser
covered_by: none
invariants: [8, 10g]
---

# One budget, one stop

This scenario is a proposal and is **not yet human-confirmed**, and its **UI
path is the least settled part of the whole program** — the human and agent have
not discussed the auto-research entry point, the campaign row, or the budget
display in enough detail. The campaign's required detailed HTML wrap-up is also
confirmed only in direction; its terminal-state, durability, and failure
semantics still need grilling. Treat the drive below as a sketch to be agreed,
not a specification. The lifecycle it protects is settled in
[the orchestrator handoff](../handoffs/handoff-2026-08-07-orchestrator.md).

A campaign spends from one pot, and stopping it is graceful. Its sibling
[S77](S77-auto-research-stops-at-belief.md) owns what the orchestrator may
change.

## Setup

A project with two ready Experiments and an open Blocker, a fake provider that
runs long enough to be observed mid-turn, and a campaign budget small enough to
exhaust during the drive.

## Drive — proposal

1. Start auto-research and set a budget.
2. Open **Runs** and find the campaign as a parent row, its workers nested
   beneath it, and one budget meter on the parent.
3. Watch orchestrator turns, worker turns, and wakes each draw from that meter.
4. Send a message to the orchestrator and confirm no control exists for
   messaging a worker directly.
5. Let the budget run out while work is in flight.
6. Reauthorize, then press **Stop** while a turn is active.
7. Attempt to start a second campaign while the first is live.
8. Interrupt a turn after it spawned a worker, then Retry it.
9. Complete a campaign and open its detailed HTML wrap-up from the campaign row.

## Assert

- `every_orchestrator_worker_and_wake_turn_spends_from_one_campaign_pot`
- `budget_is_one_number_for_the_campaign_not_per_worker`
- `exhaustion_leaves_the_campaign_in_needs_action_rather_than_failed`
- `exhaustion_lets_current_turns_finish_and_starts_nothing_new`
- `stop_finishes_the_current_turn_without_killing_external_work`
- `stop_never_discards_a_valid_patch`
- `stop_is_idempotent_durable_and_survives_restart`
- `a_second_campaign_cannot_start_while_one_is_live`
- `spawn_with_an_already_recorded_key_returns_the_existing_worker`
- `spawn_with_an_already_recorded_key_never_restarts_that_worker`
- `an_interrupted_call_with_no_recorded_exit_is_reconciled_against_live_state`
- `every_client_invocation_appears_in_the_task_event_stream`
- `the_human_can_message_the_orchestrator_but_not_a_worker`
- `campaign_wrapup_invokes_the_versioned_rcp_report_skill`
- `the_campaign_row_exposes_the_detailed_html_report`
- `the_report_covers_decisions_blockers_experiments_evidence_and_epistemic_proposals`
- `campaign_reporting_does_not_widen_graph_inbox_membership`

## Boundary

**Stop** reuses the existing Stop-loop semantics exactly: it does not cancel the
current task, kill external work, delete a watcher, edit an Experiment's status,
or discard a valid Patch. Retry and Resume of the already-authorized turn can
never clear the stop intent.

Authority — what the orchestrator may change in the graph — is
[S77](S77-auto-research-stops-at-belief.md), and should not be re-asserted here.

This proposal does not yet decide whether exhaustion, Stop, or failure must
produce a partial report; whether report bytes are durable or regenerable; or
how missing/invalid HTML affects the campaign verdict. Confirm those details
before implementing the report assertions above.

Real-time streaming and worker-to-worker mail are out of scope, deferred as
[Q8 and Q9](../open-questions.md).
