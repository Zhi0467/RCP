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
  - tests/test_compute_jobs_observers.py
  - tests/test_compute_jobs.py
  - tests/test_compute_jobs_probe.py
  - tests/test_staged_command_client.py
  - tests/test_watchers.py
  - tests/test_acceptance_experiment_watchers.py
invariants: [4, 4b, 5, 8, 9, 10g]
last_checked: >-
  2026-09-06 — human confirmed the compute-runner journey. PR B implements
  Work and Experiment-loop command and observer paths. The real team-server
  drive, web controls, and prompt instructions remain pending. PR D1 implements
  backend operator surfaces, human Cancel API, and episode-start gating. D2 adds
  Settings compute blocks/probes and job readouts; web Cancel still needs the
  backend availability projection. Browser and real-host drives remain pending.
---

# Long-running compute outlives the agent and wakes it

The human-confirmed journey is in the
[compute runner handoff](../handoffs/handoff-2026-09-06-compute-runner.md).
The [compute jobs spec](../specs/compute-jobs.md) owns the job lifecycle;
watchers retain their ordinary target, coalescing, and admission rules.

## Setup

Use real Codex on the team server with a passing compute backend probe and
disposable project data. Choose a bounded job long enough to outlive the provider
turn and an RCP restart. Capture the release, backend, job id, provider exit,
job log, watcher delivery, and task lineage. No drive writes the human's live
project data or stops unrelated work.

## Drive

1. From a Work turn, launch the bounded job through the staged client and write
   a job observer in `watch.json`. Repeat the same keyed launch and verify it
   returns the same job id without starting another job.
2. End the provider turn. Verify the job remains alive under its execution
   machine's process owner. Restart RCP while the job runs and verify
   reconciliation preserves it and its observer.
3. Let the job finish. Observe one attributed wake in the originating
   conversation with the job id, exit status, start and end times, duration,
   log path, and backend id. Restart again and verify no duplicate wake.
4. Repeat the launch, provider exit, RCP restart, and completion journey from
   an Experiment-loop turn. Inspect its watcher-state file and verify the wake
   uses the same episode, native session, and graph target and spends one
   permitted invocation.
5. Omit the observer for a still-running launched job. Verify settlement names
   the unobserved job id and corrects the handoff in the same session without
   spending another invocation or relaunching the job. A job already exited
   needs no observer.
6. Select a machine with no passing backend probe. Verify `launch` answers
   `unavailable` with the probe diagnostic and required action, and the agent
   stops with a Blocker instead of polling or launching attached computation.
7. Human Cancel a running job. Verify cancellation attribution, idempotent
   repeated Cancel, and observer completion with the cancelled status. Verify
   Stop and pause leave a separate running job alive.
8. Make a job disappear without an exit receipt. Verify its observer degrades
   with the job diagnostic and does not produce a completion wake.

## Assert

- `keyed_launch_starts_one_job_and_returns_one_identity`
- `compute_survives_provider_exit_and_rcp_restart`
- `work_wake_is_attributed_coalesced_and_not_duplicated`
- `experiment_wake_preserves_episode_session_target_and_budget`
- `both_wake_kinds_carry_job_exit_timing_log_and_backend`
- `unobserved_running_job_is_corrected_without_relaunch_or_invocation_spend`
- `unavailable_backend_produces_a_setup_blocker_without_polling`
- `human_cancel_is_attributed_idempotent_and_completes_the_observer`
- `stop_and_pause_do_not_cancel_compute`
- `lost_job_degrades_instead_of_completing`
- `patch_json_remains_the_only_graph_change_channel`

Keep this scenario pending until the real team-server drive passes with Codex.
Fake backend and process-lifecycle regressions support it but cannot prove
survival outside the provider sandbox or the production RCP service.
