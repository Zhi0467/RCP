---
id: S72-runs-operational-hierarchy
status: implemented
tier: hermetic
driver: browser
covered_by: tests/test_experiment_stop.py, tests/test_storage.py,
  web/tests/runProjection.test.mjs, web/tests/experimentRunDetail.test.mjs,
  web/tests/experimentControlRefresh.test.mjs, web/tests/runDialog.test.mjs,
  browser 2026-08-12
invariants: [8, 10, 10b]
reported_by: human, 2026-08-06
last_passed: 2026-08-12 — a served-app browser drive on the reported
  legacy-attribution episode showed one Needs action health, one Start a new
  episode recommendation, no peer task phase or semantic state, no unusable
  Stop, and a Watchers 5 fold beside its five completed records, with no browser
  error or RCP server traceback.
---

# Runs leads with live operational state

Confirmed by the human on 2026-08-06.

The Runs destination is the operational control surface for Seed/Refresh and
bounded Experiments. It is ordered by what matters now: **Running**, **Needs
action**, then **Completed**. It does not repeat the destination name with a page
title. Generic node chat, project chat, and paper-coach tasks remain outside
Runs as promised by S53; an Experiment-loop task is the deliberate exception
because its `patch_kind="experiment_loop"` and `control_node_id` make it
research execution even though its storage kind is `node_chat`.

Pressing **Start episode** for an Experiment with no episode history, or **Start
new episode** after any prior episode, starts a fresh bounded episode and native
provider session. RCP may reuse the latest durable node-conversation id for
history and watcher delivery, but it does not resume an ordinary Work session or
supply prior chat transcript. The app navigates to Runs and opens the Experiment
run detail instead of opening the floating node-chat window. The label depends
on prior episode existence, never semantic `Experiment.status`.

S73 owns native-session continuity inside the episode and the agent-facing wake
message. This scenario owns where the human sees and controls that lifecycle.

## Setup

A temporary project containing:

- active and succeeded Seed/Refresh tasks;
- failed, interrupted, and paused ingestion tasks;
- asserted open graph Blockers (accepted or contested open Blockers remain graph
  state but are outside human attention);
- Experiments that are ready, graph-gated, running an agent turn, waiting on
  healthy watchers, waiting on a degraded watcher, paused at the invocation
  ceiling with completed watcher state, gracefully stopping, human-stopped,
  completed, abandoned, and superseded, including a settled ready episode with
  a legacy-attribution session diagnostic and an unrecoverable actionable task
  continuity case;
- one generic node chat and one paper-coach task, proving they stay excluded.

The fake provider runs long enough to inspect the active state, records a valid
Patch and watcher handoff, and can be paused, resumed, failed, and retried from
the existing Agent task inspector.

## UI path

1. Open a ready Experiment with no episode history. Confirm its node drawer shows
   **Start episode** and the current node ceiling as **Next episode limit**, then
   press **Start episode**.
2. RCP starts the episode, navigates to **Runs**, and opens that Experiment's
   run detail. No floating node-chat window opens.
3. Inspect Runs from top to bottom: **Running**, **Needs action**, then
   **Completed**. Empty sections may be omitted without changing that order.
4. Select a running Experiment while its agent turn is active, while it waits
   on healthy detached work, while one watcher is degraded, and while a watcher
   completion is waiting at the invocation ceiling. In every compact row and
   expanded detail, confirm there is exactly one primary loop health and one
   recommendation derived from the structured loop state, and that the row uses
   the same recommendation. No task status or phase, and no semantic
   **Experiment state**, appears as a competing peer state. The detail keeps its
   last activity, budget, research summary and next action, watcher, execution,
   and history facts. Its health block carries the recommendation; there is no
   separately labelled **Recommended next step** strip.
