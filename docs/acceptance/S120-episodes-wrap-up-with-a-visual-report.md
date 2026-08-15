---
id: S120-episodes-wrap-up-with-a-visual-report
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_episode_lifecycle_acceptance.py
  - tests/test_experiment_episode_ending.py
  - tests/test_experiment_episode_storage.py
  - tests/test_episode_report.py
  - tests/test_episode_report_prompt.py
  - tests/test_episode_report_skill.py
  - tests/test_episode_storage.py
  - tests/test_episode_api.py
  - tests/test_episode_reauthorization.py
  - tests/test_episode_runtime_api.py
  - web/tests/campaigns.test.mjs
  - web/tests/experimentBoard.test.mjs
  - web/tests/experimentRunDetail.test.mjs
  - web/tests/projectHistory.test.mjs
last_passed: 2026-08-14 — isolated acceptance-agent browser drive covered the
  operational-only start dialog, current Experiment-index refresh, corrected
  sandboxed SVG report, hidden task surface, and Stop-only no-report path; the
  hermetic lifecycle suites cover both episode modes and restart/error endings
invariants: [4, 4b, 8, 10, 10e, 10g]
---

# Episodes wrap up with a visual report

Confirmed by the human on 2026-08-14. Auto-research and bounded Experiment
control are two orchestration modes of one persisted parent concept: an
**episode**. Their operational turns remain different, but their parent episode
manager owns the same ending fence, exact-session report continuation, durable
HTML capture, bounded correction ladder, and terminal result.

The invocation ceiling always counts useful operational turns. Report generation
is internal wrap-up work, never another visible or metered invocation.

## UI path — decided 2026-08-14

- Start either **Auto-research** from the project header or an Experiment episode
  from its node or Runs detail. Those mode names remain human-facing; the shared
  persisted lifecycle is an episode.
- When an episode completes, exhausts its operational invocation ceiling, fails,
  or pauses for human authority through a Proposal, Decision, or Blocker, Runs
  shows **Wrapping up visualization and report** on the parent. No report task,
  attempt counter, correction control, or report invocation appears in Runs.
- A successful wrap-up adds **Open report** to the parent. It opens one immutable
  sandboxed HTML report in a new tab. The report skill is prompted to make the
  report inherently visual, but RCP does not mechanically inspect its visual
  form beyond the existing bounded safe-HTML validation.
- Report generation has at most three hidden provider turns in the episode's
  exact native session and stage: the initial attempt and at most two automatic
  corrections or retries. The continuation receives only the ending, the
  official report skill and output pointers, and one compact immutable wrap-up
  receipt. RCP does not rebuild the episode's graph, research, or transcript
  context for this resumed turn.
- If all permitted attempts fail, the episode still becomes terminal. Its parent
  shows a report-generation error where the report control would have appeared.
  There is no report Retry or Resume control, and the error never prevents a new
  episode or other research work.
- Pressing **Stop** is the sole ending that skips report generation. Stop keeps
  its existing graceful semantics, settles the episode, and exposes neither a
  report link nor a report-generation error.
- Reauthorizing exhausted Auto-research starts a new episode and a fresh native
  session. The exhausted parent and its one report remain immutable; no endpoint
  reopens it or appends a second report.
- The API exposes episodes directly. It has no live `/campaigns` compatibility
  route; the only legacy handling is one-way decoding of already-append-only
  historical Patch bytes during replay.

## Mode-specific report contract

One versioned official episode-report skill owns the common visual retrospective
contract and names two mode guides:

- an Experiment-loop report explains the objective, method and configuration,
  attempts, observations, evidence, failures, limitations, and resulting human
  authority pause or next step;
- an Auto-research report additionally explains epistemic movement, Decisions,
  delegated agent orchestration, what progressed or failed, and the briefing a
  researcher needs to resume human control.

The report is retrospective only. It has no Patch, watcher, command, Proposal,
or graph-authority output channel and never determines the episode's semantic
verdict.

