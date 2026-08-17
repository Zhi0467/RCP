---
id: S124-auto-research-harvests-child-lifecycle
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_auto_research_children_storage.py
  - tests/test_auto_research_child_reconcile.py
  - tests/test_auto_research_delivery.py
  - tests/test_auto_research_mail.py
  - tests/test_auto_research_effects.py
  - tests/test_auto_research_experiments.py
  - tests/test_episode_lifecycle_acceptance.py
  - tests/test_experiment_stop.py
  - tests/test_background.py
  - tests/test_experiment_loop_agent_io.py
invariants: [4, 8, 10, 10b, 10c, 10g]
---

# Auto-research harvests child lifecycle before it finishes

**Confirmed by the human 2026-08-16.**

Spawned Work and child Experiment episodes are not fire-and-forget effects. RCP
durably tells the Auto-research orchestrator when they settle, fail, exhaust,
need recovery, or complete. The orchestrator can harvest or clear that inbox,
resume an exact usable allocation, gracefully replace work that needs a fresh
attempt, and finish only after every live obligation is explicitly settled.

This is a notification registry and message hub, not a third scheduler and not
a live provider channel. Source lifecycle transitions emit the facts; the
existing task, watcher, episode, Stop, and fresh-start paths still perform the
work.

## UI path — decided 2026-08-16

There is no new human inbox or retry control. Runs continues to show the
Auto-research parent and its child task and episode history. Pending notices,
recovery availability, replacement state, and exhausted allowances contribute
to that structured status and the final report. The agent-only inbox and Resume
commands are visible in receipts, not as buttons.

## Setup

A deterministic Auto-research episode with enough B and E budget for wakes, one
spawned worker, one child Experiment episode, and controllable provider outcomes:
success, transient network failure with a usable session, and an unusable
session-limit failure. The server can restart between any durable transition and
its delivery.

## Drive

1. Spawn a worker. Interrupt the process immediately after command admission,
   then restart. Confirm the child record and parent routing either both exist or
   neither exists; a durably accepted admission that is temporarily absent from
   the registry is reconciled and prevents finish meanwhile. Interrupt again
   after the task row commits but before its provider thread starts; reconciliation
   must dispatch that same queued child exactly once without another B spend.
2. Let the worker complete while the orchestrator is asleep. Confirm one
   deduplicated RCP-authored lifecycle notice wakes the exact orchestrator session
   as a new paid B invocation. Confirm the strict lifecycle input is separate
   from agent mail. Read the graph for graph truth; treat the worker's prose as
   hearsay. If the worker also replied, confirm both inputs share this one wake
   without being conflated.
3. Let another child settle while the orchestrator provider is already running.
   Confirm no concurrent same-actor turn and no live-process injection occurs.
   In that same running turn call `inbox --harvest`; receive and acknowledge one
   bounded batch, and confirm it cannot cause a later duplicate wake.
4. Create several more notices. Call idempotent `inbox --clear` and confirm it
   acknowledges exactly the command's pending snapshot, returns ids and counts
   without full bodies, retains audit history, and does not clear a notice
   committed afterward. If the complete compact snapshot cannot fit one response,
   confirm Clear refuses before acknowledging any of it; bounded new-key Harvest
   calls can reduce the snapshot before a fresh Clear.
5. Fail a worker through a transient network error with its exact session and
   stage still usable. Confirm the notice says Resume is available. Call
   `resume <worker-id>` and confirm it uses that binding, repeats no completed side
   effect, and spends no new B allocation.
6. Fail a worker through an unusable or session-limited binding. Confirm Resume
   returns `resume_unavailable` and instructs the orchestrator to `spawn` a fresh
   replacement. Confirm there is no staged Retry verb and a fresh replacement
   spends B normally.
7. Let a child Experiment watcher wake, then let the episode complete, exhaust,
   and fail in separate cases. Confirm watcher continuation remains inside the
   Experiment episode while attention and terminal lifecycle notices route to
   the orchestrator and consume B only if they actually wake it.
8. Kick off a new Experiment episode on a node whose prior episode is active.
   Confirm exactly one project-global live episode: RCP persists
   `replacement_pending`, calls the existing graceful Stop path, permits the
   authorized turn and valid Patch to settle, and starts the fresh episode only
   afterward. Confirm there is no overlap, hard kill, duplicated stop logic, or
   refund of prior E usage.
   Confirm the human duplicate-start action remains disabled, while this command
   treats only the live-episode gate as replacement. If another graph prerequisite
   changes before fresh start, confirm normal readiness refuses the launch and a
   lifecycle notice reports that terminal replacement outcome.
