---
id: S41-bounded-experiment-control
status: pending
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_acceptance_experiment_watchers.py::test_s41_ceiling_pauses_then_human_run_starts_a_new_episode_and_exits
  - tests/test_acceptance_agent.py
  - tests/test_api.py::test_human_run_claims_over_ceiling_completion_into_a_new_episode
  - web/tests/acceptanceAgentMode.test.mjs
  - web/tests/experimentControlRefresh.test.mjs
  - web/tests/experimentRunDetail.test.mjs
  - web/tests/runDialog.test.mjs
invariants: [3, 4, 4b, 10, 10b, 10e, 10g]
---

# Run an experiment through a bounded control loop

An Experiment node can turn its graph preconditions into a bounded sequence of
Work turns. The graph decides whether its episode-start action is available;
**Start episode** authorizes the first episode, and **Start new episode**
authorizes every later one. Each episode has a fresh bounded number of agent
invocations; the agent executes and records scientifically meaningful attempts
at its own discretion; and the human remains the only authority that accepts
evidence or decides an upstream choice.

Experiment attempts and generic watchers are separate records. This scenario
uses the watcher machinery promised by S42, with S88's node-owned Experiment
resource, to continue the episode's bound conversation. The
human-set `invocation_ceiling` bounds the initial Run invocation plus attributed
watcher wakes inside one episode. Semantic `ExperimentAttempt` records neither
spend nor reset that budget. The ceiling counts operational loop invocations
only; the shared hidden episode wrap-up never spends or appears in that count.

## UI path

Confirmed by the human on 2026-08-05.

- Open an Experiment node whose governing decision is decided, whose blockers
  are closed, and which has no prior episode. The node shows a live **Start
  episode** action and labels the current node ceiling **Next episode limit**.
- Open otherwise-ready Experiments with prior episode history in both completed
  and nonterminal semantic states. Each action reads **Start new episode**;
  episode history, not `Experiment.status`, selects the label.
- On the node with a completed prior episode, change **Next episode limit**. Its
  pinned used / ceiling history stays unchanged in Runs, while Runs and the node
  drawer both show the changed prospective limit separately.
- Open otherwise identical nodes with an open decision, a pending proposal, an
  open blocker, or a loop episode that still has automatic invocations available.
  The corresponding episode-start action is disabled and the exact gating reason
  is visible. Ordinary Work conversation remains available.
- Press **Start episode**. RCP opens the first bounded Experiment-loop invocation
  with the governing decision bundle pinned, assigns a durable episode id and a
  fresh native provider session, and records invocation 1 of the current **Next
  episode limit**. No semantic attempt record exists merely because the button
  was pressed. Per S72 the app navigates to Runs and opens this Experiment's run
  detail rather than a floating node-chat window. While that episode can continue
  automatically, another episode start is refused; ordinary Work remains
  available.
- Inspect the staged contract file. It contains the normal RCP ontology,
  authority, focused-node/one-hop context, repository pointers, and exact Patch,
  validator, watcher, schema, and artifact paths. It points separately to one
  small loop-control JSON file containing only phase, episode and invocation
  counts, pinned decisions, live drift, completion criteria, and delivered
  watcher or group ids, plus one watcher-state JSON path. Attempts remain in the
  Experiment inside canonical `graph.json` and use the existing Patch schema;
  RCP does not duplicate them into another loop input or schema. No prior chat
  transcript is supplied.
- Let bounded preflight fail once and succeed after an in-turn repair. No attempt
  or extra invocation is spent by the failed preflight. Separately, drive a
  provider failure before launch and use the existing task Retry; it continues
  the same authorized invocation.
