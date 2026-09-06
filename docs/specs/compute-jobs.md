# Compute jobs

RCP owns launch and durable observation; the execution machine's process owner
owns the computation. Work and Experiment-loop turns can launch, inspect, and
cancel jobs through the staged command client, then hand observation to RCP.
Child Work continuation, web Cancel availability, and agent prompt
instructions remain in the [compute runner handoff](../handoffs/handoff-2026-09-06-compute-runner.md).

## Backend profiles

`compute_jobs.backends` is one registry of profiles. Each profile owns support
detection, start, alive, cancel, and facility checks. Shared task execution gains
no backend selector. All backend commands use one injectable command runner;
remote commands use RCP's ordinary SSH argument construction. Shipped remote
helpers are stdlib-only source modules.

- `systemd_user` uses transient `rcp-job-<job_id>` user units with `--collect`,
  explicit log paths. Every user-manager command sets
  `XDG_RUNTIME_DIR=/run/user/<target uid>` even from a session-less service.
- `launchd` bootstraps a job-root plist into `gui/<uid>`, with `RunAtLoad=true`
  and `KeepAlive=false`. It never uses `launchctl submit`.
- `ssh_session` is offered only remotely on Linux. The shipped launcher creates
  a separate session and records PID plus `/proc/<pid>/stat` start time. Liveness
  checks both values; cancellation terminates the owned process group, escalating
  from SIGTERM to SIGKILL. PID reuse cannot identify a different job as this job.
- `slurm` submits through `sbatch --parsable`, with machine-owned account,
  partition, and extra submission arguments. Liveness tests membership in the
  whole `squeue -h -o %A` active set; a scheduler command failure is unknown,
  never completion. Cancellation uses `scancel`.

An explicit backend wins resolution. Otherwise Linux with a reachable user
manager selects `systemd_user`; remote Linux without one selects `ssh_session`;
macOS selects `launchd`. Other machines, including local Linux without a user
manager, have no automatic backend. There is no local subprocess backend.

## Machine configuration and roots

An optional `machines[].compute` block contains `backend`, `jobs_root`,
`slurm_account`, `slurm_partition`, and `slurm_submit_args`. Backend ids derive
from the registry. Slurm fields require an explicit Slurm backend. Metadata is
one-line, rejects credential-shaped text, and provides no credential channel.
A configured jobs root must be absolute. Existing manifests need no changes.
The manifest writer validates and atomically replaces the TOML while preserving
other machine settings.

Local roots are `<data_dir>/jobs/<job_id>` regardless of configured `jobs_root`.
Remote roots are `<jobs_root>/<job_id>`, defaulting to `~/.rcp/jobs` expanded on
the execution account. These private durable directories are outside task stages
and their retention. A job id grants no filesystem authority.

Project transfer excludes machine-bound compute rows and job directories; it
does not relocate or cancel OS-owned work. Whole-database backups retain job
rows, while their filesystem payload excludes local `jobs` directories. Job
outputs remain on the execution machine and need their own retention or backup;
restoring the database does not recreate them. Project deletion refuses while
compute jobs are running and removes terminal job rows in its database
transaction; job directories on disk are not removed.

## Wrapper and receipts

One RCP-authored POSIX `run.sh` starts from the job root, writes `started` as
epoch seconds, changes to the absolute requested working directory, and runs
the shell-quoted argv with both output streams appended to `log`. A failed
directory change also records failure.
It atomically publishes `exit` as `<status> <epoch>`; scheduler accounting is not
an exit-status source. On Linux it captures its cgroup for probe verification.
`command.json` preserves argv, cwd, optional label, and requester lineage.

`command.json` is the pre-start intent receipt. After the backend starts,
`launch.json` records the backend handle before SQLite insert. An interrupted
start/insert leaves inspectable evidence in the durable job root. A successful
launch returns only after the running row and handle exist.
A known failed start removes its root; an ambiguous submission timeout or a
failed post-start receipt or database write
retains evidence for operator repair. Automatic orphan-directory adoption is not
implemented. Log-tail reads have a fixed byte ceiling.
A remote transport failure at launch is uncertain and retains the job root.

