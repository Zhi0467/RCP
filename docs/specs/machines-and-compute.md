# Machines and compute

RCP observes external work through shell watchers. Agents submit scheduler jobs
directly and choose the command, script, and resources. Where a process would
otherwise die with the provider or RCP service, a generic launch helper starts
it under an OS process owner. Both routes use the same watcher contract and
human job controls.

## Base and project machines

The base is the machine of the canonical state repository. Every agent profile
runs beside that repository's `.research/`. Configuration rejects an off-base
`run_on`, except the Paper Coach's, which loading rewrites to the base machine.
[Manifest.base_machine and require_base_machine](../../src/rcp/config.py) own
this rule. Provider launch routing is described by
[Local and SSH execution](providers-and-containment.md#local-and-ssh-execution).

A project names space machines by aliases and assigns each repository to one.
The space card owns provider paths, compute settings and access hints. Manifests
retain machine identity and repository placement. Legacy per-project settings
are validated and migrated to the card. They are not a second live
configuration. [MachineConfig](../../src/rcp/config.py) and
[space_machines.py](../../src/rcp/storage/space_machines.py) own the types.

## Execution route

Each space machine card sets an optional job manager and `jobs_root`. A project
configures neither. A card's job manager starts undecided. A route check of the
card, or of a project using it, turns Slurm on when the account has `sbatch`,
`squeue` and `scancel`. The check turns Slurm off when those commands are
missing. An unreachable machine stays undecided. A human's choice is never
overridden. Slurm adds direct scheduler submission. Independently, the helper
resolves `systemd_user` on Linux with a reachable user manager or `launchd` on
macOS. If no OS owner is available, launch refuses before creating job files.
There is no fallback to a detached process or an SSH session.

Each job manager owns its instructions and readiness. Slurm is the first choice
for compute. The helper also owns dashboards and other services. Agents choose
the route. RCP never reroutes scheduler submissions or supplies scheduler
resource arguments, users or associations. A job manager belongs to its machine.
The agent's list of project machines marks each machine's manager and states
each manager's rules once. A reachable cluster's Slurm therefore has the same
guidance as the agent's own. The helper belongs only to the turn's execution
machine.

**Why:** A scheduler adds compute rules without removing OS ownership for
non-compute services. Keeping both avoids consuming scheduler slots for a
long-lived dashboard. Their readiness stays independent.

Short jobs may finish inline. The guidance recommends watchers for jobs lasting
more than roughly ten minutes. This is not a runtime cutoff. Services use helper
launch and human Cancel. On Linux, Cancel stops the cgroup. On macOS, it stops
the launchd main process and process group. A descendant that deliberately
starts another session may survive.

## Readiness and setup

Slurm probes `sbatch`, `squeue`, `scancel` and queue access through the
watcher login shell on the execution host. It submits no test or dry-run job.
Job-specific accounts and resources are validated at actual submission. Failures
name the administrator prerequisite.

The helper probe resolves an OS owner and launches bounded work. It must observe
the work alive and receive both an exit receipt and a log marker. Local Linux
must place the work outside RCP's cgroup. A remote cgroup cannot prove
separation from the local service. Proving survival after provider exit or
service restart requires a live journey.

systemd first probes `PrivateUsers=yes`, `ProtectSystem=strict`, a read-only
home and exact writable roots. If only cooperative containment succeeds, the
probe reports that limitation. Mirrored launches preserve the resolved roots and
exclusions. These are cooperative write guards. They do not isolate reads or
network access. Helper jobs require linger so the user manager survives logout.
Failure points to **Allow background processes**. Installation enables and
verifies service-account linger. Updates retain the existing choices.

Startup checks the routes of every home project's machines. The helper route
exists only on the base machine, where every agent runs. The scheduler route
exists on each machine whose card turns Slurm on. A check drops any stored result
for a route its machine no longer offers. The fixed `scheduler` and `helper`
slots retain independent backends and results. One failure cannot mark the
other route unready. Each stored result bumps the project's settings watermark,
which refreshes open pages. The machine card's compute fields, its **Check
compute again** button and their synchronous re-check are
[card endpoints](api-web-and-desktop-projections.md#paper-settings-and-history).

Runs shows each unready route's diagnostic, action, Settings link and **Check
again**. The button calls
`POST /api/projects/{project_id}/machines/{machine_alias}/compute/check`. The
[operator probe](../server.md) uses the service socket and saves its results. It
refuses a route the machine does not offer and succeeds only for a ready route.
Episode start and reauthorization never gate on compute readiness. Helper launch
probes its own route afresh.

Live-artifact job needs resolve launch keys through durable successful receipts
and the same recovery lineage. They grant observation only.

## Generic launch helper

`compute_jobs.backends` owns the OS adapters. systemd uses transient
`rcp-job-<id>` units with `--collect`, explicit logs and
`XDG_RUNTIME_DIR=/run/user/<uid>`. launchd uses a non-keepalive plist in the
account's GUI domain with cooperative containment. Remote helpers ship source
modules over ordinary SSH.

Job roots are `<data_dir>/jobs/<id>` locally and `<jobs_root>/<id>` remotely.
The remote default is `~/.rcp/jobs`. Configured roots must be absolute. For remote
jobs, the execution account resolves the root and refuses one at or inside its
`~/.rcp/stages` or a legacy `/tmp/rcp-run.*` stage, so launches and helper
checks fail with that reason. Internal ids grant neither filesystem authority
nor a second watcher identity. `command.json` records the intent. `launch.json`
records the OS handle. The wrapper writes `started`, appends output to `log`,
and atomically writes `exit` with the status and time. An uncertain start or
failed post-start persistence retains evidence. It does not adopt orphan
directories or claim that nothing ran.

Work, Experiment and child Work expose
`launch --key K --cwd <absolute-path> [--label TEXT] -- <argv...>` through the
turn-bound process-tree broker. The working directory must be in write scope and
outside protected paths. Validate-only credentials and other run kinds cannot
launch. The response supplies the watcher to copy into `watch.json`. The
[compute job models](../../src/rcp/compute_jobs/models.py) own stored job,
launch request and readiness fields. The watcher observes OS ownership and
receipts rather than guessed PIDs. Cancel uses the saved handle.

Each launch probes readiness, starts work, waits
`COMPUTE_JOB_STARTUP_CHECK_SECONDS` and refreshes once. `startup` reports
running, unknown with a diagnostic, or ended with an exit code and log tail. RCP
checks outside the provider sandbox so immediate failures reach the agent.
Settlement requires observers for still-running helper jobs, including retained
jobs from the recovery lineage. Missing observers enter correction without a
relaunch or another invocation. Finished jobs need no observers. Scheduler jobs
have no helper registry or enforcement.

Keys replay one intent across Resume, Retry and handoff without a duplicate
launch. Each dispatched replay retains a receipt. Broker delivery recovery is
not another attempt. Proven non-submission returns `submitted: false` and leaves
the key reusable. Interrupted commands without a result return
`disposition: uncertain` and the original attempt. The agent must not resubmit
under any key.

The broker's bounded allowance covers working-directory setup and transfer,
containment probes, one reprobe of a stale binding, launch and cleanup. Client
waits remain bounded by `COMMAND_CLIENT_WAIT_SECONDS`. Slower work returns
unavailable with `delivery: unknown` while RCP continues. Repeating the exact
call joins the in-flight send or replays its result. Interrupted results retain
uncertainty.

## Episode merge and removal gate

Merge and Remove worktree check jobs started by every episode sharing the
isolation owner. A live or unobservable job pauses both actions. RCP lists the
jobs, and the human either cancels or confirms. Stopping a watcher does not
prove job completion. A confirmed Merge with a code worktree always runs the
code merge agent. The agent receives the confirmed jobs and stops them before it
merges. If a job still reads as unfinished afterwards, Merge keeps the worktree
for a later Merge to remove. The gate itself never cancels a job. RCP runs a
job's cancel action only on a human's Cancel or a confirmed project deletion.

## One watcher and human Cancel contract

The [watcher spec](conversations-episodes-and-watchers.md#watcher-resources)
owns the external schema, grouping, target and session binding, polling and
Stop. Scheduler and helper entries share an absolute working directory and log
path, a literal check command and an optional cancel command. There is no job-id
observer form. Checks report operational liveness, never scientific success.

`GET /api/projects/{project_id}/watchers` supplies the job UI's observation,
log, diagnostic, cancellation history and backend-computed `can_cancel`. There
is no separate compute-job list API.

A human request to
`POST /api/projects/{project_id}/watchers/{watcher_id}/cancel` requires membership,
write admission and actor attribution. Foreign ids return 404. A
fresh saved check first observes already-ended work or records why cancellation
cannot run. Otherwise, an atomic claim records the actor and time. The saved
command then executes on its host, in its working directory, through the bounded
shell.
Concurrent requests share the claim.

Success means **Cancel requested** and disables repeat requests. Polling must
still prove completion. Failure allows an explicit retry. Timeout leaves the
outcome unknown. A crash between the claim and result never triggers an
automatic replay. Stop, pause, arming and polling never execute cancellation.
Stopped observers retain Cancel for live work. They may record completion
without reopening delivery.

## Durable state and transfer

Internal helper rows retain the OS identity, roots, requester lineage, and
receipts for recovery. Background reconciliation does not block startup. It uses
the saved execution identity even when the project manifest or graph history is
unavailable. Transport errors remain diagnostics rather than evidence of
completion. Helper job directories are not included in whole-database backup
payloads. Restoring a row does not recreate its outputs. Project deletion runs
each running helper job's cancel action first and confirms it with the job's
check. A job it cannot stop is reported without refusing deletion
([deletion](projects-spaces-and-operations.md#deletion)).

Project transfer retains watcher worker identity and cancellation attribution,
time, and diagnostics as history. It strips executable check and cancel actions.
It does not move or cancel external work. Offline restore also clears every
watcher's cancel action, including already-stopped watchers, while detaching
continuations. Ordinary restart retains watchers and their human actions.