9. While a replacement is pending, call `episode --stop <episode-id>`. Confirm it
   cancels the reserved launch through the replacement coordinator. On an active
   episode, confirm the same command routes to the existing graceful Stop path.
10. For each finish blocker in turn—running or queued worker, active or stopping
    Experiment episode, pending Experiment replacement, undelivered lifecycle
    notice, and accepted-but-not-reflected child admission—call `finish`. Confirm
    one `invalid` response lists every present blocker with kind, id, state, and
    an actionable command, and changes none of them.
11. Explicitly settle work with worker control, `episode --stop`, reconciliation,
    and `inbox --harvest` or `--clear`. Invoke `finish` again with a new key and
    confirm the existing finish fence commits. A terminal failed child whose
    notice was delivered in the lifecycle input no longer blocks and does not
    need to be harvested again. Reusing the refused Finish key returns its exact
    earlier blocker snapshot; only a new key evaluates the settled state.
12. Exhaust B and E before a child settles. Confirm the lifecycle notice remains
    durable and visible in status/report, but starts no unauthorized wake or
    child invocation. Restart and confirm deduplication and acknowledgments
    survive.

## Assert

- `child_admission_and_parent_routing_commit_together`
- `source_transition_and_lifecycle_notice_commit_together`
- `lifecycle_notices_deduplicate_by_source_event_and_attempt`
- `worker_settlement_and_recovery_route_to_the_orchestrator`
- `experiment_watchers_resume_only_the_bound_experiment_episode`
- `experiment_attention_and_terminal_state_route_to_the_orchestrator`
- `a_busy_orchestrator_queues_notices_without_live_injection`
- `sleeping_delivery_stages_lifecycle_facts_separately_from_agent_mail`
- `worker_reply_and_settlement_notice_can_share_one_paid_wake`
- `inbox_harvest_returns_and_acknowledges_one_bounded_batch`
- `inbox_clear_acknowledges_one_snapshot_without_returning_bodies`
- `acknowledged_notices_cannot_wake_twice`
- `exact_failed_worker_resume_reuses_session_stage_and_allocation`
- `unusable_resume_instructs_a_fresh_replacement`
- `the_staged_orchestrator_surface_has_no_retry_verb`
- `one_experiment_node_has_one_project_global_live_episode`
- `experiment_replacement_reuses_graceful_stop_then_fresh_start`
- `replacement_rechecks_normal_readiness_before_fresh_start`
- `a_pending_replacement_can_be_cancelled_without_starting_it`
- `finish_returns_every_live_blocker_without_cleaning_any_of_them`
- `finish_succeeds_after_settlement_and_acknowledgment_of_still_queued_notices`
- `terminal_child_failure_stops_blocking_after_notice_delivery`
- `exhausted_budget_preserves_notices_without_waking_work`
- `restart_reconciliation_never_loses_or_duplicates_a_child_notice`

## Tool-response boundary

A durable accepted effect returns `ok`; an agent-correctable request or current
state returns `invalid`; an unreachable broker or infrastructure path returns
`unavailable`. `resume_unavailable` is a structured disposition with a named
replacement command, not an invitation to guess. `finish` returns one complete
`blockers` array rather than one error per retry, backed by an immutable receipt
so the same key replays exactly and a new key evaluates current state. `inbox`
returns either bounded notices or compact cleared ids and counts; oversized Clear
is all-or-nothing and says to Harvest before trying a new Clear key. Messages
state what happened and the next useful action without storage, binding, or
scheduler jargon.

## Boundary

Lifecycle notices are authoritative only about RCP lifecycle. They do not make a
worker's scientific claim true and do not grant graph authority. Agent mail
remains star-topology hearsay. No peer mail, live interrupt channel, callback
scheduler, worker-owned watcher, or nested Auto-research episode is introduced.

`finish` is intentionally mechanical but not automatic. It identifies
obligations and refuses; the orchestrator chooses the explicit stop, resume,
replacement, harvest, clear, or wait action. This prevents silent cleanup while
still allowing the orchestrator to finish once all obligations are visible and
settled.