- Submit external work and, if useful, have the agent's loop patch record a
  semantic attempt with the pinned decisions. The agent follows the staged
  attempt schema and recording protocol, but RCP does not infer attempt
  boundaries from job or watcher counts. Every loop invocation also writes the
  exact `watch.json`: strict observers may carry an Experiment-only group label,
  and a staged compatible observer may be retired with a reasoned stop item.
  After those dispositions, `[]` or a stop-only list that leaves no live observer
  is valid only when the same Patch explicitly records success, a Proposal, or a
  Blocker that exits or pauses the loop.
- Omit `watch.json`, then provide a malformed or initially uncheckable watcher.
  Separately, provide `[]` without an explicit exit Patch. RCP keeps the same
  episode and invocation and asks the same native session to inspect
  authoritative external state and correct the loop handoff. The correction
  writes valid watchers for work that exists, or writes `[]` and a validated
  success/Proposal/Blocker Patch when the loop should exit; it never resubmits.
  If correction cannot establish either state, the task fails visibly and
  remains Retryable instead of silently losing the work.
- Deliver completion through S42's watcher machinery. The attributed watcher
  invocation reads the newly assembled loop context and logs, then decides
  whether to continue debugging, record or close an attempt, arm more watchers,
  create a proposal/blocker, or finish. The context distinguishes the delivered
  watcher or immutable watcher group from other active, degraded, completed, or
  stopped Experiment watchers, so one completed job is not treated as an attempt
  boundary. A labelled group wakes only when no member is still observed active
  and every remaining member is complete or diagnostically degraded; the
  coalesced wake consumes one invocation and does not mechanically create an
  attempt record. Per S73 the wake resumes this episode's native provider session
  with a compact continuation message rather than starting a fresh session and
  rebuilding the contract; generic Work watcher wakes stay fresh turns.
- From another permitted Work conversation, inspect this Experiment's staged
  watcher state, retire obsolete observers, and arm replacements through its
  distinct Experiment watcher file. The maintenance answer stays in that
  conversation, consumes no loop invocation, and the replacements still wake
  this episode's bound native session. Its ordinary `watch.json`, if written in
  the same turn, remains a self-wake for the maintenance conversation. When
  that Work turn is already running on the watcher execution machine, its
  contract says **this machine** and uses a local cold-login shell; it never
  presents the same hostname as an SSH destination. A genuinely different host
  remains explicit.
- Pause, Resume, or Retry that watcher turn. Resume and Retry retain its episode
  and invocation number and receive a compact live control update before acting.
  A recognized provider limit remains recoverable in that same episode and
  invocation: same-provider Retry resumes the exact binding, while an explicit
  provider switch replaces the active binding only after a successful joint
  Patch/watcher handoff and never changes the execution machine, truth scope,
  decisions, watchers, or invocation budget.
  Patch correction and watcher correction receive only the retained contract,
  current output paths, and exact diagnostics; none may repeat operational work
  or consume another loop invocation.
- After a successful recovery and a later watcher invocation or human-started
  episode, try the old task's Resume and Retry actions again. RCP refuses the
  stale operational continuation. A separately eligible patch-only repair may
  reflect the old completed work but cannot rerun it or reactivate that episode.
- Interrupt recovery after the canonical Patch commits or after its watcher set
  persists, then Retry the same invocation. RCP reconciles the root-invocation
  Patch and deterministic watcher identities: at most one canonical Patch and
  one observer per requested watcher remain.
- Drive a turn whose changed evidence makes an upstream choice ready or
  undermines its prior selection. It queues that pinned Decision as `ready` or
  `revisit` in Inbox and fences the episode for wrap-up. Runs says **Wrapping up
  visualization and report** while the exact episode session produces its
  visual report; then the episode pauses. Choosing in the existing ballot does
  not resume automatically; the human presses **Start new episode**, which starts
  a new episode at invocation 1 with the current **Next episode limit**.
