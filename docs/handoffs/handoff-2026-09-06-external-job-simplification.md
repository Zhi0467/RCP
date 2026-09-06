# External job and watcher simplification

Date: 2026-09-06
Status: active verification on `codex/compute-runner-simplify`, published as
[draft PR #83](https://github.com/Zhi0467/RCP/pull/83). The six compute PRs are
integrated with one shell-watcher contract, human Cancel, watcher-based job UI,
generic Linux helper, and route-specific prompts. The ownership fix removes
launchd and refuses macOS helper launches; the slow remote response-budget
regression reproduces the former failure and passes after correction. Ordinary
episode tests now isolate readiness at the admission boundary instead of
launching real OS probes. Full follow-up verification passed: 4,522 backend
tests, 11 skipped, plus formatting and documentation checks.
The service account's Slurm tools, queue access, and native Codex authentication
have been checked on the reachable team server through its operator tmux
session. The isolated PR-code server drive is prepared and awaits explicit
source-transfer approval. No merge or installed-service update is authorized.
Close this handoff after S136's remaining live journey is resolved.

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
- Generic processes that must outlive the turn need reliable process ownership.
  Linux requires the systemd user manager. macOS helper launches are refused
  because launchd cannot retain detached descendants. The SSH-session fallback,
  launchd backend, and scheduler wrapper are removed.
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

- macOS helper launches refuse before any job root, command, or record is
  created, even with a stale ready probe. Actual Mac readiness, admission, and
  launch checks passed; the served Settings UI displays the refusal and
  supported-machine guidance. Evidence is in
  `/private/tmp/rcp-macos-helper-refusal-x7m8sq9o/result.json` and
  `/private/tmp/rcp-pr83-macos-ui-20260906b/results.json`.
- The shared client/broker response allowance derives from the existing finite
  remote call bounds. The former 120-second allowance fails the scaled slow
  sequence; the corrected allowance returns the launch result and replays it
  without a duplicate. Focused command/runtime checks passed (86 tests).
  Evidence is in `/private/tmp/rcp-helper-deadline-old-budget.log` and
  `/private/tmp/rcp-helper-deadline-focused.log`.
- Episode admission tests no longer depend on OS facilities or a real SSH
  server. The retry regression uses a reserved example hostname and still
  proves that a fresh provider check gates retry before mutation.

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
4. Run [S136](../acceptance/S136-long-running-compute-outlives-the-agent.md) with
   real Codex and reachable disposable team-server/Slurm work. Verify provider
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
