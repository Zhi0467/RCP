# Compute jobs

RCP observes external work through shell watchers. Agents submit scheduler jobs
directly and choose the command, script, and resources.
Where a process would otherwise die with the provider or RCP service, a generic
launch helper starts it under an OS process owner. Both routes use the same
watcher contract and human job controls. The real-host acceptance drive remains
pending in [S136](../acceptance/S136-long-running-compute-outlives-the-agent.md).

## Execution route

An optional `machines[].compute` block contains `job_manager` and `jobs_root`.
`job_manager = "slurm"` opts into direct scheduler submission. An unset manager
selects the generic helper automatically: Linux requires a reachable systemd
user manager; macOS uses launchd. A Linux machine without reliable process
ownership has no helper route. There is no detached-process or SSH-session
fallback. A selected scheduler does not silently fall back to the helper.

The current macOS route remains an unresolved merge blocker. Live launchd
verification found that a descendant which calls `setsid` escapes the process
group: the owner can disappear and Cancel can return while that child continues.
A passing short probe does not establish reliable descendant ownership. The
user has not yet decided whether macOS helper launches must be refused or an
explicit exception is acceptable; the code still offers launchd. The
[active handoff](../handoffs/handoff-2026-09-06-external-job-simplification.md)
records the audit and decision boundary.

Slurm submission belongs to the agent. RCP supplies no account, partition, GPU,
memory, time-limit, or other submission arguments, and never creates scheduler
users or associations. The execution route belongs to the turn's execution
machine; an attached compute connection alone does not change it.

Short jobs may finish inline. Prompt guidance recommends handing off a watcher
for work expected to take roughly more than ten minutes, so the agent can end
its turn instead of repeatedly polling. This is an agent judgment, not an
enforced duration limit or mandatory watcher for every launch.

## Readiness and setup

For Slurm, the probe uses the same bounded login shell and execution host as
watcher checks. It verifies `sbatch`, `squeue`, and `scancel` are available and
queries the queue under that account. It submits no readiness job, including no
default dry run: a default resource policy could reject an account whose real
jobs work with agent-supplied arguments. Failure names the prerequisite problem
and asks an administrator to repair it. Slurm validates submission permission
and resources when the agent submits the actual job; readiness does not claim
to have proved those job-specific conditions.

For the generic helper, the probe resolves the OS owner, starts a bounded test
job, observes it alive, and requires its exit receipt and log marker. On local
Linux, its cgroup must be outside the RCP process's cgroup. A remote cgroup path
cannot establish separation from the local server's process tree. The real
provider-exit and service-restart journey remains a separate live check.

The systemd probe first tries `PrivateUsers=yes`, `ProtectSystem=strict`,
`ProtectHome=read-only`, and exact `ReadWritePaths`. If only cooperative
containment passes, the result explicitly reports that limitation. Mirrored
launches apply the same resolved writable roots and protect the turn's excluded
paths. These are write guardrails, not read secrecy, network isolation, or a
hostile same-account security boundary.

Install enables and verifies linger for the service account. An ordinary update
preserves configured machine choices. Selecting Slurm is a setup choice; the
probe executes as the actual execution account, including the `rcp` service
account for server-local work.

`rcp server compute probe --project <project_id> <machine_alias>` uses the
installed-service control socket. The matching API is
`POST /api/projects/{project_id}/machines/{machine_alias}/compute/probe`.
Both store the current readiness result; the CLI exits zero only when ready.
Settings accepts `machine_compute`, a partial alias-to-config map: omission
preserves a machine, null removes its optional block. A changed block invalidates
its stored probe. Machine projections include configuration and readiness.

Human Experiment-loop start and Auto-research start or reauthorization run a
fresh readiness check for the selected route before reserving the episode.
A non-ready result refuses with the machine, diagnostic, and required action.
Ordinary human Work is not gated by episode admission. Setup failures should
be surfaced as blockers rather than worked around by repeated polling.

## Generic launch helper

`compute_jobs.backends` contains the generic OS owners. `systemd_user` uses
transient `rcp-job-<id>` units with `--collect`, explicit log paths, and
`XDG_RUNTIME_DIR=/run/user/<uid>`. `launchd` bootstraps a job-root plist into
`gui/<uid>` with `RunAtLoad=true`, `KeepAlive=false`, and same-process-group
cleanup. That cleanup does not retain descendants which enter another session.
Remote commands use ordinary SSH transport; executable remote helpers ship from
their source modules.

A helper job's root is `<data_dir>/jobs/<id>` locally. Remotely it is
`<jobs_root>/<id>`, defaulting to the execution account's `~/.rcp/jobs`.
Configured roots must be absolute. These directories are outside task-stage
retention. Internal helper ids grant no filesystem authority and are not a
second watcher identifier.