- Reach the configured invocation ceiling after the last allowed turn arms a
  watcher. When that watcher completes, RCP does not start an over-budget wake or
  mark the completion delivered. RCP first wraps up the episode without changing
  its operational used / ceiling values, then visibly says the loop is paused
  at its limit. Press **Start new episode**. The prior episode keeps its pinned
  used / ceiling history; Runs and the node drawer show the current prospective
  limit separately. RCP starts the fresh episode at invocation 1 of that current
  limit and atomically delivers the pending watcher or ready group with its
  original attribution and current loop context. The control phase says
  `human_reauthorization`, rather than mislabelling it an automatic wake. Nothing
  is discarded and the human need not raise the limit merely to resume from this
  pause.
- Let compatible ungrouped watchers and ready labelled groups from different
  invocations and permitted arming conversations become deliverable together.
  Their immutable origin
  episode/invocation fields remain visible in watcher state, and compatible
  delivery may coalesce them into one invocation without splitting a group.
  Race S72's **Stop loop** or an agent's reasoned staged-observer retirement
  against notification claim: one atomic winner is visible and nothing wakes
  after the accepted stop. An Experiment loop has no per-watcher human Stop
  action; ordinary Work watchers keep theirs. Confirm that pressing **Stop
  loop** settles the episode without generating a report; Stop is the sole
  no-report ending.
- Complete an experiment successfully. The loop asserts the Evidence node and
  evidence edge, then produces exactly one Inbox item for the Hypothesis status
  transition with that same-patch edge as its cause. If optional
  `completion_criteria` exists, the pinned criterion is shown with the proposed
  belief change but never controlled the loop. Only human acceptance updates the
  downstream belief; edges have no standing. The episode wraps up first through
  the same generic report mechanism, using the Experiment-loop guide in the
  official `episode-report` skill.
- Drive a terminal operational failure. Confirm the same exact-session wrap-up
  produces a partial report. Make report output invalid for all three automatic
  report turns and confirm the episode still settles with one visible
  report-generation error, no Retry control, and no blocked unrelated work.
- Have a later graph-writing task materially introduce new work to that completed
  Experiment. The same Patch reopens it to an appropriate nonterminal status and
  refreshes `current_summary` and `next_action`; its episode action still reads
  **Start new episode** because prior episode history exists.
- While the loop is active, edit the repository manually through ordinary Work.
  RCP shows an advisory active-loop marker but neither locks the repository nor
  blocks the human action.

## Assertions

