# Compute jobs

RCP owns launch and durable observation; the execution machine's process owner
owns the computation. This foundation adds no agent-visible behavior. Agent
launch verbs, job observers, setup surfaces, and human Cancel controls are not
yet implemented; their sequence is the [compute runner handoff](../handoffs/handoff-2026-09-06-compute-runner.md).

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

## Probe and containment

A probe resolves the execution machine, checks its facility, launches a bounded
trivial job through the real backend, observes it alive, and requires an exit of
zero and the `rcp-probe` log marker. The caller supplies the data directory and
receives the result. Missing automatic resolution is `unavailable` with the action
"configure a compute backend for this machine"; execution failures are `failed`
with redacted single-line diagnostics and an action.

Launch resolves the backend directly. If none resolves, it raises a `RuntimeError`
naming the machine and asking to configure a compute backend.

On local Linux, the job's captured cgroup must differ from the RCP process's
`/proc/self/cgroup`; a shared service cgroup fails the probe. Remote cgroup paths
cannot establish separation from a process on another host, so that comparison
is unknown there. Passing a short probe proves the observed launch and exit, not
survival of a future machine reboot, account logout, or scheduler outage.

The systemd probe first attempts `PrivateUsers=yes`, `ProtectSystem=strict`,
`ProtectHome=read-only`, and exact `ReadWritePaths`. If that attempt fails but a
cooperative attempt passes, the result explicitly records that limitation. A
mirrored backend start applies those properties to the supplied writable roots
and its job root. Internal job launches currently use cooperative containment.
Other backends are cooperative-only. These are accidental-write guardrails for cooperative users, without read secrecy, network confinement, or
hostile same-account isolation claims.

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
