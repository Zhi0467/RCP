# Compute runner handoff

Date: 2026-09-06
Status: active, human-confirmed on 2026-09-06. PR A is implemented on
`codex/compute-runner-foundation`: backend profiles, durable receipts and job
state, probes, reconciliation, configuration, and storage/restore integration.
Focused tests, Ruff, pre-commit, and a throwaway served-app startup check pass.
Real systemd and launchd execution is unavailable in this sandbox; the full
suite's existing watcher process checks cannot execute `ps` here. PR B implements
the Work and Experiment-loop broker channel, durable probes, compute verbs, job
observers, settlement correction, and wake payload on
`codex/compute-runner-channel`. Broker, fake-backend command/observer, settlement,
migration, and restore regressions pass, as do Ruff, pre-commit, and a throwaway
HTTP startup check. The focused and full suites fail only the two existing `ps`
sandbox checks; Chromium console inspection is also sandbox-blocked.

PR D1 is implemented on `codex/compute-runner-setup`: install linger, server CLI
probe, machine compute settings/probe APIs, job list and human Cancel, and
episode-start gating. Focused tests, Ruff, pre-commit, and an isolated served HTTP
check pass. The final full suite fails only the two known watcher `ps` permission
checks; the launcher terminal-event test passes on that run. Chromium console
inspection and the real Linux/remote drive remain unavailable here.

PR D2 is partially implemented on `codex/compute-runner-ui`: web contracts,
Settings machine compute drafts/probes, and chat/Experiment job observer rows.
Human Cancel availability remains blocked on the backend record's missing
`can_cancel` projection; no frontend lifecycle derivation or backend change was
added. Non-browser tests, typecheck, build, documentation tests, and an isolated
served HTTP smoke check pass; browser interaction and the real-host drive remain
unverified. PRs C, E, D2 Cancel, and the real-host acceptance drive
remain. The decisions below are settled. Closure:
all six PRs merged, the S136 drive passes on the team server with a real Codex
Work turn, and this file is archived in the same change.

## What this is

A research agent must be able to hand long-running computation to an owner
whose lifetime is independent of the agent's turn, end its turn, and be woken by
RCP when the computation is done. If that handoff is unavailable, the agent must
stop with a concrete setup failure instead of polling.

Today the agent is told to detach work itself and write a shell check into
`watch.json`. That contract cannot be met in the production runtime, and it
produced the expensive polling episodes that motivated this work.

## Evidence (2026-09-06)

- **The provider sandbox kills detached processes.** A real Codex Work turn on
  the team server launched 90-second jobs with `nohup`, `setsid`, and a double
  fork. All three died when the turn ended. The launched processes had PIDs 3,
  4, and 5: Codex's Linux sandbox runs each command in a fresh PID namespace,
  and a PID namespace's init exiting kills every process in it. No shell
  detachment escapes that, and a PID the agent sees inside the sandbox is
  meaningless to a check RCP runs outside it. The same jobs survived when run by
  the same account outside the sandbox. Slurm jobs survive because slurmd owns
  them.
- **RCP's own service is not a safe owner.** `rcp.service` uses the systemd
  default kill mode (control group), so every promoted-release update kills any
  child RCP spawned, however it was detached. S36 promises that updating never
  interrupts work.
- **systemd user units are a durable local owner.** On the team server
  (systemd 249, Ubuntu 22.04) a transient user unit finished after the SSH
  session that started it ended, its cgroup sits under the user slice outside
  `rcp.service`, its exit status is readable, and it launches from a stripped,
  session-less environment. `PrivateUsers`, `ProtectSystem=strict`,
  `ProtectHome=read-only`, and `ReadWritePaths` work on user units there. The
  `rcp` account is not lingering yet; enabling linger needs root once.
- **The historical Slurm rejection was a guessed account.** The `rcp` account
  has a valid default account. A bare dry-run submission is accepted; passing a
  bogus account reproduces the exact historical error.
- **launchd needs a plist.** `launchctl submit` marks jobs keep-alive on
  failure. A plist with `RunAtLoad` and `KeepAlive=false` runs once, exposes the
  exit code, and does not restart.
- **Mutating mailbox verbs require the broker credential.** The command mailbox
  refuses any idempotency-keyed verb on the validate-only credential (tested).
  Work and Experiment-loop turns use validate-only today, and the shared Work
  runtime already passes an invocation-gate slot.

## Settled decisions

1. **RCP owns launch, not the process.** A per-machine backend starts the job
   under the OS's own process owner. Backends: `systemd_user` (Linux),
   `launchd` (macOS), `ssh_session` (a shipped launcher that starts the job in
   its own session under sshd, for SSH machines without a user manager), and
   `slurm` (opt-in). There is no plain subprocess backend. Backends are profiles
   in one registry module; adding one adds one profile.
2. **One RCP-authored wrapper per job.** It records start time, runs the agent's
   script in its working directory with stdout and stderr to the job log, and
   writes an exit file with status and end time. Backends only start, report
   alive, and cancel. Exit status never depends on scheduler accounting.
3. **Job root.** Locally `<data_dir>/jobs/<job_id>`. Remotely a configured
   per-machine `jobs_root`, defaulting to `~/.rcp/jobs` on the execution
   account. Never a temp directory. Job directories are RCP-owned, readable by
   the agent, and excluded from stage retention because they are not stages.
4. **Agent channel.** Three new verbs on the existing staged command mailbox:
   `launch`, `job-status`, `cancel`. The broker identity is generalized from
   episode-bound to turn-bound (live provider process tree), so these
   side-effect verbs keep the stale-process protection of S119. Verbs are
   served by Work, Experiment-loop, and Auto-research child Work. The
   Auto-research root, Discuss, Seed/Refresh, graph merge, and Paper coach do
   not get them. `launch` answers `unavailable` when the machine has no passing
   backend; that is the agent's setup-failure signal.