- `readiness_is_derived_from_decisions_proposals_blockers_and_episode_state`
- `same_machine_watcher_contract_uses_local_shell_not_self_ssh`
- `provider_limit_recovery_stays_inside_current_episode_and_invocation`
- `first_episode_action_reads_start_episode`
- `later_episode_actions_read_start_new_episode_regardless_of_experiment_status`
- `completed_episode_retains_its_pinned_used_and_ceiling_history`
- `runs_and_node_drawer_show_the_current_next_episode_limit_separately`
- `new_episode_starts_at_invocation_one_with_the_current_node_limit`
- `run_is_disabled_with_the_exact_gate_reason`
- `ordinary_work_remains_available_when_run_is_disabled`
- `run_pins_the_governing_decision_bundle`
- `run_starts_a_fresh_episode_at_invocation_one`
- `pressing_run_alone_does_not_create_an_attempt`
- `one_experiment_cannot_start_a_second_active_loop`
- `loop_contract_reuses_node_chat_context_without_transcript_ingestion`
- `provider_launch_text_is_only_the_short_contract_pointer`
- `loop_control_input_contains_only_irreducible_per_invocation_state`
- `watcher_state_is_a_separate_path_not_inlined_prompt_text`
- `attempts_and_their_schema_are_not_duplicated_outside_graph_and_patch_schema`
- `loop_prompt_and_input_staging_are_dedicated_modules_without_generic_fallback`
- `missing_loop_binding_fails_before_provider_launch`
- `preflight_repair_stays_inside_one_work_turn`
- `provider_failure_before_launch_retries_without_spending_another_invocation`
- `provider_task_attempt_lineage_retains_one_loop_invocation_number`
- `only_the_latest_unresolved_task_may_resume_or_retry_operational_work`
- `patch_only_repair_cannot_reactivate_a_stale_episode`
- `loop_invocation_budget_is_consumed_by_run_and_watcher_wake`
- `report_generation_does_not_consume_or_appear_as_a_loop_invocation`
- `attempt_bookkeeping_is_agent_semantic_output_not_loop_budget`
- `old_attempts_load_with_backward_compatible_control_defaults`
- `attempt_records_pin_a_strict_decision_bundle`
- `proposal_only_attempts_are_explicitly_typed`
- `debug_retry_records_fault_change_and_prediction_before_launch`
- `scientific_disappointment_is_not_a_retry_classification`
- `proposal_or_blocker_pauses_the_current_loop_episode`
- `proposal_decision_and_blocker_pauses_wrap_up_before_settling`
- `proposal_resolution_requires_a_new_human_started_episode`
- `resume_and_retry_receive_live_control_delta_without_spending_again`
- `corrections_receive_narrow_context_and_cannot_repeat_operational_work`
- `every_loop_invocation_records_an_explicit_watcher_disposition`
- `missing_or_invalid_watcher_handoff_is_corrected_inside_the_same_invocation`
- `empty_watcher_list_requires_an_explicit_success_proposal_or_blocker_exit`
- `unrecoverable_watcher_handoff_fails_visibly_and_remains_retryable`
- `joint_handoff_recovery_never_duplicates_patch_or_watchers`
- `ceiling_pauses_automatic_wakes_without_discarding_completion`
- `ceiling_wraps_up_before_presenting_human_reauthorization`
- `human_run_claims_pending_completion_as_invocation_one_of_a_new_episode`
- `pending_completion_context_names_human_reauthorization`
- `compatible_cross_invocation_watchers_coalesce_under_current_delivery_policy`
- `successful_stop_acknowledges_a_completion_before_it_can_wake`
- `pressing_stop_is_the_only_experiment_episode_ending_that_skips_report_generation`
- `experiment_episode_wrapup_resumes_the_exact_session_and_stage`
- `experiment_episode_wrapup_uses_minimal_receipt_context_and_the_generic_report_skill`
- `experiment_episode_report_is_inherently_visual_by_skill_prompting`
- `experiment_episode_report_correction_is_hidden_and_bounded_to_three_turns`
- `final_report_error_is_visible_nonblocking_and_has_no_retry_control`
- `completion_criteria_is_pinned_advisory_and_visible_at_acceptance`
- `run_uses_the_experiment_loop_patch_kind`
- `control_watcher_wake_retains_the_experiment_loop_patch_policy`
- `loop_patch_cannot_decide_set_standing_or_edit_the_pinned_bundle`
- `successful_operational_work_survives_a_rejected_graph_reflection`
- `successful_completion_produces_one_human_authority_item`
- `new_work_reopens_completed_experiment_and_refreshes_summary_and_next_action`
- `human_acceptance_is_required_for_the_evidence_grounded_belief_change`
- `active_loop_marker_derives_from_control_tasks_and_watchers_not_semantic_attempts`
- `generic_watchers_never_define_the_active_loop_marker`
- `active_loop_state_only_suppresses_duplicate_run_not_ordinary_work`
- `watcher_records_never_mechanically_create_or_close_attempts`
- `no_console_failed_request_or_server_error_during_the_browser_drive`

## Deliberately not possible in v1

A graph-wide research scheduler, silent automatic restart or budget reset after
a proposal, agent acceptance of its own evidence, a hard repository lease,
RCP-owned job submission, transcript ingestion, or a mechanical shell-command
parser enforcing the invocation ceiling.

## Failure means

RCP spends compute while a graph precondition is red, lets Experiment-loop agent
invocations exceed the human-set ceiling, infers scientific attempt boundaries
from watcher events, converts a scientific result into a debug excuse, or
changes belief without the human's explicit acceptance.
