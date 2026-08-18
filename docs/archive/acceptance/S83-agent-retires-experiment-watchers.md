---
id: S83-agent-retires-experiment-watchers
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_watchers.py::test_experiment_watch_json_accepts_groups_and_staged_stop_items_only
  - tests/test_watchers.py::test_agent_stop_is_atomic_idempotent_and_scoped_to_the_bound_episode
  - tests/test_watchers.py::test_stop_loop_absorbs_the_running_turn_s_own_watcher_retirement
  - web/tests/experimentRunDetail.test.mjs
invariants: [4, 4b, 8, 10b, 10c, 10d, 10g]
reported_by: human, 2026-08-07
---

# An Experiment agent can retire observers for work it cancelled

Confirmed by the human on 2026-08-07.

An Experiment-loop turn may decide that detached work from an earlier invocation
is no longer useful. The agent can cancel that external work with its existing
Work tools and explicitly retire the corresponding RCP watchers through the
same file-based handoff that arms watchers. An accepted retirement permanently
removes that watcher from polling and delivery; it can never cause another wake.

This is observer disposition, not episode authority. It does not add a
per-watcher human control, let the agent request **Stop loop**, infer that a
degraded watcher is dead, or make RCP responsible for cancelling the external
job.

This changes the current blueprint rule that only human and lifecycle actions
stop Experiment watchers. The implementation must update the canonical
blueprint in place, bump its internal version and changelog, and update affected
implemented clauses in S41 and S73. No amendment or retained blueprint snapshot
is allowed.

## UI path

There is no new control. In **Runs**, expand the Experiment and inspect its
watcher history. A watcher retired by the agent remains present as **stopped**
with agent provenance and the agent-supplied reason. It is no longer counted as
active or waiting. Experiment watchers still have no per-watcher human **Stop
watching** action; **Stop loop** remains the only human stop control.

## File contract

- Before every Experiment-loop invocation and permitted maintenance
  conversation, RCP stages the bounded watcher-state file containing the
  relevant watcher ids and statuses. The agent never reads the watcher database.
- The Experiment's watcher file remains its one watcher handoff. Its list may contain the current
  strict three-field observer items and explicit stop items. A stop item names
  one staged watcher id and a non-blank reason; it carries no command or path.
  The shapes may be mixed in one list:

  ```json
  [
    {
      "check_command": "test ! -e /tmp/job-8.pid",
      "log_path": "/tmp/job-8.log",
      "cwd": "/tmp"
    },
    {
      "stop_watcher_id": "watcher-id-from-staged-state",
      "reason": "Cancelled superseded external job"
    }
  ]
  ```

  The two strict shapes are distinguished by their fields; no existing observer
  item gains a discriminator or changes representation.
- Stop items are available to the Experiment loop and to a Work conversation
  admitted to that node-attached resource. The same conversation's ordinary
  `watch.json` retains its strict non-empty self-wake observer contract.
- One handoff may stop old watchers and arm replacement observers. Duplicate
  stop ids, unknown ids, already notified watchers, watchers outside the bound
  project or resolved node scope, and watchers incompatible with the current
  episode are rejected atomically.
- RCP does not interpret a stop item as proof that the external job was
  cancelled. Cancelling or otherwise settling that work is an operational side
  effect the agent performs before requesting observer retirement.

## Drive

1. Start one bounded Experiment episode. The fixture agent launches seven
   detached jobs and arms seven watchers in one valid `watch.json` handoff.
2. Complete one job so its watcher wakes the same episode-native session. Leave
   the other six jobs and watchers active.
3. During the wake, have the fixture agent cancel those six jobs. In one
   `watch.json`, explicitly stop their six staged watcher ids with reasons and
   arm no replacement observer. In the same Patch, record an explicit successful
   Experiment exit.
4. Race one stopped watcher against a polling pass and notification claim. The
   stop disposition and any new observer set commit as one recoverable handoff:
   either a prior claim is reported as a conflict and the handoff does not claim
   success, or the accepted stop wins and no notification task exists for it.
5. Press **Stop loop** while that authorized turn is still running, so its stop
   items name watchers the graceful stop already retired. The turn finishes
   normally rather than entering correction, and each record keeps the loop's
   own stop disposition.
6. Restart RCP, wait through several polling intervals, and Retry the completed
   invocation's recovery path. The six records remain stopped exactly once.
   They are never polled, regrouped, delivered, or converted into another task.
7. Try stop items naming an unstaged watcher, a watcher from another Experiment,
   and an already notified watcher. Each invalid list arms and stops nothing and
   enters the bounded watcher-handoff correction in the same native session.
8. Write a valid stop-only handoff that would leave an episode without a live
   watcher but omit an explicit success, Proposal, or Blocker Patch. RCP refuses
   the unexplained empty continuation and requests correction.

## Assertions

- `experiment_watch_json_accepts_explicit_staged_watcher_stop_items`
- `ordinary_work_watch_json_remains_a_strict_observer_list`
- `agent_stop_is_scoped_to_the_permitted_experiment_node_and_episode`
- `agent_stop_requires_a_non_blank_reason`
- `invalid_stop_item_arms_and_stops_nothing`
- `agent_stop_and_new_observers_form_one_recoverable_handoff`
- `accepted_agent_stop_is_idempotent_across_retry_and_restart`
- `accepted_agent_stopped_watcher_is_never_pollable`
- `accepted_agent_stopped_watcher_is_never_grouped_or_delivered`
- `accepted_agent_stopped_watcher_can_never_create_another_wake_task`
- `notification_claim_and_agent_stop_have_one_atomic_winner`
- `stop_loop_absorbs_the_running_turn_s_own_watcher_retirement`
- `agent_stop_never_claims_to_cancel_the_external_job`
- `stop_only_handoff_requires_an_explicit_loop_exit_patch`
- `stopped_record_retains_agent_provenance_reason_and_time`
- `agent_stop_does_not_request_or_set_stop_loop`
- `experiment_watchers_still_have_no_per_watcher_human_stop_control`

## Failure means

An agent cancels obsolete work but its observers continue spending SSH polls or
later wake the episode; a stop silently reaches another Experiment's watcher;
Retry duplicates or reverses an accepted disposition; an observer disappears
from history; RCP mistakes observer retirement for proof that a job was killed;
or the primitive gives the agent the human's episode-level **Stop loop**
authority.