The wrapper writes `started`, runs the supplied argv in its absolute working
directory with output appended to `log`, and atomically writes `exit` with the
status and end time. `command.json` records the launch intent and
`launch.json` records the OS handle. An uncertain start or failed post-start
receipt/database write retains evidence; RCP does not guess that nothing ran
or automatically adopt orphaned directories.

Ordinary Work, Experiment-loop, and child Work serve one keyed helper verb:
`launch --key K --cwd <absolute-path> [--label <text>] -- <argv...>`.
The turn binds the project, execution host, writable roots, operation, and
episode. The working directory must remain inside the writable scope and
outside protected paths. The response is
`{watcher: {check_command, log_path, cwd, cancel_command}}`; the agent copies that
object into `watch.json` rather than inventing a PID check. Helper-owned observation distinguishes running work, a valid completion receipt, and unknown
or missing evidence. Cancellation uses the saved OS handle.

A helper launch runs a fresh readiness probe. Before a turn ends, settlement
checks that its still-running helper jobs have their returned check commands in
the final handoff. This includes helper jobs retained from a retry or resume
lineage. Missing observers enter the existing correction path without another
invocation or relaunch; an unresolved handoff cannot become normal completion.
A job that already finished needs no watcher. Slurm submission has no RCP
launch registry or corresponding automatic handoff enforcement.

The broker binds one live provider process tree; validate-only credentials
cannot issue launch. Keys and protected command receipts make the same intent
replayable without duplicate launch. An interrupted command without a result
retains an uncertain-outcome diagnostic. These operational effects create no
new graph-change channel. Discuss, graph merge, Seed/Refresh, Paper, and the
Auto-research root do not gain helper launch authority.

The staged helper client's 120-second wait has not been shown to cover the
combined remote resolution, probe, and launch timeouts. A slow valid operation
may outlast it. Keyed receipts preserve replay safety; they do not prove deadline
coverage. This remains an open review item in the active handoff.

## One watcher and human Cancel contract

Every external `watch.json` entry requires `check_command`, absolute `log_path`,
and absolute `cwd`; it may include one nonblank `cancel_command`. Experiment
entries may additionally retain their existing group label. There is no
`job_id` observer form. Scheduler commands and helper-generated commands use
this same schema and the recorded execution host.

Check exit zero means the named work is gone; one means it remains present;
other results are unobservable. Completion is an operational observation, not
scientific success. Error backoff, grouping, target binding, native-session
continuation, budget spend, and Stop fences remain owned by the
[watcher spec](conversations-episodes-and-watchers.md).

The job UI reads external watcher records from
`GET /api/projects/{project_id}/watchers`. It shows observation state, log path,
check diagnostic, and cancellation history. There is no separate compute-job
list API. The backend exports `can_cancel`; the browser never derives it from
watcher status.

A human clicks Cancel through
`POST /api/projects/{project_id}/watchers/{watcher_id}/cancel`. The route requires
project membership, write admission, and an attributed human. Foreign-project
ids return 404. RCP first runs a fresh saved check. Already completed work is
observed complete without attributing its exit to Cancel. A failed check records
why Cancel was not run. Otherwise an atomic durable claim records the human and
time, then executes the saved cancel command in the same bounded shell on the
recorded host and working directory.

Concurrent requests share the claim. A successful command means **Cancel
requested**, not proof of cancellation or a new watcher lifecycle state. Its
receipt disables further requests. A failed command records a bounded diagnostic
and allows an explicit retry; a timeout reports an unknown outcome. Polling
continues to determine whether observed work is gone. A process interruption
between claim and result leaves the request recorded and is not automatically
replayed.

Stop and pause never execute cancellation. A stopped watcher may still describe
live work and keeps its Cancel action. Its fresh check can record completion
without reopening delivery. Arming, polling, and Stop never run `cancel_command`.

## Durable state and transfer

Internal helper rows retain the OS identity, roots, requester lineage, and
receipts for recovery. Background reconciliation does not block startup and
uses the saved execution identity even when the project manifest or graph
history is unavailable. Transport errors remain diagnostics rather
than evidence of completion. Helper job directories are not included in
whole-database backup payloads; restoring a row does not recreate its outputs.
Project deletion refuses while recorded helper jobs remain running.

Project transfer retains watcher worker identity and cancellation attribution,
time, and diagnostics as history. It strips executable check and cancel actions
and does not move or cancel external work. Offline restore also clears every
watcher's cancel action, including already-stopped watchers, while detaching
continuations. Ordinary restart retains watchers and their human actions.
