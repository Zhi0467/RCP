---
id: S41-bounded-experiment-control
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_acceptance_experiment_watchers.py::test_s41_ceiling_pauses_then_human_run_starts_a_new_episode_and_exits
  - tests/test_acceptance_agent.py
  - tests/test_api.py::test_human_run_claims_over_ceiling_completion_as_new_episode_invocation_one
  - web/tests/acceptanceAgentMode.test.mjs
  - web/tests/experimentControlRefresh.test.mjs
invariants: [3, 4, 4b, 10, 10b]
last_passed: 2026-08-05 — a CPU-only served-app browser drive reached the
  invocation ceiling, persisted the pending watcher completion across restart,
  required a fresh human Run, produced grounded Evidence and one proposal, and
  changed the Hypothesis only after human approval and Sync.
---

# Run an experiment through a bounded control loop

An Experiment node can turn its graph preconditions into a bounded sequence of
Work turns. The graph decides whether **Run** is available; each human **Run**
authorizes one episode with a fresh bounded number of agent invocations; the
agent executes and records scientifically meaningful attempts at its own
discretion; and the human remains the only authority that accepts evidence or
decides an upstream choice.

Experiment attempts and generic watchers are separate records. This scenario
uses the watcher delivery promised by S42 to continue the conversation. The
human-set `invocation_ceiling` bounds the initial Run invocation plus attributed
watcher wakes inside one episode. Semantic `ExperimentAttempt` records neither
spend nor reset that budget.

## UI path

Confirmed by the human on 2026-08-05.

- Open an Experiment node whose governing decision is decided, whose blockers
  are closed, and which has no automatically continuing loop episode. The node
  shows a live **Run** action and its per-episode invocation ceiling.
- Open otherwise identical nodes with an open decision, a pending proposal, an
  open blocker, or a loop episode that still has automatic invocations available.
  **Run** is disabled and the exact gating reason is visible. Ordinary Work
  conversation remains available.
- Press **Run**. RCP opens the first bounded Experiment-loop invocation with the
  governing decision bundle pinned, assigns a durable episode id and a fresh
  native provider session, and records invocation 1 of the canonical ceiling. No
  semantic attempt record exists merely because the button was pressed. Per S72
  the app navigates to Runs and opens this Experiment's run detail rather than a
  floating node-chat window. While that episode can continue automatically, a
  second **Run** is refused; ordinary Work remains available.
- Inspect the staged contract file. It contains the normal RCP ontology,
  authority, focused-node/one-hop context, repository pointers, and exact Patch,
  validator, watcher, schema, and artifact paths. It points separately to one
  small loop-control JSON file containing only phase, episode and invocation
  counts, pinned decisions, live drift, completion criteria, and delivered
  watcher ids, plus one watcher-state JSON path. Attempts remain in the
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
  exact `watch.json`: a non-empty list for remaining detached work or `[]` only
  when the same Patch explicitly records success, a Proposal, or a Blocker that
  exits or pauses the loop.
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
  watcher group from other active, degraded, completed, or stopped Experiment
  watchers, so one completed job is not treated as an attempt boundary. The
  coalesced wake consumes one invocation; it does not mechanically create an
  attempt record. Per S73 the wake resumes this episode's native provider session
  with a compact continuation message rather than starting a fresh session and
  rebuilding the contract; generic Work watcher wakes stay fresh turns.
- Pause, Resume, or Retry that watcher turn. Resume and Retry retain its episode
  and invocation number and receive a compact live control update before acting.
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
- Drive a turn that needs a changed upstream decision. It appends a proposal-only
  attempt when that bookkeeping is useful, sends the proposal to Inbox, and
  pauses the episode. Approval does not resume automatically; the human presses
  **Run**, which starts a new episode at invocation 1.
- Reach the configured invocation ceiling after the last allowed turn arms a
  watcher. When that watcher completes, RCP does not start an over-budget wake or
  mark the completion delivered. The Experiment visibly says the loop is paused
  at its limit. Press **Run**; RCP starts a fresh episode and atomically delivers
  the pending watcher group as invocation 1 with its original attribution and
  current loop context. The control phase says `human_reauthorization`, rather
  than mislabelling it an automatic wake. Nothing is discarded and the human
  need not raise the ceiling merely to resume from this pause.
- Let compatible watchers armed by different invocations complete together.
  Their immutable origin episode/invocation fields remain visible in watcher
  state, but RCP coalesces them under current delivery policy and spends one
  invocation. Race S72's **Stop loop** against a completion: either the stop
  atomically fences the unclaimed watcher or an already-claimed wake wins
  visibly; nothing wakes after the stop is persisted. An Experiment loop has no
  per-watcher Stop action; ordinary Work watchers keep theirs.
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

- `readiness_is_derived_from_decisions_proposals_blockers_and_episode_state`
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
- `attempt_bookkeeping_is_agent_semantic_output_not_loop_budget`
- `old_attempts_load_with_backward_compatible_control_defaults`
- `attempt_records_pin_a_strict_decision_bundle`
- `proposal_only_attempts_are_explicitly_typed`
- `debug_retry_records_fault_change_and_prediction_before_launch`
- `scientific_disappointment_is_not_a_retry_classification`
- `proposal_or_blocker_pauses_the_current_loop_episode`
- `proposal_resolution_requires_a_new_human_started_episode`
- `resume_and_retry_receive_live_control_delta_without_spending_again`
- `corrections_receive_narrow_context_and_cannot_repeat_operational_work`
- `every_loop_invocation_records_an_explicit_watcher_disposition`
- `missing_or_invalid_watcher_handoff_is_corrected_inside_the_same_invocation`
- `empty_watcher_list_requires_an_explicit_success_proposal_or_blocker_exit`
- `unrecoverable_watcher_handoff_fails_visibly_and_remains_retryable`
- `joint_handoff_recovery_never_duplicates_patch_or_watchers`
- `ceiling_pauses_automatic_wakes_without_discarding_completion`
- `human_run_claims_pending_completion_as_invocation_one_of_a_new_episode`
- `pending_completion_context_names_human_reauthorization`
- `compatible_cross_invocation_watchers_coalesce_under_current_delivery_policy`
- `successful_stop_acknowledges_a_completion_before_it_can_wake`
- `completion_criteria_is_pinned_advisory_and_visible_at_acceptance`
- `run_uses_the_experiment_loop_patch_kind`
- `control_watcher_wake_retains_the_experiment_loop_patch_policy`
- `loop_patch_cannot_decide_set_standing_or_edit_the_pinned_bundle`
- `successful_operational_work_survives_a_rejected_graph_reflection`
- `successful_completion_produces_one_human_authority_item`
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