5. Fail a loop turn with a recognized provider session limit. In the Experiment
   detail, confirm the historical failure remains visible but the primary
   health and recommendation explain that the same episode and invocation can
   recover. Press **Retry Claude** (or the active provider label) to recheck and
   resume the exact binding. In a separate failure, press **Switch provider…**,
   choose a provider/model/reasoning configuration while the execution machine
   remains locked, and continue the same loop. There is no **Open agent task**
   button.
6. Open a ready episode whose latest task succeeded, which has no live or
   completion-pending watcher, and whose retained native-session history has a
   legacy-attribution diagnostic. Confirm its row and detail recommend **Start
   new episode** directly, the diagnostic remains available under execution or
   history, and **Stop loop** is hidden because it is unavailable. In a separate
   unrecoverable actionable task-continuity case where Stop is available,
   confirm the recommendation is to stop and restart and the control is shown.
   Across these states, no recommendation names a control the UI does not offer.
7. Press **Stop loop** first while an agent turn is active, then in a separate
   episode while only watchers remain. Observe the graceful lifecycle below.
8. After the stop settles, press **Start new episode**. RCP starts invocation 1
   of a fresh episode using the current **Next episode limit**, with stopped
   watcher history visible but no delivered watcher trigger.
9. Complete an Experiment and one ingestion task. They appear in the final
   Completed section. Open the completed Experiment: its prior episode retains
   the pinned used / ceiling history, while Runs and the node drawer separately
   show the current **Next episode limit**. Change that limit and confirm history
   is unchanged and the action still reads **Start new episode**, based on prior
   episode existence rather than completed status.
10. Repeat at a narrow viewport and navigate away and back while another loop
   continues.

## Projection and ordering

Classification uses this precedence; the first matching state wins:

1. **Running** — a Seed/Refresh task is queued, running, or pausing; or an
   Experiment has a queued/running/pausing loop task, a graceful stop waiting for
   that live task to finish, or active/degraded watchers that may still wake the
   current episode automatically.
2. **Needs action** — an ingestion task is failed, interrupted, or paused; an
   asserted open graph Blocker exists; or a nonterminal Experiment is not Running. Accepted and
   contested open Blockers remain operational graph state but leave this section after Sync. This
   includes ready-to-Run, graph-gated, human-stopped, failed/paused invocation,
   and invocation-limit states. A failed, paused, or interrupted turn remains
   here after **Stop loop** while its detail reads **Stopping** and links to task
   recovery. A completed watcher waiting for human reauthorization at the
   ceiling belongs here, not Running.
3. **Completed** — succeeded ingestion work and terminal Experiments with
   `status` `completed`, `abandoned`, or `superseded`, unless a higher-priority
   operational state still applies.

Rows remain newest-first inside their section. This change does not introduce
new pagination or history-retention behavior; complete operational history
remains in project History and the Agent task inspector.

Each compact Experiment row and its expanded detail use the same structured
projection: one primary loop health and one recommendation. The recommendation
is never replaced by the latest task status, and neither task phase/status nor
semantic `Experiment.status` is rendered as a peer state.

## Experiment run detail

The detail answers these questions without making the human reconstruct state
from unrelated task rows:

- **Loop health and recommendation** — exactly one primary health: starting,
  agent active, waiting on watchers, degraded, stopping gracefully, paused at
  the invocation limit, needs action, human-stopped, or completed; plus one
  recommendation derived from structured task, control, stop, and watcher state.
  The recommendation lives inside this health block rather than a separately
  labelled **Recommended next step** strip, and matches the compact row.
- **Activity and history** — last activity and retained diagnostics remain
  visible as supporting facts. The current task's phase or status is not shown
  as another state, and task history remains reachable without competing with
  loop health.
- **Invocation budget** — episode id, used / ceiling, and remaining. Task-level
  Resume, Retry, Patch correction, and watcher correction retain the same
  invocation number; an automatic S73 wake advances it. The episode's pinned
  ceiling remains historical, while the current node's **Next episode limit** is
  shown separately here and in the node drawer and is pinned only when the next
  episode starts at invocation 1.
