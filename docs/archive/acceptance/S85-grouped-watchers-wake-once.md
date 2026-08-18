---
id: S85-grouped-watchers-wake-once
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_watchers.py::test_grouped_watchers_wait_for_all_members_then_claim_once
  - tests/test_watchers.py::test_agent_stopped_group_members_neither_block_nor_trigger_delivery
  - tests/test_watchers.py::test_fifth_group_observation_error_is_ready_but_remains_degraded
  - tests/test_watchers.py::test_claimed_diagnostic_group_remains_history_not_live_work
  - tests/test_prompts.py::test_experiment_work_contract_explains_the_bound_loop_and_watcher_handoff
  - web/tests/experimentRunDetail.test.mjs
  - web/tests/runProjection.test.mjs
last_passed: 2026-08-07 — isolated acceptance-agent project with a four-member
  group rendered one expandable operational unit with finished, degraded,
  running, and agent-stopped counts; member errors and stop provenance remained
  visible, no per-watcher Experiment stop control appeared, and the clean
  browser reload and server log had no failed request, console error, or traceback
invariants: [4, 4b, 8, 10b, 10c, 10d, 10g]
reported_by: human, 2026-08-07
---

# A watcher group wakes once when no member is still observed running

Confirmed by the human on 2026-08-07.

An Experiment agent may launch many detached jobs whose meaningful operational
unit is the whole set: for example, eight inference or evaluation shards. It may
label their observers as one group in `watch.json`. An early member completion
never wakes the episode alone. The group produces one wake only after every
member is either complete or persistently unobservable and none is still known
healthy and running.

Exit `0` means the named work is gone, whether it succeeded or crashed. A
degraded check means only that RCP cannot currently observe the work; it never
asserts a job outcome. After five consecutive observation errors—the point at
which S84 enters its capped 30-minute backoff tier—a group member is eligible for
a one-time diagnostic group wake. The wake tells the agent to inspect the
uncertainty rather than treating it as failure.

## UI path

- In **Runs**, expand the Experiment. Watchers carrying the same agent label and
  RCP-bound group identity appear as one operational unit with a summary such as
  **6 finished · 2 degraded · 0 running**.
- Expanding the group shows every member's watcher id, log path, last check,
  current error, and origin invocation. Existing ungrouped watcher rows retain
  their current presentation.
- Grouping adds no new human stop action. **Stop loop** remains the Experiment's
  human control. S83's agent stop disposition remains file-based.

## File contract

- A normal observer item may add one non-blank `group` label:

  ```json
  {
    "group": "eval-shards",
    "check_command": "test ! -e /tmp/eval-shard-0.pid",
    "log_path": "/tmp/eval-shard-0.log",
    "cwd": "/tmp"
  }
  ```

- All observer items with the same label in one accepted Experiment-loop
  handoff form one immutable group. RCP binds a durable group identity from the
  originating operation and label, so the label cannot collide with a group
  from another invocation, conversation, project, or Experiment.
- Observer items without `group` keep the existing independent behavior. Group
  is available only for Experiment-loop handoffs; ordinary Work retains S42's
  strict ungrouped observer items.
- Initial validation remains all-or-none across the complete `watch.json`
  handoff, including grouped observers and S83 stop items. A group must contain
  at least two newly armed observers. Stop items do not create or join groups.
- Group membership and identity are immutable after arming. Continuing work is
  represented by a new accepted group, not by mutating an old one.

## Readiness and delivery

- A group is waiting while any member reports exit `1`.
- A member returning exit `0` is complete, but cannot independently enter the
  notification queue while it belongs to an undelivered group.
- A degraded member becomes diagnostic-ready after five consecutive errors. It
  remains degraded—never completed or automatically stopped—and its unknown job
  outcome is preserved.
- The group becomes ready when every member is complete or diagnostic-ready and
  no member is active. RCP claims the group as one durable delivery unit and
  creates at most one wake task for it.
- A member retired through S83 before group delivery is excluded from readiness
  requirements and can never trigger the group. If all members are retired, the
  group is historical and produces no wake.
