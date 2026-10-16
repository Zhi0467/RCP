# Compute jobs

RCP observes external work through shell watchers. Agents submit scheduler jobs
directly and choose the command, script, and resources.
Where a process would otherwise die with the provider or RCP service, a generic
launch helper starts it under an OS process owner. Both routes use the same
watcher contract and human job controls.

## Execution route

`machines[].compute` optionally sets `job_manager` and `jobs_root`.
`job_manager = "slurm"` adds direct scheduler submission. Independently, the
helper resolves `systemd_user` on Linux with a reachable user manager or `launchd`
on macOS. Missing ownership refuses launch before job files; no detached-process
or SSH-session fallback exists.

Each job manager owns instructions and readiness. Slurm comes first for compute;
the helper also owns dashboards and other services. Agents choose; RCP never
reroutes scheduler submissions. RCP supplies no scheduler resource arguments,
users or associations. The route belongs to the turn's execution machine, not
an attached compute connection.

**Why:** A scheduler adds compute rules without removing OS ownership for
non-compute services. Keeping both avoids consuming scheduler slots for a
long-lived dashboard. Their readiness stays independent.

Short jobs may finish inline. Guidance recommends watchers beyond roughly ten
minutes, not a runtime cutoff. Services use helper launch and human Cancel.
Linux Cancel stops the cgroup; macOS stops the launchd main process and process
group, but a descendant deliberately starting another session may survive.

## Readiness and setup

Slurm probes `sbatch`, `squeue`, `scancel` and queue access through the watcher
login shell on the execution host. It submits no test/dry-run job: job-specific
accounts and resources are validated at actual submission. Failures name the
administrator prerequisite.

The helper probe resolves an OS owner, launches bounded work, observes it alive
and requires exit receipt plus log marker. Local Linux must place it outside
RCP's cgroup; a remote cgroup cannot prove separation from the local service.
Actual provider-exit/service-restart survival needs a live journey.

systemd first probes `PrivateUsers=yes`, `ProtectSystem=strict`, read-only home
and exact writable roots. Cooperative-only success reports that limitation;
mirrored launches preserve resolved roots and exclusions. These are cooperative
write guards, not read/network isolation. Helper jobs require linger so the user
manager survives logout. Failure points to **Allow background processes**;
installation enables/verifies service-account linger, while updates retain choices.

Startup probes helper routes on home projects and schedulers where configured.
A changed compute block invalidates both probes and refreshes that machine in
background. `compute_probes_probed_at` drives page refresh. Fixed `scheduler` and
`helper` slots retain independent backend/results; one failure cannot mark the
other unready. Settings' partial `machine_compute` map preserves omitted aliases
and removes null blocks. Settings displays the slots without a probe button.

Runs shows each unready route's diagnostic, action, Settings link and **Check
again**, calling `POST /api/projects/{project_id}/machines/{machine_alias}/compute/check`.
Operators may use `rcp server compute probe --project <id> --route <scheduler|helper> <alias>`
through the service socket; it saves results and succeeds only for a ready route.
Episode start/reauthorization never gates on compute readiness; helper launch
probes its own route afresh.

Live-artifact job needs resolve launch keys through durable successful receipts
and the same recovery lineage. They grant observation only.

## Generic launch helper

`compute_jobs.backends` owns OS adapters. systemd uses transient `rcp-job-<id>`
units with `--collect`, explicit logs and `XDG_RUNTIME_DIR=/run/user/<uid>`.
launchd uses a non-keepalive plist in the account's GUI domain with cooperative
containment. Remote helpers ship source modules over ordinary SSH.

Job roots are `<data_dir>/jobs/<id>` locally and `<jobs_root>/<id>` remotely,
defaulting to remote `~/.rcp/jobs`. Configured roots are absolute and outside
stage retention. Internal ids grant neither filesystem authority nor a second
watcher identity. `command.json` records intent, `launch.json` the OS handle;
the wrapper writes `started`, appends output to `log`, and atomically writes
`exit` with status/time. Uncertain starts or failed post-start persistence retain
evidence without adopting orphan directories or claiming nothing ran.

