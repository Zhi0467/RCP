---
id: S78-one-budget-one-stop
status: implemented
tier: hermetic
driver: browser
covered_by:
  - tests/test_campaign_lifecycle_acceptance.py
  - tests/test_campaign_recovery.py
  - tests/test_campaign_storage.py
  - tests/test_campaign_api.py
  - tests/test_campaign_background.py
  - tests/test_acceptance_agent.py
  - web/tests/campaigns.test.mjs
invariants: [8, 10g]
last_passed: 2026-08-12 — isolated acceptance-agent browser drive covered active,
  automatic and exact recovery, exhaustion, reauthorization, Stop, failure,
  nested turns and mail, and opened every ending report
---

# One budget, one stop

Confirmed 2026-08-12. A campaign spends from one pot, and stopping it is
graceful. Its sibling [S77](S77-auto-research-stops-at-belief.md) owns what the
orchestrator may change; the lifecycle this one protects is settled in
[the orchestrator handoff](../handoffs/handoff-2026-08-07-orchestrator.md).

## The surface, decided 2026-08-12

This scenario's UI path was previously the least settled part of the whole
program. Three decisions closed most of it.

- **Auto-research starts from the project header, beside Ask.** The action is
  project-wide and it lives where project-wide actions live. A button on one
  question would misstate its own reach.
- **The budget is typed in invocations, with observed cost shown beside it.** The
  enforced ceiling stays exactly `invocation_ceiling`; the existing usage ledger
  supplies what has actually been spent. Two numbers, one of them exact and the
  other honest.
- **The report is durable and produced on every ending** — completion,
  exhaustion, Stop, and failure alike.

- **The human may type a starting instruction** when authorizing the campaign.
  Optional, ordinary prose, and carrying no authority — it exists so the
  orchestrator's first paid invocation goes on research rather than on choosing
  where to begin, which is what starting from the header would otherwise cost.
- **A missing or invalid report is a correction, not a verdict.** It goes back to
  the same session with the diagnostic, under the bounded in-session correction
  ladder the Patch path already uses.
- **One invocation is reserved for the wrap-up**, because a report is required on
  every ending and exhaustion is an ending. Without it, running out of budget
  would be the one outcome unable to explain itself.
- **The Settings default is 10 invocations.** It includes the one reserved report
  turn.
- **Normal completion is explicit.** The orchestrator uses an idempotent staged
  `finish` command; RCP never infers completion from quiescence.
- **Wrap-up is the concluding orchestrator turn.** It waits for every
  already-admitted child to settle, resumes the sole orchestrator's exact native
  session and stage, stages the required official `campaign-report` skill, and
  accepts exactly `campaign-report.html`. Invalid HTML is corrected in that same
  allocation and session.
- **Human controls stay on the campaign parent.** A structured action table,
  derived from durable campaign and orchestrator state like Experiment control,
  recommends the safe campaign-level action. No individual worker control is
  exposed.
- **Only an unrecoverable orchestrator failure ends the campaign.** Ordinary
  provider/network/session-limit failures remain recoverable; worker failures
  remain visible work for the orchestrator. A terminal orchestrator failure
  fences admissions, stops watchers with the campaign Stop semantics, retains
  pending mail, and produces a partial report.

## Runs projection — confirmed 2026-08-12

The campaign parent answers only two human questions: **how is auto-research
doing?** and **what should I do next?** Its compact row and expanded detail derive
the same single campaign health and recommendation. The expanded detail renders
them as two distinct projected views: exactly one **Campaign health** and exactly
one separately labelled **Recommended next step**. The compact row carries that
same recommendation. Raw `campaign.status`, the current control task's status or
phase, and worker status are not peer campaign-level answers. Task and worker
rows retain their exact statuses and diagnostics as supporting history.

| Durable condition | Campaign health | Recommended next step | Available parent controls |
|---|---|---|---|
| Queued or starting | Starting | Wait for auto-research to start | Stop only when `can_stop` |
| A campaign turn is healthy and live | Active | Let auto-research continue | Pause and Stop only when each is valid |
| Automatic recovery is pending | Recovering | Wait for automatic recovery | No duplicate manual Retry |
| The current campaign control can Resume or Retry | Needs action | Use the valid Resume or Retry recovery | The matching valid recovery control |
| Stop is settling | Stopping gracefully | Wait for current work to finish | No Stop control |
| Wrap-up is healthy | Writing report | Wait for the concluding report | No Stop control |
| The invocation budget is exhausted | Needs action | Reauthorize auto-research | Reauthorize |
| The campaign ended and has its report | Completed, Stopped, or Failed | Open the concluding report | The matching report control |