## Probe and containment

A probe resolves the execution machine, checks its facility, launches a bounded
trivial job through the real backend, observes it alive, and requires an exit of
zero and the `rcp-probe` log marker. The caller supplies the data directory and
receives the result. SQLite's `compute_backend_probes` table retains the latest
`ComputeBackendProbe` JSON and `probed_at` for each `(project_id,
execution_machine)`. Agent launch reads that result; when none exists, it runs
one probe and stores the result before deciding. A stored result that is not
ready is re-run at the next launch and replaced; if it is still not ready, launch
is refused with the fresh diagnostic and required action.
A stored probe whose backend no longer matches current resolution is re-run once
at launch and replaced.
Missing automatic resolution is `unavailable` with the action
"configure a compute backend for this machine"; execution failures are `failed`
with redacted single-line diagnostics and an action.

The public launch function still resolves the configured backend; the task owner
owns probe admission and passes the turn's writable roots to that function.

On local Linux, the job's captured cgroup must differ from the RCP process's
`/proc/self/cgroup`; a shared service cgroup fails the probe. Remote cgroup paths
cannot establish separation from a process on another host, so that comparison
is unknown there. Passing a short probe proves the observed launch and exit, not
survival of a future machine reboot, account logout, or scheduler outage.

The systemd probe first attempts `PrivateUsers=yes`, `ProtectSystem=strict`,
`ProtectHome=read-only`, and exact `ReadWritePaths`. If that attempt fails but a
cooperative attempt passes, the result explicitly records that limitation. A
mirrored backend start applies those properties to the supplied writable roots
and its job root. Agent launch uses mirrored containment when the stored probe
proved support.
Other backends are cooperative-only. These are accidental-write guardrails for cooperative users, without read secrecy, network confinement, or
hostile same-account isolation claims.

## Setup and human control

Install enables and verifies linger for the service account. The installed-service
CLI `rcp server compute probe --project <project_id> <machine_alias>` and
`POST /api/projects/{project_id}/machines/{machine_alias}/compute/probe` run the
same backend probe under the service account and store its result. The CLI
prints label, backend id, containment, diagnostic, and required action, returning
0 only when ready and 1 otherwise. The API returns `ComputeBackendProbe` directly.

Project settings accept `machine_compute`, a partial map from machine alias to
`MachineComputeConfig`: omission preserves a machine and null removes its block;
an unset backend selects automatic resolution. Writes use the manifest writer.
Changing a compute block deletes that machine's stored probe before publication,
so the next probe or launch verifies the new settings. An unchanged block keeps
its probe. Project machine entries carry `compute` and the live stored
`compute_probe` (or null), including on cached project reads.

`GET /api/projects/{project_id}/compute-jobs` refreshes running project rows on
read, then returns `ComputeJobRecord` rows newest first with the existing list
limit. Records include the optional launch label; pre-label records have null.
No new polling worker is added. Human
`POST /api/projects/{project_id}/compute-jobs/{job_id}/cancel` requires project
write admission and the same named human identity as Stop. It records the human's
user id and the first cancellation timestamp, calls the backend, and returns the
row; terminal cancellation is an unchanged 200 response. Jobs from another
project are not visible through either route. Stop and pause never cancel jobs.

Experiment-loop and Auto-research human episode starts require a ready stored
probe for the resolved execution machine. When absent, admission runs and stores
one first. A failed or unavailable probe refuses with 422 naming the machine,
diagnostic, and required action, before reserving an episode. Ordinary human
Work is not gated. The concrete request admission owns this check; shared
background execution does not.

## Turn-bound agent commands

Explicit graph-repair tasks retain their existing validation-only policy.
The staged client serves these keyed commands in ordinary Work and
Experiment-loop turns, including their same-invocation correction turns:

- `launch --key K --cwd <absolute-path> [--label <text>] -- <argv...>` returns
  `{job_id, log_path, backend_id}`. The working directory must be inside the
  turn's writable roots. Repeating the same keyed intent returns the same job id
  and starts no additional job.
- `job-status --key K <job_id>` refreshes a project-owned job and returns
  `{status, exit_status, started_at, ended_at, log_tail}`. The command's log tail
  reads at most 1 KiB so its replayable response fits the diagnostic receipt.
- `cancel --key K <job_id>` records the task operation id as requester and calls
  the backend. Repeating the same key is a no-op.

Execution machine, host, project, operation id, and optional episode id come
from the turn, never the command. A bad request answers `invalid`; a machine
without a ready probe answers `unavailable` with its diagnostic and required
action; success answers `ok`. Each call has a task event; each new keyed command
has two protected diagnostic receipts, one for its start and one for its result.
Commands do not apply a graph Patch.

Idempotency is scoped to the task operation, verb, and key, including correction
turns within that operation. The start receipt retains an argument digest, and
the result receipt retains the response; both survive ordinary diagnostic
retention. Reusing a key with different arguments is invalid. An interrupted
call without a result stays unavailable with an uncertain-outcome diagnostic;
replay never guesses that a missing response means nothing started.

Broker authority is explicit and independent of episode identity. One live
provider process tree binds each turn, with a fresh binding for correction;
remote turns run the broker on their execution host. A validate-only identity
cannot issue keyed commands. Auto-research retains its episode identity and
per-request signatures, and stale processes cannot command a later Work turn.
Task owners retain policy: child Work, the Auto-research root, Discuss, graph
merge, Seed/Refresh, and Paper do not serve these compute verbs.

## Job observers and settlement

An external item in `watch.json` may be `{"job_id": "<id>"}` instead of the
closed shell form. Experiment items may also carry their existing `group` label;
shell and job items may coexist in one list. A stored job observer has no shell
fields, and a stored shell observer has no job id.

Arming requires a job in the same project and task lineage: the same origin
operation, or the same episode for an Experiment-loop. A refreshed `exited` or
`cancelled` job arms completed. An initially lost job rejects the handoff like
an unobservable initial shell check. Polling uses `refresh_compute_job`, never an
agent shell: `running` stays active, `exited` or `cancelled` completes, and `lost`
degrades with its diagnostic instead of completing. Completion says the process
ended, not that its computation succeeded.

After reading the final watcher declaration, settlement refreshes jobs launched
by that turn. Any still-running job missing a job observer is a correctable
handoff defect naming every unobserved job id. It enters the existing correction
round without spending an invocation; arming remains after settlement. Exited
jobs need no observer, and a turn that launched nothing is unaffected.

Each delivered job observer contributes `job_id`, `exit_status`, `started_at`,
`ended_at`, `duration_seconds`, `log_path`, and `backend_id` to the Experiment
watcher-state file or the generic Work wake message. Existing target, coalescing,
admission, and Stop fences still own delivery. Stop and pause do not cancel jobs.
Duration is end time minus start time, or null when either timestamp is absent.

## Durable state and startup

SQLite records requester lineage, execution alias and host, backend id and opaque
handle, paths, argv, containment, lifecycle, timestamps, cancellation attribution,
and diagnostics. Reconciliation runs in background maintenance after startup;
failures are logged and never block startup. After a backend is unreachable, the
same diagnostic is recorded on remaining rows for that execution host in the
pass without contacting it again. Reconciliation uses the saved backend and
execution identity rather than retargeting old jobs after configuration changes.

Alive remains `running`. Unknown also remains `running`, with a diagnostic even
when an exit file exists. Once gone, a valid exit file yields `exited`; its absence
yields `lost`. A job with a recorded cancellation request becomes `cancelled`
once gone. Cancellation preserves the first requester and time and is idempotent
for terminal jobs. Stop and pause do not call it. Terminal records stay terminal.
Restore rehearsal rebinds local job paths to known-absent overlay locations, as
it does watcher paths; remote paths retain their execution-host qualification.