- **Watchers** — every relevant Experiment watcher with status, originating
  episode and invocation, delivered/pending relationship, last check, exit code,
  current error, completion time, and log path. The fold count matches the
  non-stopped watcher records it displays, including completed records; it never
  says zero beside visible completed watchers. Watchers never appear as scientific
  attempts.
- **Execution** — the persisted provider, model, reasoning effort, execution
  machine, truth scope, native-session continuity state and diagnostics, and
  current task id.
- **Experiment meaning** — semantic attempts, current summary, next action,
  governing decisions, and decision drift remain visible alongside operational
  state without controlling it. Semantic `Experiment.status` is not repeated as
  a visible **Experiment state**.

Failed or paused loop turns expose their recovery where the failure is visible:
**Retry provider** resumes the current binding, and **Switch provider…** opens
the ordinary provider/model/reasoning controls with execution machine locked.
Both retain the episode and invocation. An unavailable **Stop loop** is hidden
rather than disabled; readiness-gated controls may remain visible with their
existing reasons. A ready episode whose latest task succeeded, no
live or completion-pending watcher, and a retained legacy-attribution session
diagnostic recommends **Start new episode** directly. The diagnostic stays in
execution or History rather than overriding current readiness. An unrecoverable
actionable task-continuity state may recommend **Stop loop**, then restart only
when Stop is actually available. **Stop loop** remains a distinct episode-level
abandonment action when shown. The detail has no **Open agent task** button;
provider events, diagnostics, receipts, and staged contracts remain available
from History without making the inspector a prerequisite for recovery. The
detail presents no per-watcher Stop action for an Experiment loop. Generic Work
watchers keep their existing individual Stop authority.

## Graceful Stop loop

**Stop loop** means “finish the current turn, then disable automatic
continuation,” not “cancel the current task” and not “change the Experiment.”

- RCP first persists a durable, restart-safe stop request for the current
  episode. From that point, no unclaimed watcher may win a new wake.
- If no loop task is queued/running/pausing, RCP terminally stops every
  compatible current or adopted active, degraded, or completed-pending watcher
  immediately and the stop settles. Incompatible historical groups remain
  pending.
- If a loop task is queued/running/pausing or a watcher wake already won its
  atomic claim, that task is the current turn and may finish normally. Its valid
  Patch and semantic bookkeeping may apply. Existing watchers and every valid
  watcher emitted by its final handoff are retained as `stopped`, never polled
  or delivered. The UI reads **Stopping** until that task becomes terminal.
- If the current task pauses, fails, or is interrupted, the stop request remains
  durable. The task may still Resume or Retry from the inspector because that
  is recovery of the already-authorized turn; every eventual watcher handoff is
  still stopped. A fresh human episode start remains disabled while that turn is
  unresolved.
- If recovery proves impossible because the pinned native session, exact stage,
  or continuation context is no longer usable, RCP records that exact diagnostic
  and never retries the turn in a fresh provider session. **Stop loop** then
  explicitly abandons only recovery of the already-terminal task, records that
  transition with the preserved task history, terminalizes the compatible
  watchers, and settles so a fresh human episode start becomes possible. It does
  not discard the retained Patch or reinterpret the failed turn.
- A recognized provider usage, session, quota, or credit limit is a recoverable
  condition, not proof that the episode must end. RCP records the diagnostic and
  offers same-provider Retry and explicit provider switch in this detail. Stop
  remains available when the human actually intends to abandon the loop.
- Stop never deletes a watcher, kills external work, edits Experiment status,
  creates or closes an ExperimentAttempt, interprets a result, or discards a
  valid Patch.
- A stopped watcher remains inspectable but can never poll, wake, be delivered,
  or become active again. The next **Start new episode** action creates a fresh
  episode rather than resuming the stopped one.
- That next initial Run has `delivered_watcher_ids=[]`. Its staged watcher state
  includes the immediately preceding human-stopped episode's watcher records so
  the agent can inspect external work that may still exist without treating a
  stopped observer as a trigger. It uses S73's ordinary full human-Run contract,
  not a special stop-restart prompt.