## Drive

1. Start an Experiment episode with an operational invocation ceiling of two.
   Let its final Work turn complete the Experiment and return its ordinary
   Markdown answer.
2. Observe **Wrapping up visualization and report** while the hidden report
   continuation resumes that episode's exact session and stage. Confirm the
   operational meter remains two of two and no report task row or recovery
   control appears.
3. Have the acceptance agent return a missing report, then invalid HTML, then a
   valid visual HTML report. Confirm the same hidden allocation and session are
   used for all three attempts and the resulting **Open report** control renders
   the immutable sandboxed bytes.
4. Repeat with three unsuccessful attempts. Confirm the episode closes, Runs
   shows the report-generation error without a Retry control, and **Start new
   episode** is available.
5. End separate Experiment episodes through invocation exhaustion, an
   unrecoverable operational failure, and a Proposal, Decision, or Blocker pause.
   Confirm each enters the same wrap-up path and produces a mode-appropriate
   report when generation succeeds.
6. Press **Stop** while an Experiment turn is active. Let the current turn settle
   and confirm the episode ends without report generation.
7. Repeat the completion, exhaustion, failure, three-attempt correction, final
   report error, and Stop cases for Auto-research. Confirm the same parent episode
   states and report machinery are used while the Auto-research-specific report
   guide covers epistemic and delegated orchestration history.
8. Reauthorize the exhausted Auto-research episode. Confirm the response is a
   new episode id with invocation 1 and a fresh session, while the exhausted
   episode and report remain unchanged.
9. Restart RCP during hidden wrap-up. Confirm startup reconstructs the durable
   attempt count, never exceeds three turns, never duplicates a report, and
   releases the episode whether report capture succeeds or ends in error.

## Assert

- `auto_research_and_experiment_loops_persist_one_parent_episode_contract`
- `mode_specific_orchestration_plugs_into_one_episode_manager`
- `operational_invocation_ceiling_never_counts_report_generation`
- `completion_exhaustion_failure_and_human_authority_pause_enter_wrapup`
- `pressing_stop_is_the_only_ending_that_skips_report_generation`
- `wrapup_fences_new_work_before_report_generation`
- `report_generation_resumes_the_exact_episode_session_and_stage`
- `report_continuation_receives_only_minimal_immutable_wrapup_context`
- `one_official_report_skill_carries_common_and_mode_specific_guidance`
- `visual_report_form_is_prompted_but_not_mechanically_scored`
- `report_generation_uses_at_most_three_hidden_provider_turns`
- `report_corrections_reuse_the_same_hidden_allocation_session_stage_and_path`
- `runs_exposes_no_report_task_attempt_counter_or_manual_recovery_control`
- `wrapping_parent_says_wrapping_up_visualization_and_report`
- `successful_wrapup_stores_and_opens_one_immutable_sandboxed_html_report`
- `failed_report_generation_is_visible_but_never_blocks_episode_settlement`
- `a_report_error_offers_no_retry_or_resume`
- `new_episode_work_is_gated_only_while_hidden_wrapup_is_in_progress`
- `restart_reconciliation_never_duplicates_or_exceeds_the_report_attempt_limit`
- `report_generation_accepts_no_graph_watcher_command_or_proposal_output`
- `auto_research_reauthorization_creates_a_new_episode_and_fresh_session`
- `the_live_api_has_no_campaign_parent_or_compatibility_route`
- `legacy_patch_lineage_is_decoded_read_only_and_new_history_emits_episode_id`

## Boundary

The shared parent manager owns episode lifecycle, report allocation, capture, and
reconciliation. Auto-research and Experiment-loop adapters continue to own their
different operational authority, quiescence rules, histories, prompts, and
session bindings. Shared plumbing does not branch on a surface discriminator.

No report is generated retrospectively for legacy terminal work. Migration
preserves its existing history without inventing agent output that never ran.