Work, Experiment and child Work expose
`launch --key K --cwd <absolute-path> [--label TEXT] -- <argv...>` through the
turn-bound process-tree broker. Cwd must be in write scope and outside protected
paths. Validate-only credentials and other run kinds cannot launch. Response
`{watcher: {check_command, log_path, cwd, cancel_command}, startup: {...}}`
supplies the watcher to copy into `watch.json`. It observes OS ownership and
receipts rather than guessed PIDs; Cancel uses the saved handle.

Each launch probes readiness, starts work, waits
`COMPUTE_JOB_STARTUP_CHECK_SECONDS` and refreshes once. `startup` reports running,
unknown with diagnostic, or ended with code/log tail. RCP checks outside the
provider sandbox so immediate failures reach the agent. Settlement requires
observers for still-running helper jobs, including retained recovery-lineage jobs;
missing observers enter correction without relaunch or another invocation.
Finished jobs need none. Scheduler jobs have no helper registry/enforcement.

Keys replay one intent across Resume, Retry and handoff without duplicate launch.
Each dispatched replay retains a receipt; broker delivery recovery is not another
attempt. Proven non-submission returns `submitted: false` and leaves the key
reusable. Interrupted commands without a result return `disposition: uncertain`
and the original attempt; the agent must not resubmit under any key.

The broker's bounded allowance covers cwd/transfer, containment probes, one stale
binding reprobe and launch/cleanup. Client waits remain bounded by
`COMMAND_CLIENT_WAIT_SECONDS`; slower work returns unavailable with
`delivery: unknown` while RCP continues. Repeating the exact call joins the
in-flight send or replays its result. Interrupted results retain uncertainty.

## Episode merge and removal gate

Merge and Remove worktree check jobs started by every episode sharing the
isolation owner. A live or unobservable job pauses both actions: RCP lists the
jobs, and the human either cancels or confirms. Stopping a watcher does not
prove job completion. A confirmed Merge with a code worktree always runs the code
merge agent, which receives the confirmed jobs and stops them before it merges.
If a job still reads as unfinished afterwards, Merge keeps the worktree for a
later Merge to remove. RCP itself never cancels a job.

## One watcher and human Cancel contract

The [watcher spec](conversations-episodes-and-watchers.md#watcher-resources)
owns external schema, grouping, target/session binding, polling and Stop.
Scheduler and helper entries share absolute cwd/log path, literal check command
and optional cancel command; there is no job-id observer form. Checks report
operational liveness, never scientific success.

`GET /api/projects/{project_id}/watchers` supplies the job UI's observation, log,
diagnostic, cancellation history and backend-computed `can_cancel`. There is no
separate compute-job list API.

Human `POST /api/projects/{project_id}/watchers/{watcher_id}/cancel` requires
membership, write admission and actor attribution; foreign ids return 404.
A fresh saved check first observes already-ended work or records why cancellation
cannot run. Otherwise an atomic claim records actor/time and executes the saved
command on its host/cwd in the bounded shell. Concurrent requests share the claim.

Success means **Cancel requested**, disables repeat requests, and leaves polling
to prove completion. Failure allows explicit retry; timeout is unknown. A crash
between claim and result never automatically replays. Stop/pause/arming/polling
never execute cancellation. Stopped observers retain Cancel for live work and
may record completion without reopening delivery.

## Durable state and transfer

Internal helper rows retain the OS identity, roots, requester lineage, and
receipts for recovery. Background reconciliation does not block startup and
uses the saved execution identity even when the project manifest or graph
history is unavailable. Transport errors remain diagnostics rather
than evidence of completion. Helper job directories are not included in
whole-database backup payloads; restoring a row does not recreate its outputs.
Project deletion runs each running helper job's cancel action first and
confirms it with the job's check; one it cannot stop is reported, not a refusal ([deletion](projects-spaces-and-operations.md#deletion)).

Project transfer retains watcher worker identity and cancellation attribution,
time, and diagnostics as history. It strips executable check and cancel actions
and does not move or cancel external work. Offline restore also clears every
watcher's cancel action, including already-stopped watchers, while detaching
continuations. Ordinary restart retains watchers and their human actions.