Pause and Stop are optional controls gated by validity, not recommendations for
a healthy campaign. The expanded detail keeps its **Campaign health** and
**Recommended next step** views separate. The compact row carries that same
recommendation instead of exposing a second status vocabulary. A recommendation
never names an action whose control is unavailable.

## Setup

A project with two ready Experiments and an open Blocker, a fake provider that
runs long enough to be observed mid-turn, and a campaign budget small enough to
exhaust during the drive.

## Drive

1. Start auto-research from the project header and set a budget in invocations.
2. Open **Runs** and find the campaign as a parent row, its workers nested
   beneath it, and one budget meter on the parent showing both the invocation
   ceiling and the cost observed so far.
3. While the campaign is healthy and active, confirm its compact row and detail
   derive the same one health and **Let auto-research continue** recommendation.
   In the expanded detail they appear as two distinct views: **Campaign health**
   and the separately labelled **Recommended next step**. Task status, task
   phase, and worker status appear only in supporting history. Pause and Stop
   appear only when valid and neither replaces the recommendation.
4. Watch orchestrator turns, worker turns, and wakes each draw from that meter.
5. Observe automatic recovery and confirm the recommendation is to wait, with no
   duplicate manual Retry. Then interrupt a turn after it spawned a worker and
   confirm the actionable exact recovery recommends the valid Resume or Retry.
6. Send a message to the orchestrator and confirm no control exists for
   messaging a worker directly.
7. Let the budget run out while work is in flight. Confirm the campaign
   recommends reauthorization, then reauthorize.
8. Press **Stop** while a turn is active and confirm settling recommends waiting,
   with no second Stop control.
9. Attempt to start a second campaign while the first is live.
10. During healthy wrap-up, confirm the recommendation is to wait for the
    concluding report. Once terminal, confirm it changes to opening the report,
    then open the detailed HTML wrap-up from the campaign row.
11. Do the same for a campaign that exhausted its budget, one that was stopped,
    and one that failed.

## Assert

- `every_orchestrator_worker_and_wake_turn_spends_from_one_campaign_pot`
- `budget_is_one_number_for_the_campaign_not_per_worker`
- `the_budget_is_set_in_invocations_and_shows_observed_cost_beside_it`
- `an_optional_starting_instruction_reaches_the_first_turn_and_grants_no_authority`
- `every_ending_produces_a_durable_report_including_exhaustion_stop_and_failure`
- `a_report_for_an_unclean_ending_reads_as_partial_not_as_a_tidy_summary`
- `an_exhausted_campaign_still_has_the_reserved_invocation_to_report_with`
- `a_missing_or_invalid_report_is_corrected_in_session_not_failed`
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
- `campaign_wrapup_waits_for_every_already_admitted_child`
- `campaign_wrapup_resumes_the_exact_orchestrator_session_and_stage`
- `normal_completion_requires_the_idempotent_finish_command`
- `campaign_controls_are_derived_from_state_and_never_attached_to_a_worker`
- `campaign_parent_shows_one_health_and_one_recommendation`
- `compact_and_expanded_campaign_views_share_the_same_projection`
- `campaign_health_and_recommended_next_step_are_separate_projected_views`
- `raw_campaign_task_phase_and_worker_status_are_supporting_history_not_peer_states`
- `healthy_active_campaign_recommends_let_auto_research_continue`
- `a_healthy_campaign_never_recommends_pause_just_because_pause_is_available`
- `pause_and_stop_are_optional_controls_gated_by_validity_not_recommendations`
- `automatic_recovery_recommends_wait_without_duplicate_retry`
- `actionable_exact_recovery_recommends_resume_or_retry`
- `exhaustion_recommends_reauthorization`
- `stopping_and_wrapup_recommend_waiting`
- `terminal_campaign_with_report_recommends_opening_it`
- `expanded_campaign_detail_has_one_health_and_one_distinct_recommendation_view`
- `a_campaign_recommendation_never_names_an_unavailable_control`
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

The report renders through the existing sandboxed HTML boundary; a campaign
document is not a reason to invent an unrestricted one.

Real-time streaming and worker-to-worker mail are out of scope, deferred as
[Q8 and Q9](../open-questions.md).
