---
id: S41-bounded-experiment-control
status: pending
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [3, 4, 4b, 10, 10b]
---

# Run an experiment through a bounded control loop

An Experiment node can turn its graph preconditions into a bounded sequence of
Work turns. The graph decides whether **Run** is available; the human authorizes
each new spend envelope; the agent executes and records attempts inside it; and
the human remains the only authority that accepts evidence or decides an upstream
choice.

Experiment attempts and generic watchers are separate records. This scenario
uses the watcher delivery promised by S42 to continue the conversation, but a
watcher never creates, closes, counts, or identifies an attempt.
Provider task attempts created by Pause, Resume, or Retry are also separate and
never spend the experiment's ceiling.

## UI path

Confirmed by the human on 2026-08-01.

- Open an Experiment node whose governing decision is decided, whose blockers
  are closed, and whose attempt count is below its ceiling. The node shows a live
  **Run** action and its attempt budget.
- Open otherwise identical nodes with an open decision, a pending proposal, an
  open blocker, or an exhausted attempt ceiling. **Run** is disabled and the
  exact gating reason is visible. Ordinary Work conversation remains available.
- Press **Run**. RCP opens a Work turn with the governing decision bundle pinned.
  No attempt record exists merely because the button was pressed. While that
  loop operation or a nonterminal attempt exists, a second **Run** is refused;
  ordinary Work remains available.
- Let bounded preflight fail once and succeed after an in-turn repair. No attempt
  is spent by the failed preflight. Separately, drive a provider failure before
  launch and use the existing task Retry; it also spends no attempt.
- Submit the external work and have that Work turn's loop patch append attempt 1.
  After collection, the experiment log shows the pinned decisions and, for a
  debug retry, the fault, change, and predicted mechanical effect written before
  launch.
- Deliver its completion through S42. The attributed watcher turn reads the log
  and closes the attempt. A mechanical failure below the ceiling may create the
  next attempt; a disappointing scientific result may not be relabelled as a
  mechanical fault merely to retry it.
- Drive a turn that needs a changed upstream decision. It appends a proposal-only
  attempt, sends the proposal to Inbox, and turns the gate red. Approval does not
  resume spending automatically; the human presses **Run** again.
- Reach the configured ceiling. The final watcher turn may inspect artifacts and
  write evidence, a blocker, or a proposal, but its visible contract says that no
  further long-running launch is allowed. **Run** remains disabled. Raise the
  positive integer `attempt_ceiling` in the Experiment editor and press **Sync**;
  only then does the new canonical budget make **Run** available again.
- Complete an experiment successfully. The loop asserts the Evidence node and
  evidence edge, then produces exactly one Inbox item for the Hypothesis status
  transition with that same-patch edge as its cause. If optional
  `completion_criteria` exists, the pinned criterion is shown with the proposed
  belief change but never controlled the loop. Only human acceptance updates the
  downstream belief; edges have no standing.
- While the loop is active, edit the repository manually through ordinary Work.
  RCP shows an advisory active-loop marker but neither locks the repository nor
  blocks the human action.

## Assertions

- `readiness_is_derived_from_decisions_proposals_blockers_and_ceiling`
- `run_is_disabled_with_the_exact_gate_reason`
- `ordinary_work_remains_available_when_run_is_disabled`
- `run_pins_the_governing_decision_bundle`
- `pressing_run_alone_does_not_create_an_attempt`
- `one_experiment_cannot_start_a_second_active_loop`
- `preflight_repair_stays_inside_one_work_turn`
- `provider_failure_before_launch_retries_without_spending_an_attempt`
- `provider_task_attempt_lineage_never_spends_the_experiment_ceiling`
- `successful_launch_appends_one_attempt_record`
- `one_loop_patch_appends_at_most_one_attempt_record`
- `old_attempts_load_with_backward_compatible_control_defaults`
- `attempt_records_pin_a_strict_decision_bundle`
- `proposal_only_attempts_are_explicitly_typed`
- `debug_retry_records_fault_change_and_prediction_before_launch`
- `scientific_disappointment_is_not_a_retry_classification`
- `proposal_only_iteration_consumes_one_attempt_and_gates_run`
- `proposal_resolution_requires_another_human_run_action`
- `ceiling_context_forbids_another_long_running_launch_by_prompt_contract`
- `only_a_synced_positive_human_attempt_ceiling_reopens_run`
- `completion_criteria_is_pinned_advisory_and_visible_at_acceptance`
- `run_uses_the_experiment_loop_patch_kind`
- `control_watcher_wake_retains_the_experiment_loop_patch_policy`
- `loop_patch_cannot_decide_set_standing_or_edit_the_pinned_bundle`
- `successful_operational_work_survives_a_rejected_graph_reflection`
- `successful_completion_produces_one_human_authority_item`
- `human_acceptance_is_required_for_the_evidence_grounded_belief_change`
- `active_loop_marker_derives_from_control_work_or_nonterminal_attempts`
- `generic_watchers_never_define_the_active_loop_marker`
- `active_loop_state_only_suppresses_duplicate_run_not_ordinary_work`
- `watcher_records_never_count_or_mutate_attempts`
- `no_console_failed_request_or_server_error_during_the_browser_drive`

## Deliberately not possible in v1

A graph-wide research scheduler, silent automatic restart after a proposal,
agent acceptance of its own evidence, a hard repository lease, RCP-owned job
submission, or a mechanical shell-command parser enforcing the attempt ceiling.

## Failure means

RCP spends compute while a graph precondition is red, counts setup retries as
experiments, lets the loop evade its budget, converts a scientific result into a
debug excuse, treats a watcher as an attempt, or changes belief without the
human's explicit acceptance.