The stop request, watcher terminalization, task final handoff, and watcher claim
must be race-safe under the existing per-Experiment operation lock and
recoverable joint-handoff rules. A claim that committed first becomes the
current turn; otherwise the stop wins and no wake task is created.

## Assert

- `run_does_not_open_node_chat`
- `run_reuses_conversation_identity_without_native_work_resume_or_transcript`
- `run_navigates_to_selected_experiment_detail`
- `no_redundant_runs_heading_or_orphaned_as_of_header`
- `sections_are_running_then_action_then_completed`
- `projection_precedence_is_operational_before_semantic`
- `experiment_loop_tasks_are_in_runs_but_generic_chat_and_coach_tasks_are_not`
- `completed_watcher_at_limit_is_actionable_not_running`
- `terminal_work_is_last`
- `loop_health_comes_from_task_control_stop_and_watcher_state`
- `one_primary_loop_health_and_one_structured_recommendation`
- `compact_row_and_detail_share_the_same_recommendation`
- `task_phase_status_and_semantic_experiment_status_are_not_peer_states`
- `health_block_owns_recommendation_without_a_redundant_strip`
- `current_activity_survives_navigation`
- `invocation_budget_is_truthful`
- `episode_start_label_depends_on_history_not_semantic_status`
- `completed_episode_budget_remains_pinned_history`
- `runs_and_node_drawer_separate_next_episode_limit_from_episode_budget`
- `new_episode_pins_current_node_limit_at_invocation_one`
- `watcher_health_and_provenance_are_detailed`
- `resolved_execution_and_native_session_state_are_visible`
- `semantic_attempts_and_watchers_remain_distinct`
- `failed_loop_recovery_is_directly_actionable_without_opening_task_inspector`
- `open_agent_task_button_is_absent`
- `only_valid_controls_are_shown_and_unavailable_stop_is_hidden`
- `ready_succeeded_legacy_diagnostic_recommends_start_new_episode`
- `unrecoverable_continuity_recommends_stop_then_restart_only_with_stop`
- `same_provider_retry_and_provider_switch_retain_episode_and_invocation`
- `stop_loop_is_durable_graceful_and_episode_scoped`
- `stop_never_cancels_or_semantically_interprets_the_current_turn`
- `stop_blocks_new_claims_and_terminalizes_existing_and_new_watchers`
- `task_recovery_after_stop_cannot_reenable_automatic_continuation`
- `unrecoverable_task_recovery_never_falls_back_and_stop_can_abandon_recovery`
- `provider_usage_limit_remains_recoverable_until_human_stops_the_loop`
- `stopped_watchers_are_retained_history_not_triggers`
- `next_run_after_stop_is_fresh_with_stopped_history_and_no_delivery`
- `stop_claim_and_handoff_races_have_one_visible_winner`
- `task_diagnostics_remain_reachable`
- `narrow_layout_preserves_hierarchy`
- `no_console_network_or_server_errors`

## Failure means

Run looks like an ordinary chat resume; a node-chat window opens; live or
actionable Experiment work is hidden or misordered; watcher state is presented
as scientific progress; a task phase/status or semantic **Experiment state**
competes with loop health; the compact row substitutes latest task status for
the detail recommendation; the health recommendation is repeated in a separate
**Recommended next step** strip; an unavailable control is shown or recommended;
a settled ready episode is told to Stop solely because retained history has a
legacy-attribution diagnostic; an unrecoverable actionable continuation omits
Stop-and-restart while Stop is valid; Stop cancels valid current work, loses
history, permits a later wake, silently changes graph meaning, forces a new
episode for a provider limit, hides recovery behind an Agent-task detour, or
confuses a historical limit message with a currently enforced limit, repaints a
completed episode's pinned budget with the current node limit, labels episode
start from semantic status rather than prior episode history; or a healthy wait
and a broken watcher look the same.
