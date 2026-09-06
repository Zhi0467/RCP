# External job and watcher simplification

Date: 2026-09-06
Status: active integration on `codex/compute-runner-simplify`. The six compute
PRs are combined locally. The single shell-watcher contract, human Cancel API,
watcher-based job UI, generic helper, and route-specific prompt changes are
implemented. The complete backend suite passes (4,536 tests, 11 skipped); the
web suite, build, and disposable browser/API drive also pass. Merge is blocked on the unresolved macOS
ownership policy and matching implementation: live launchd testing found a
child that escaped Cancel through `setsid`. The 120-second staged helper
response deadline also needs review against slow sequential remote operations.
The real team-server/Codex acceptance drive remains.
The user confirmed direct scheduler submission, watcher-supplied Cancel, and
the settled scope below; no macOS ownership exception has been approved. Close
this handoff only after those review items and S136's live journey are resolved,
with explicit human merge; servers consume merged main.

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
  Linux requires the systemd user manager; the SSH-session fallback and
  scheduler wrapper are removed. The current macOS launchd implementation does
  not meet the descendant-ownership requirement; its disposition is unresolved
  below.
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

## Unresolved review items

- **macOS ownership blocks merge.** The live audit at
  `/private/tmp/rcp-ownership-check-whqtv7_m/result.json` observed the launchd
  owner gone after Cancel while a child that called `setsid` continued writing
  its heartbeat. The audit cleaned up that child. The local
  `/usr/share/man/man5/launchd.plist.5`, lines 609–614, documents cleanup of the
  job's process group, which does not include that new session. The code still
  offers launchd. The user has been asked whether to refuse macOS helper
  launches too or authorize an explicit documented exception; no answer has
  arrived. Do not infer approval from silence or describe launchd as reliable
  ownership. Apply the user's decision to resolution, readiness, prompts, UI,
  and the live acceptance expectations before merging.
- **Helper deadline coverage remains unproved.** The staged client waits 120
  seconds, while remote machine resolution, probing, and actual launch run
  sequentially with their own timeouts. The total may exceed the client wait;
  no shared deadline has been tested. Retained receipts and keyed replay avoid
  a duplicate launch but do not establish that a slow valid first launch gets
  its response. Review the full timing path and verify its chosen deadline
  behavior before claiming this issue is fixed.

## Verification and remaining release work

1. Verify Slurm prerequisite checks under the execution account. The settled
   probe checks tool availability and queue connectivity, and never submits a
   readiness job or assumes default resource arguments. Slurm validates actual
   submission permission and resource choices when the agent submits its job.
2. Completed combined verification: 4,536 backend tests passed and 11 skipped;
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

The human requested finishing this scoped simplification without unrelated implementation.
The outstanding ownership and timing findings stay review items; do not add a new backend or
timeout framework without resolving their scope with the human. No push or merge is authorized.