5. **Job observer.** `watch.json` external items gain a second form,
   `{"job_id": "<id>"}`. RCP checks it through the backend and the exit file,
   never through an agent-authored shell command. The wake carries exit status,
   duration, and log path. The `check_command` form remains for work RCP did not
   launch.
6. **Validation stays at settlement; arming stays after the turn.** One new
   settlement rule: a job launched in this turn that is still running and is not
   named by an observer is a correctable handoff defect and enters the existing
   correction round. A job that already exited needs no observer. Launch is the
   durable in-session receipt; there is no in-session arming.
7. **Child Work may launch and watch.** A completed child watcher wakes the same
   child route and native session, spends one unit of the episode budget, and
   obeys Stop. A waiting child is a distinct route state; the root's status verb
   reports it and guarded finish refuses while any child waits. The root keeps
   its graph conditions and gains no launch and no external observer.
8. **Stop and pause do not cancel jobs.** This is the existing rule. Every job
   row has a human **Cancel** control that calls the backend, is idempotent, and
   records who and when. Nothing else cancels compute.
9. **Backend is machine configuration.** `MachineConfig` gains a compute block:
   backend (explicit or resolved by the probe from the OS), `jobs_root`, and
   Slurm account, partition, and extra submit arguments. The agent never
   supplies scheduler accounts.
10. **Probe, do not assume.** The probe launches a trivial job through the
    configured backend, observes it alive, observes it exit 0 with a log, and on
    Linux asserts the job's cgroup is outside the RCP service's. It also records
    whether write-root mirroring is supported. It runs from the server CLI and
    from Settings with the same label and tone pattern as compute connections.
    Install enables linger for the `rcp` account. Episode starts refuse on a
    machine without a passing probe; human Work turns still run.
11. **Containment.** The `systemd_user` backend mirrors the turn's writable
    roots with unit properties when the probe proved support. Otherwise, and for
    the other backends, jobs are cooperative-only, which is the containment claim
    the spec already makes. The probe result records which.
12. **Reconciliation.** Startup asks each backend about rows still marked
    running. Alive means running. Not alive with an exit file means exited. Not
    alive without an exit file means lost, with a diagnostic. Watchers on lost
    jobs degrade rather than complete.
13. **Out of scope.** No cost backstop. Prompt changes land last, after the
    runner exists, so the agent is told about a channel that works.

## PR sequence

Each PR is a short-lived branch, PR CI, human merge. Each is stacked on the
previous branch until that branch merges.

- **A `codex/compute-runner-foundation`.** This handoff. `src/rcp/compute_jobs/`
  package: models, backend registry and four backends, wrapper, probe,
  reconciliation. `compute_jobs` table, migration, restore-validator awareness.
  `MachineConfig` compute block and manifest writer. Limits. New spec
  `docs/specs/compute-jobs.md`, indexed in `design.md` and the documentation
  test. Unit tests with fake backends; real backend tests skip when the OS
  facility is unavailable. No verbs, no UI, no behavior change for agents.
- **B `codex/compute-runner-channel`.** Turn-bound broker for Work-like turns.
  Protocol, client, and handlers for the three verbs in Work and Experiment-loop.
  Job observer in `watch.json`, storage, poller, arming, and the
  settlement rule. Wake payload and watcher-state projection. Spec updates.
  S136 added as pending.
- **C `codex/compute-runner-child-wake`.** Child Work gets the three verbs and
  its watcher continuation together: route state, wake, budget spend, Stop
  fence, finish guard, root status. No child can launch before its wake exists.
- **D1 `codex/compute-runner-setup`.** Implemented: install linger, server CLI
  probe, machine compute settings API, probe API, job list and human Cancel API,
  episode-start gating, and specs. No web changes.
- **D2 `codex/compute-runner-ui`.** Implemented: web types, Settings compute block
  and probe status, and job rows in chat and Runs. Pending: a backend-owned
  `can_cancel` field and the human Cancel control that consumes it. The typed
  Cancel API client and response/refresh path are prepared; status stays opaque.
- **E `codex/compute-runner-prompts`.** Work, Experiment-loop, and child Work
  prompts: use `launch`, one bounded status check at most, finish; `unavailable`
  is a Blocker naming the setup failure, never a cue to run attached; remove the
  local-PID example; child Work reports back when it cannot finish; rewrite the
  transient-failure rule so a scheduler rejection is not a reason to find
  another execution path.

## Acceptance

S136, "Long-running compute outlives the agent and wakes it", is the one new
scenario. Its journey: a Work turn launches a job through the client and writes
a job observer; the turn ends; the job is alive after the provider exits; RCP
restarts while the job runs; the job finishes; one attributed wake carries exit
status and log path; the same journey from an Experiment-loop turn; a machine
with no passing probe makes `launch` answer `unavailable` and the agent stops
with a Blocker; a human Cancel ends a running job and the observer completes
with the cancelled status. Live tier on the team server with real Codex.

## Owners and invariants

- `compute_jobs` owns job identity, backends, wrapper, probe, reconciliation.
  Task owners (`work.py`, `experiment_loop.py`, `auto_research_child_work.py`)
  own verb policy. `BackgroundAgentTasks` gains no kind branch.
- Invariant 4: capability stays fixed in code; the compute block cannot widen
  it. Invariant 4b: `patch.json` remains the only graph channel. Invariant 5:
  a job id grants no filesystem authority. Invariant 9: a failed turn keeps its
  jobs and job directories. Invariant 10g: a job wake never falls back to a
  fresh session or the wrong graph target. No invariant is added or renumbered.
- Limits live in `limits.py`. Remote code ships from its source module.
