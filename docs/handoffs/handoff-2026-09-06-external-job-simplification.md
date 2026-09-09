# External job and watcher simplification

Date: 2026-09-06
Status: implemented on `codex/compute-runner-simplify`, published as
[PR #83](https://github.com/Zhi0467/RCP/pull/83). The six compute PRs are
integrated with one shell-watcher contract, human Cancel, watcher-based job UI,
generic Linux/macOS helper, and route-specific prompts. On 2026-09-06 the human
chose to retain launchd with an explicit ownership exception and remove the
episode compute-readiness gate; both changes are implemented. The slow remote
response-budget regression reproduces the former failure and passes after
correction. The Codex review findings on the integrated branch are fixed: Cancel
no longer inherits graph or Experiment view locks, helper watchers run in the
retained job root rather than an ephemeral task cwd, and project deletion
reconciles running helper rows before its fence. Outside the sandbox on
2026-09-07, the full backend suite, the web suite (653 tests), Ruff, and
pre-commit pass, and the launchd facility test ran against real launchd on a Mac.
On 2026-09-07 the source-transfer drive ran on the team server as the rcp
service account and both compute routes passed: the Slurm route (agent-submitted
pending job, armed watcher, survival across RCP restart, human Cancel through the
API, one wake in the originating conversation, no duplicate after another restart)
and the generic helper route (systemd user manager after linger, mirrored
containment with cgroup isolation, keyed launch, survival, Cancel stopping a
setsid descendant, one wake, no duplicate); evidence in
`/private/tmp/rcp-s136-live-20260907/`. It exposed
two setup defects, fixed in the follow-up PR: the probe's cgroup comparison
rejected hosts with leftover v1 hierarchies, and doctor did not report a service
account without linger. PR #83 is merged. Not yet exercised live: Experiment-loop
and child-Work handoff and wake, missing-handoff correction, stopped-watcher
Cancel, unobservable-work degradation, the served-browser drive, and the slow
staged-command deadline. Those and the release promotion are the open work.
Close this handoff after that remaining live journey is resolved.

The former compute-runner plan is
[archived](../archive/handoffs/handoff-2026-09-06-compute-runner.md). It is evidence
for the process-survival problem, not implementation authority. Current behavior
belongs to the [compute jobs spec](../specs/compute-jobs.md) and
[watcher spec](../specs/conversations-episodes-and-watchers.md).

## Settled scope

- Slurm is opt-in machine setup. The agent submits its own job or script and
  chooses each job's resource arguments. RCP checks prerequisites under the
  actual execution account and guides an administrator; it never configures
  Slurm users, associations, accounts, partitions, or resources.
- Generic processes that must outlive the turn use the systemd user manager on
  Linux and launchd on macOS. On 2026-09-06 the human chose the explicit macOS
  ownership exception: Cancel stops the main process and its process group, but
  a descendant that deliberately starts its own session can survive. Linux
  stops the whole cgroup. The SSH-session fallback and scheduler wrapper remain
  removed.
- Episode starts and reauthorization are not gated on compute readiness, as
  chosen by the human on 2026-09-06. The helper probes when invoked; Settings
  shows each machine's stored probe.
- Every external job uses the existing shell watcher: required `check_command`,
  `log_path`, and `cwd`, plus optional `cancel_command`. No job-id watcher form,
  no second job-list API, and no scheduler job interpretation in RCP.
- The helper returns that watcher object. The staged agent channel exposes only
  `launch` on a helper route and no helper verbs on a scheduler route. Existing
  Work/Experiment/child owners retain broker, native-session, budget, and Stop
  authority.
- Human job management stays. The UI shows external watcher records and a
  backend-owned Cancel capability. A human click executes the saved command on
  the recorded host and cwd after a fresh active check. A successful command is
  a request receipt, not proof of cancellation or scientific outcome.
- Stop fences continuation and leaves work alive. Its stopped watcher retains
  human Cancel. Transfer and offline restore make executable actions inert.
- Short jobs can finish normally inline. Watchers are recommended for roughly
  more than ten minutes to prevent repeated agent polling; no duration cutoff
  is enforced. A still-running helper needs its shell handoff when the turn
  ends, including when recovered through a retry/resume lineage.

## Resolved review findings

- On 2026-09-06 the human chose to restore launchd with the explicit macOS
  ownership exception instead of refusing helper launches. The ownership audit
  at `/private/tmp/rcp-ownership-check-whqtv7_m/result.json` established that a
  descendant starting its own session can survive Cancel; the spec now states
  that limitation. The restored facility test still requires real launchd
  bootstrap when available; sandbox failures remain explicit verification gaps.
- The shared client/broker response allowance derives from the existing finite
  remote call bounds. The former 120-second allowance fails the scaled slow
  sequence; the corrected allowance returns the launch result and replays it
  without a duplicate. Focused command/runtime checks passed (86 tests).
  Evidence is in `/private/tmp/rcp-helper-deadline-old-budget.log` and
  `/private/tmp/rcp-helper-deadline-focused.log`.
- The episode compute-readiness gate and its isolated probe fixture are removed.
  Launch-time probing and stored Settings readiness remain. The retry regression
  uses a reserved example hostname and still proves that a fresh provider check
  gates retry before mutation.

## Verification and remaining release work

1. Verify Slurm prerequisite checks under the execution account. The settled
   probe checks tool availability and queue connectivity, and never submits a
   readiness job or assumes default resource arguments. Slurm validates actual
   submission permission and resource choices when the agent submits its job.
2. Completed follow-up verification: 4,522 backend tests passed and 11 skipped;
   schema upgrades, restore inventory, retained-session prompts, and transfer
   identity are covered. All 653 web tests passed; after the final control-state
   changes, the build and 51 affected browser/UI tests passed again.
3. Completed disposable app drive: the actual browser/API preserved work on
   Stop, executed saved Cancel, avoided cancelling naturally completed work,
   and retained Slurm selection/diagnostics across reload. Console, network,
   and server logs were clean; no provider or compute work was launched. API
   regressions cover authority refusals and cancellation failure/retry.
4. Drive long-running compute end to end with real Codex and reachable
   disposable team-server/Slurm work. Verify provider
   exit, RCP restart, ordinary shell watcher wake, native-session continuation,
   child budget/Stop, and human Cancel. Never use the human's live data directory
   or stop unrelated jobs.

## Concrete owners

- `compute_jobs` owns helper receipts, generic process ownership, and selected
  route readiness. Its internal job ids are not agent watcher identities.
- Work and Experiment runtime owners bind the launch helper and validate its
  shell handoff. The shared task engine gains no scheduler selector.
- `watchers` owns shell checks, saved human actions, and existing delivery;
  storage owns atomic claims and action history.
- API watcher projections export Cancel availability. Web consumes that
  projection and uses one external job row in chat and Experiment views.
- Prompt owners render the resolved execution route and one watcher contract,
  including current instructions for retained native sessions.

The schema changes from the six PRs are unshipped. Their integrated migration
sequence replaces the obsolete job-observer migration, preserves worker
identity, and has one current restore fingerprint. Do not add compatibility
paths for a contract that never shipped, or modify production data to fit tests.

Keep further implementation limited to defects demonstrated by these checks.
Do not add scheduler submission logic, another backend, or a timeout framework.
