---
id: S136-long-running-compute-outlives-the-agent
status: pending
tier: live
driver: pytest + ssh + real Codex
covered_by:
  - tests/test_api_compute_jobs.py
  - tests/test_api_compute_admission.py
  - tests/test_server_cli_compute.py
  - tests/test_compute_jobs_commands.py
  - tests/test_compute_jobs_settlement.py
  - tests/test_external_watcher_actions.py
  - tests/test_api_watchers.py
  - tests/test_restore_lifecycle_detachment.py
  - web/tests/externalJobs.browser.test.mjs
  - tests/test_compute_jobs.py
  - tests/test_compute_jobs_probe.py
  - tests/test_auto_research_child_work_compute.py
  - tests/test_auto_research_child_work_watchers.py
  - tests/test_auto_research_child_work_watchers_storage.py
  - tests/test_staged_command_client.py
  - tests/test_watchers.py
  - tests/test_acceptance_experiment_watchers.py
invariants: [4, 4b, 5, 8, 9, 10g]
last_checked: >-
  2026-09-06 — human confirmed direct Slurm submission, prerequisite checks,
  one shell-watcher contract, and human Cancel supplied by the watcher. The
  six-PR integration implements that scope. The complete backend suite passed
  (4,522 tests, 11 skipped), web checks passed, and a disposable browser/API
  drive verified Stop, Cancel, natural completion, and Slurm setup diagnostics.
  The live Mac check now proves helper readiness/admission/launch refuse before
  creating job state. The served UI shows the refusal. A slow remote regression
  reproduces the former response timeout and passes after correction, including
  same-key replay. On the team server, actual rcp-account Slurm tool availability,
  queue access, and Codex authentication pass. The isolated PR-code server drive
  is prepared; explicit source-transfer approval is pending.
---

# Long-running compute outlives the agent and wakes it

The [active simplification handoff](../handoffs/handoff-2026-09-06-external-job-simplification.md)
records the human-confirmed scope. The [compute jobs spec](../specs/compute-jobs.md)
owns execution routes and human actions; existing watcher delivery rules retain
target, coalescing, session, budget, and Stop authority.

## Ownership and response boundary

Generic helper launches require the Linux systemd user manager. macOS launches
refuse before job state is created; local and SSH Darwin regressions and a live
Mac check cover that refusal. There is no launchd ownership exception.

The staged client/broker response allowance covers the existing remote call
bounds. A scaled slow sequence exercises both containment attempts, stale
binding retry, launch, response delivery, and keyed replay without another job.
The real server journey below remains a separate acceptance requirement.

## Setup

Use disposable project data with real Codex on a reachable team server. Exercise
both a ready systemd helper and opted-in Slurm under the configured execution
account. Choose bounded jobs that outlive the provider turn and an RCP restart;
short fixtures may explicitly request handoff because ten minutes is guidance,
not an enforced cutoff. Capture the release, selected route, literal job
reference, provider exit, log, watcher delivery, and task lineage. Never write
the human's live project data or stop unrelated work.

## Drive

1. Opt into Slurm and check access as the execution account. Missing tools or
   account access produce concrete administrator guidance without changing the
   cluster. Ready setup requires no RCP resource configuration.
2. Have a Work agent submit its own Slurm command or script with its chosen
   resources and write a shell watcher with an exact-job cancel command. Queued
   and running work both remain active. Verify RCP does not wrap submission.
3. On the generic route, use the keyed helper launch and copy its returned
   watcher object into `watch.json`. Repeat the same key and arguments and
   verify no second job starts. The agent sees one applicable launch path and
   the same watcher fields on both routes.
4. End the provider turn, restart RCP while work runs, and verify both jobs
   survive. Let each finish and observe one attributed wake in its originating
   conversation with the saved log path. Restart again; no duplicate wake.
5. Repeat handoff and completion from an Experiment-loop turn. Verify the same
   episode, native session, graph target, and one permitted invocation spend.
6. Repeat from Auto-research child Work. The root reports its waiting child;
   the wake continues that child's native session and spends one parent B unit.
   Root Stop fences the wake while the external work remains alive.
7. Omit a still-running helper's shell handoff. Verify same-session correction
   recovers the returned watcher, without another launch or invocation spend.
   Exhausted correction fails visibly. Retry retains the prior running job's
   handoff obligation. A job that already finished needs no watcher.
8. Select a Linux execution machine without reliable helper ownership. Launch
   is refused with setup guidance; no detached SSH-session fallback is offered.
   Verify macOS refuses before creating a job or running the supplied command.
   On Linux, Cancel also stops descendants that create a new session.
9. Human Cancel a running job through its watcher. Verify the saved command
   runs only on that click, with the saved host/cwd and human attribution.
   Concurrent clicks execute once. A command failure shows its diagnostic and
   supports explicit retry; success says Cancel requested until observation
   establishes completion. Already-completed work receives no cancellation
   attribution.
10. Stop watching a separate running job, then Cancel it. Work survives Stop;
    Cancel remains available and never reopens continuation. Restore or transfer
    makes the historical watcher action inert.
11. Make observation fail, including a helper disappearing without a valid
    receipt. The watcher degrades instead of claiming completion. Short jobs
    may finish inline; agent instructions guide longer waits toward watchers
    without imposing a runtime threshold.
12. Drive both job rows and Settings in the served browser. Inspect network,
    console, and server logs. Missing/foreign watcher ids and missing human
    authority cannot execute an action.
13. Exercise the chosen staged-command deadline with slow sequential remote
    resolution, probe, and launch. Verify its response or uncertain-outcome
    handling and keyed retry without starting another job.

## Assert

- `slurm_submission_and_resources_remain_agent_owned`
- `selected_execution_account_gets_actionable_readiness`
- `one_shell_watcher_contract_for_scheduler_and_helper`
- `keyed_helper_launch_starts_one_job`
- `compute_survives_provider_exit_and_rcp_restart`
- `work_wake_is_attributed_coalesced_and_not_duplicated`
- `experiment_wake_preserves_episode_session_target_and_budget`
- `child_work_wake_is_budgeted_waits_the_root_and_is_fenced_by_stop`
- `unobserved_helper_is_corrected_without_relaunch_or_invocation_spend`
- `linux_without_reliable_process_ownership_refuses_launch`
- `macos_without_reliable_process_ownership_refuses_launch`
- `slow_remote_helper_deadline_and_retry_are_verified`
- `human_cancel_is_attributed_bounded_and_not_completion_by_itself`
- `stopped_watcher_can_cancel_without_reopening_delivery`
- `restore_and_transfer_disable_executable_actions`
- `unobservable_work_degrades_instead_of_completing`
- `short_jobs_need_no_duration_gate_or_watcher`
- `patch_json_remains_the_only_graph_change_channel`

Keep this scenario pending until the real team-server drive passes with Codex
and Slurm. Fake backends, local shells, and component tests support it but cannot
prove survival outside the provider sandbox or the production service.
