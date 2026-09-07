---
id: S136-long-running-compute-outlives-the-agent
status: pending
tier: live
driver: pytest + ssh + real Codex
covered_by:
  - tests/test_api_compute_jobs.py
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
  Those checks precede the human's 2026-09-06 decision to restore launchd with
  an explicit macOS ownership exception and remove episode readiness gating.
  Both adjustments are implemented. On 2026-09-07, outside the sandbox, the full
  backend suite, the web suite, Ruff, and pre-commit pass, and the launchd
  facility test ran against real launchd on a Mac. Codex review findings on the
  integrated branch are fixed: Cancel ignores view locks, helper watchers run in
  the job root, and deletion reconciles helper rows first. A slow remote
  regression reproduces the former response timeout and passes after
  correction, including same-key replay. On 2026-09-07 the source-transfer drive
  ran on the team server as the rcp service account with a disposable RCP
  instance, real Codex, and the real Slurm queue. Both routes passed: the Slurm
  route (agent-submitted pending job, armed watcher, survival across RCP
  restart, human Cancel through the API, one wake in the originating
  conversation, no duplicate after another restart) and the generic helper
  route (systemd user manager after linger, mirrored containment with cgroup
  isolation, keyed launch, survival, Cancel stopping a setsid descendant, one
  wake, no duplicate). Evidence: /private/tmp/rcp-s136-live-20260907/. The drive
  found two setup defects, both fixed: the probe rejected hosts whose leftover
  cgroup v1 hierarchies place every process at the root, and doctor did not
  report a service account without linger. Steps 5, 6, 7, and 10 to 13 are not
  yet exercised live.
---

# Long-running compute outlives the agent and wakes it

The [active simplification handoff](../handoffs/handoff-2026-09-06-external-job-simplification.md)
records the human-confirmed scope. The [compute jobs spec](../specs/compute-jobs.md)
owns execution routes and human actions; existing watcher delivery rules retain
target, coalescing, session, budget, and Stop authority.

## Ownership and response boundary

Generic helper launches use the Linux systemd user manager or macOS launchd.
Linux Cancel stops the whole cgroup. macOS has an explicit ownership exception:
Cancel stops the launchd service's main process and process group, but a
descendant that deliberately starts its own session can survive Cancel.
Episode starts and reauthorization are not gated on compute readiness; the
helper probes when invoked and Settings shows the stored probe.
Ordinary Work wakes are fresh watcher-attributed Work turns in the originating
conversation; only Experiment-loop and child Work wakes resume a native session.

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
   Verify macOS launches through launchd and Cancel stops the main process and
   its process group; record the exception that a descendant deliberately
   starting its own session can survive Cancel. On Linux, Cancel stops the whole
   cgroup, including descendants that create a new session.
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
- `macos_launchd_cancel_has_an_explicit_ownership_exception`
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
