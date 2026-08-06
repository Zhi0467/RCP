---
id: S72-runs-operational-hierarchy
status: implemented
tier: hermetic
driver: browser
covered_by: tests/test_experiment_stop.py, tests/test_storage.py,
  web/tests/runProjection.test.mjs, web/tests/experimentRunDetail.test.mjs,
  web/tests/experimentControlRefresh.test.mjs, browser 2026-08-06
invariants: [8, 10, 10b]
reported_by: human, 2026-08-06
last_passed: 2026-08-06 — served-app browser drive covered the complete Runs
  hierarchy and detail, an automatic watcher wake, watcher-only and active-turn
  graceful Stop, fresh Run after Stop, navigation persistence, and narrow layout;
  hermetic coverage also passed for direct Stop after a bound provider limit.
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

Pressing an Experiment's **Run** action starts a fresh bounded episode and
native provider session. RCP may reuse the latest durable node-conversation id
for history and watcher delivery, but it does not resume an ordinary Work
session or supply prior chat transcript. The app navigates to Runs and opens the
Experiment run detail instead of opening the floating node-chat window.

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
  completed, abandoned, and superseded;
- one generic node chat and one paper-coach task, proving they stay excluded.

The fake provider runs long enough to inspect the active state, records a valid
Patch and watcher handoff, and can be paused, resumed, failed, and retried from
the existing Agent task inspector.

## UI path

1. Open a ready Experiment and press **Run**.
2. RCP starts the episode, navigates to **Runs**, and opens that Experiment's
   run detail. No floating node-chat window opens.
3. Inspect Runs from top to bottom: **Running**, **Needs action**, then
   **Completed**. Empty sections may be omitted without changing that order.
4. Select a running Experiment while its agent turn is active, while it waits
   on healthy detached work, while one watcher is degraded, and while a watcher
   completion is waiting at the invocation ceiling.
5. Open the current Agent task from the Experiment detail. Pause, Resume, or
   Retry it there, then return to Runs. Those invocation controls are not
   duplicated in the Experiment detail.
6. Press **Stop loop** first while an agent turn is active, then in a separate
   episode while only watchers remain. Observe the graceful lifecycle below.
7. After the stop settles, press **Run**. RCP starts invocation 1 of a fresh
   episode through the normal human-Run contract, with stopped watcher history
   visible but no delivered watcher trigger.
8. Complete an Experiment and one ingestion task. They appear in the final
   Completed section. Repeat at a narrow viewport and navigate away and back
   while another loop continues.

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

## Experiment run detail

The detail answers these questions without making the human reconstruct state
from unrelated task rows:

- **Loop health** — starting, agent active, waiting on watchers, degraded,
  stopping gracefully, paused at the invocation limit, needs action,
  human-stopped, or completed.
- **Now** — the current task's persisted phase/status message and last activity,
  or the exact watcher wait/stop state when no agent task is active.
- **Invocation budget** — episode id, used / ceiling, and remaining. Task-level
  Resume, Retry, Patch correction, and watcher correction retain the same
  invocation number; an automatic S73 wake advances it.
- **Watchers** — every relevant Experiment watcher with status, originating
  episode and invocation, delivered/pending relationship, last check, exit code,
  current error, completion time, and log path. Watchers never appear as
  scientific attempts.
- **Execution** — the persisted provider, model, reasoning effort, execution
  machine, truth scope, native-session continuity state, and current task id.
- **Experiment meaning** — semantic attempts, current summary, next action,
  governing decisions, and decision drift remain visible alongside operational
  state without controlling it.

The current Agent task and its full inspector are one action away. That
inspector remains the only UI for invocation-level Pause, Resume, Retry,
provider events, diagnostics, receipts, and staged contracts. Experiment Runs
exposes one loop-level action: **Stop loop**. It does not duplicate task controls
or present per-watcher Stop actions for an Experiment loop. Generic Work
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
  still stopped. A fresh human Run remains disabled while that turn is
  unresolved.
- If recovery proves impossible because the pinned native session, exact stage,
  or continuation context is no longer usable, RCP records that exact diagnostic
  and never retries the turn in a fresh provider session. **Stop loop** then
  explicitly abandons only recovery of the already-terminal task, records that
  transition with the preserved task history, terminalizes the compatible
  watchers, and settles so a fresh human Run becomes possible. It does not
  discard the retained Patch or reinterpret the failed turn.
- A recognized provider usage, session, quota, or credit limit on a bound
  episode session is already proof that its current turn cannot resume. RCP
  records that diagnostic when the task fails, before any recovery action. The
  human may press **Stop loop** directly; the stop abandons only that terminal
  task's recovery and settles without requiring a doomed **Retry** click first.
- Stop never deletes a watcher, kills external work, edits Experiment status,
  creates or closes an ExperimentAttempt, interprets a result, or discards a
  valid Patch.
- A stopped watcher remains inspectable but can never poll, wake, be delivered,
  or become active again. The next human Run starts a fresh episode rather than
  resuming the stopped one.
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
- `current_activity_survives_navigation`
- `invocation_budget_is_truthful`
- `watcher_health_and_provenance_are_detailed`
- `resolved_execution_and_native_session_state_are_visible`
- `semantic_attempts_and_watchers_remain_distinct`
- `task_inspector_alone_owns_invocation_controls`
- `stop_loop_is_durable_graceful_and_episode_scoped`
- `stop_never_cancels_or_semantically_interprets_the_current_turn`
- `stop_blocks_new_claims_and_terminalizes_existing_and_new_watchers`
- `task_recovery_after_stop_cannot_reenable_automatic_continuation`
- `unrecoverable_task_recovery_never_falls_back_and_stop_can_abandon_recovery`
- `provider_usage_limit_allows_direct_stop_without_retry_ordering`
- `stopped_watchers_are_retained_history_not_triggers`
- `next_run_after_stop_is_fresh_with_stopped_history_and_no_delivery`
- `stop_claim_and_handoff_races_have_one_visible_winner`
- `task_diagnostics_remain_reachable`
- `narrow_layout_preserves_hierarchy`
- `no_console_network_or_server_errors`

## Failure means

Run looks like an ordinary chat resume; a node-chat window opens; live or
actionable Experiment work is hidden or misordered; watcher state is presented
as scientific progress; Stop cancels valid current work, loses history, permits
a later wake, silently changes graph meaning, or depends on a doomed Retry click
before a known provider limit can settle; or a healthy wait and a broken watcher
look the same.
