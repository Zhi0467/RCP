---
id: S84-watchers-poll-with-persistent-backoff
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_watchers.py::test_watcher_schedule_persists_backoff_and_resets_after_a_healthy_check
  - tests/test_watchers.py::test_runtime_error_degrades_only_that_watcher_and_later_clears
  - tests/test_watchers.py::test_a_human_release_takes_a_watcher_out_of_the_polling_set
invariants: [8, 10g]
reported_by: human, 2026-08-07
---

# Watchers poll patiently and survive connection wobble

Confirmed by the human on 2026-08-07.

Long-running detached work must not produce an SSH request storm. A healthy
active watcher checks every two minutes. Repeated connection or check failures
back off durably, and only an observed exit `0` completes a watcher. Restarting
RCP neither forgets the backoff nor synchronizes every watcher into one burst.

This scheduling change applies to generic Work and Experiment-loop watchers. It
does not change their authority, delivery, or stop semantics.

## UI path

There is no new scheduling control. Existing watcher surfaces continue showing
the last check time and current degraded error. Ordinary Work keeps its manual
**Stop watching** action. Experiment watchers still have no per-watcher human
stop action; their human control remains **Stop loop**, while S83 separately
permits an Experiment agent to retire a staged observer through `watch.json`.

## Poll schedule

- Exit `1` means the external work is still present. It is a successful check,
  clears any prior degraded error and consecutive-error count, and schedules the
  next check for two minutes later.
- Exit `0` alone means complete. A completed watcher has no next check and may
  enter its existing delivery path.
- A timeout, transport/runner exception, or any exit other than `0` or `1`
  marks the watcher degraded and never means completion.
- Consecutive errors schedule checks after 2, 4, 8, 15, and then 30 minutes. The
  30-minute delay is the cap for every later consecutive error.
- Every scheduled delay receives deterministic jitter of plus or minus ten
  percent derived from watcher identity, so watchers armed together do not all
  hit SSH simultaneously. Tests use the resulting bounded due time rather than
  wall-clock sleep.
- `next_check_at` and the consecutive-error count are durable watcher state.
  Restart uses those values as written: it does not immediately poll a watcher
  that is not due and does not reset its backoff.
- A successful exit `1` after any number of errors resets the next delay to the
  jittered two-minute healthy interval. Exit `0` completes normally.
- A manually stopped ordinary watcher, a human-stopped loop watcher, or an
  S83 agent-stopped watcher is never selected merely because its old
  `next_check_at` has arrived. A degraded watcher is never stopped
  automatically, regardless of error count or age.

## Drive

1. Arm twenty remote fixture watchers at the same instant. Run the scheduler
   until each has received its initial validated state and one later healthy
   active check.
2. Advance the injected clock. No watcher is checked before its jittered due
   time; all become eligible within the two-minute interval's ten-percent
   bounds, and their checks are distributed rather than one simultaneous burst.
3. Make one watcher return consecutive transport errors. Observe due times at
   the jittered 2-, 4-, 8-, 15-, and 30-minute levels, with later errors capped
   at the 30-minute level. It remains degraded and produces no wake.
4. Restart the store and poller before that watcher is due. Its persisted error
   count and due time are unchanged, and startup performs no early check.
5. Return exit `1` on its next due check. The error clears, the consecutive count
   becomes zero, and the next check uses the healthy two-minute schedule.
6. Exercise timeout, runner exception, and an exit other than `0` or `1`; none
   completes or stops a watcher. Then return exit `0` and confirm that this alone
   completes and enters the existing coalesced delivery path.
7. Stop an ordinary watcher manually, stop an Experiment loop, and accept an S83
   agent stop. Advance beyond every stored due time. None is polled or delivered.

## Assertions

- `healthy_active_watcher_uses_two_minute_interval`
- `scheduled_checks_receive_bounded_deterministic_jitter`
- `watchers_armed_together_do_not_share_one_due_instant`
- `consecutive_errors_back_off_at_two_four_eight_fifteen_thirty_minutes`
- `error_backoff_remains_capped_at_thirty_minutes`
- `next_check_at_and_consecutive_errors_survive_restart`
- `restart_does_not_poll_a_not_yet_due_watcher`
- `successful_active_check_resets_error_backoff`
- `only_exit_zero_completes_a_watcher`
- `timeout_exception_and_unexpected_exit_remain_degraded_without_wake`
- `degraded_watcher_is_never_automatically_stopped`
- `stopped_watchers_are_never_selected_even_when_past_due`
- `existing_completion_coalescing_and_delivery_authority_are_unchanged`

## Failure means

Twenty watchers continue producing thousands of SSH commands per hour; restart
causes a synchronized retry storm; a transient connection failure completes,
stops, or wakes a watcher; error backoff never resets after a successful check;
or any stopped watcher returns to the polling or delivery set.