- Compatible ready groups may coalesce into one wake, but no group is split and
  members from different groups are never merged into a synthetic group. For an
  Experiment, compatibility follows the node's live episode rather than the
  conversation or provider that created or maintained each group.
- Claim, notification bookkeeping, and task creation are atomic. Restart,
  polling races, callback retry, task Resume, and task Retry cannot create a
  second wake from the same group.

## Wake contract

The staged watcher state, loop-control file, and compact continuation identify
each delivered group and list every member with its watcher id, status, log
path, last error, consecutive-error count, and origin invocation. The prompt
states:

- the group woke because no member is still observed running;
- exit-`0` members are gone but their success or failure is not inferred;
- degraded members have unknown external state and must be inspected before the
  agent relaunches, cancels, or records an outcome;
- the agent may retire obsolete observers through S83 and may arm a new group
  for additional detached work;
- completed operational side effects must not be repeated during correction,
  Resume, or Retry.

The grouping label and watcher state never grant graph or episode authority.
The turn retains the same Experiment-loop Patch policy, episode-native session,
invocation accounting, and three valid exits as an ordinary watcher wake.

## Drive

1. Start an Experiment-loop invocation whose fixture agent launches eight
   detached shard jobs and writes eight observer items labelled `eval-shards`.
2. Complete shards one through seven across separate polling passes. Confirm
   that their watcher rows become complete but no wake task is queued while the
   eighth watcher still returns exit `1`.
3. Complete shard eight. Confirm that the one durable group becomes ready and
   queues exactly one attributed wake containing all eight members.
4. Repeat with six members returning exit `0`, two returning consecutive
   observation errors, and one healthy member. The two degraded members reach
   five errors without producing a wake while the healthy member remains
   active. Complete the healthy member; one diagnostic group wake is queued.
5. Inspect its prompt and staged files. They preserve both degraded states and
   errors, state that their job outcomes are unknown, and name all members as
   one group rather than eight independent triggers.
6. Retry delivery callbacks, restart before and after the atomic claim, and
   Retry the wake task. No second group wake appears.
7. In a new group, use S83 to retire members before readiness. Retired members
   never trigger delivery; remaining members wake once when ready. Retire every
   member of another group and confirm it becomes historical without waking.
8. Arm ungrouped watchers beside two separate labelled groups. Ungrouped
   watchers retain independent completion behavior; each labelled group remains
   indivisible, though compatible ready groups may share one wake task.
9. Drive the Runs view and confirm grouped summaries and member details render
   without hiding ungrouped watcher history or reporting degraded jobs as
   crashed.

## Assertions

- `experiment_watch_item_accepts_non_blank_group_label`
- `ordinary_work_watch_item_remains_strict_and_ungrouped`
- `group_identity_is_bound_to_origin_operation_and_agent_label`
- `group_requires_at_least_two_new_observers`
- `watch_handoff_validation_remains_all_or_none_with_groups_and_stops`
- `group_membership_is_immutable`
- `early_group_member_completion_never_queues_a_wake`
- `active_member_blocks_group_readiness`
- `fifth_consecutive_error_makes_group_member_diagnostic_ready`
- `diagnostic_ready_member_remains_degraded_not_completed_or_stopped`
- `group_readiness_requires_no_active_member`
- `ready_group_is_claimed_and_delivered_exactly_once`
- `group_claim_task_and_notification_commit_atomically`
- `restart_callback_retry_resume_and_retry_never_redeliver_group`
- `agent_stopped_member_never_triggers_or_blocks_group`
- `fully_agent_stopped_group_never_wakes`
- `compatible_ready_groups_may_coalesce_without_merging_membership`
- `ungrouped_watchers_retain_independent_delivery`
- `group_wake_preserves_episode_session_budget_and_patch_authority`
- `group_wake_prompt_preserves_unknown_outcome_for_degraded_members`
- `runs_groups_members_and_reports_truthful_counts`
- `no_console_failed_request_or_server_error_during_browser_drive`

## Failure means

One shard wakes the agent while peers are still running; a transient observation
error is reported as a crashed job; one group produces multiple invocations;
restart loses membership or notification state; an agent-stopped member later
wakes the episode; grouping changes graph authority; or Runs flattens the group
so the human cannot see the actual operational unit.
