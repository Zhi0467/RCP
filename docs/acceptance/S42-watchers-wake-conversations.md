---
id: S42-watchers-wake-conversations
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_acceptance_experiment_watchers.py::test_s42_generic_watchers_persist_coalesce_and_never_change_the_graph
  - tests/test_acceptance_agent.py
  - tests/test_watchers.py
  - web/tests/acceptanceAgentMode.test.mjs
invariants: [4, 4b, 8, 9, 10c, 11]
last_passed: 2026-08-05 — a CPU-only served-app browser drive corrected an
  intentionally malformed two-job handoff in the same session, restored both
  watchers after backend restart, coalesced their completion into one attributed
  wake, and left the graph revision unchanged.
---

# Watch external work and wake the conversation that armed it

A watcher is a generic operational mechanism adapted from OpenClaude. It checks
whether external work remains in its system and wakes the conversation that
armed it when the work is gone. It does not understand experiments, attempts,
outcomes, failures, or scientific meaning.

## UI path

Confirmed by the human on 2026-08-01.

- In one Work turn, launch two bounded detached fixture jobs and write one
  `watch.json` containing two watcher items. Each item contains only a
  self-contained `check_command` with literal identifiers, an absolute
  `log_path`, and an absolute `cwd`.
- End the turn. RCP clears any file left by an older ordinary Work turn, reads the
  new list, binds the execution host, return conversation, and continuation
  policy from the originating operation, and runs every check once in a fresh
  `bash -lic` from its declared `cwd`, through the existing SSH login-shell
  transport when the execution host is remote.
- Make one initial check unable to answer. RCP arms none of the list, resumes the
  same provider session with the exact diagnostic, and permits only a bounded
  rewrite of `watch.json`. It never repeats the launched work merely to repair
  the watcher handoff.
- Arm the corrected list. The originating conversation carries exactly one
  watcher control — the count beside the floating chat's close control, or in the
  context row where there is no header. Pressing it discloses each watcher's
  last-check time and any current check error, without implying experiment
  status. The list is never a second permanent fixture of the chat body.
- Make an armed check fail transiently. That watcher becomes visibly degraded and
  keeps polling; the other watcher is unaffected. The error never produces a
  completion wake. A later 0 or 1 clears the degraded state.
- Close RCP, let both fixture jobs finish, and reopen it. The watcher rows show
  truthful stale timestamps until their first new poll. RCP reloads the records,
  asks again, and detects completion without replaying a missed transition.
- Keep a human turn active while both completions are detected. After the human
  turn, RCP creates one attributed watcher turn—not two and not a user message—
  listing both RCP watcher ids and both log paths. It is a fresh Work operation
  with current conversation context and the original RCP-bound patch policy; no
  watcher field selected that authority.
- Inspect the durable queue and watcher records. Creation of that one queued Work
  operation and notification of both included records happened atomically; a
  restart does not lose or enqueue the handoff again. Provider-task Retry may
  redeliver a failed turn without re-firing either watcher.
- Confirm that no ExperimentAttempt or other experiment state was created or
  changed by the watcher rows, polling, or notification bookkeeping. Any later
  graph change must come from the wake turn's separately validated patch; this
  ordinary-Work fixture writes none.

## Assertions

- `watch_json_is_a_non_empty_list_of_strict_three_field_items`
- `one_turn_can_arm_n_watchers`
- `old_watch_json_is_cleared_before_a_fresh_work_turn`
- `host_conversation_and_continuation_policy_are_rcp_bound`
- `watchers_have_no_experiment_or_attempt_identity`
- `arming_validates_the_list_atomically`
- `checks_run_from_cold_with_declared_cwd_and_hard_timeout`
- `exit_zero_means_gone_and_exit_one_means_still_present`
- `initial_check_error_uses_bounded_watch_only_correction`
- `runtime_check_error_is_degraded_retried_and_never_a_wake`
- `watchers_poll_independently`
- `watcher_store_is_separate_from_provider_task_attempt_lineage`
- `watch_records_survive_app_and_backend_restart`
- `closed_rcp_does_not_poll_and_reopen_detects_current_state`
- `completed_watchers_for_one_conversation_coalesce_at_delivery`
- `coalescing_never_merges_incompatible_continuation_contexts`
- `live_human_turn_precedes_the_queued_watcher_turn`
- `watcher_turn_is_visibly_attributed_and_never_uses_the_human_slot`
- `watcher_wake_is_fresh_work_with_current_context_and_bound_patch_policy`
- `wake_names_every_completed_watcher_id_and_log_path`
- `queued_wake_and_notified_records_commit_atomically`
- `provider_task_retry_does_not_refire_the_watcher`
- `patch_json_remains_the_only_graph_change_channel`
- `watcher_rows_never_mutate_experiment_attempts`
- `one_watcher_control_per_conversation` — the count and the list are never both
  standing fixtures of the same chat
- `watcher_list_is_disclosed_by_its_count_control`
- `no_console_failed_request_or_server_error_during_the_browser_drive`

## Deliberately not possible in v1

RCP-owned job submission, an MCP reverse tunnel, an injected handle protocol,
automatic outcome interpretation, wake-on-new-output, stale-watcher cleanup, a
user-facing cleanup primitive, or treating a watcher as an experiment attempt.

## Failure means

One bad watcher half-arms a list, a transient SSH failure becomes a false
completion, app restart loses an active watcher, multiple completions create a
storm of redundant turns, a watcher notification impersonates the human, or the
generic mechanism silently acquires experiment semantics.
