# Space settings own machines, and machines name extra writable paths

Date: 2026-09-27
Status: design confirmed by the human on 2026-09-27 (machines move to the
space; writable paths apply to agents as well as terminals). Nothing is
implemented. The design needs one review round before implementation starts.

Close this handoff when a team member adds `/data/shared/huggingface` to the
GPU machine in space Settings, and then a member terminal, a Codex Work turn,
and a systemd compute job on that machine can each write into it, with the
checks below passing.

## The problem

A user on the team server ran a script that fills the shared Hugging Face
cache. It failed with "missing or not writable". No RCP surface could fix it:

- Every write scope is built from registered repositories only
  (`ProjectWriteScope.writable_roots` in `src/rcp/agents/write_scope.py`).
- The member terminal runs under `ProtectSystem=strict`, with only the
  checkout bind-mounted writable (`src/rcp/terminals/profile.py`).
- Systemd compute jobs get `ProtectSystem=strict` plus `ReadWritePaths` for
  the write roots only (`src/rcp/compute_jobs/backends/systemd_user.py`).
- Codex Work receives the same roots as its sandbox writable roots.
- Only Claude Work could write there, through its unbounded shell. That is the
  accepted gap from `docs/decisions/2026-09-13-claude-work-runs-without-the-os-sandbox.md`,
  not a feature.

The only way out was a shell outside RCP. That counts as a product defect.

A second problem sits under it. Machines are defined inside each project's
`manifest.toml` (`MachineConfig`: host, OS account, provider paths, compute).
Two projects on one host each carry their own copy. The Settings page mixes
these machine facts, space-wide provider logins, and real project settings
on one project page.

## Settled

1. **Two settings levels.** Each space (personal, and each team) gets a space
   Settings page, opened from the identity menu next to the user profile.
   Project Settings keeps only what belongs to one project.
2. **The space owns machines.** A space machine record holds host, OS account,
   provider paths, job manager, jobs root, and the new writable paths.
   Projects reference space machines and no longer define them.
3. **Writable paths apply to agents too.** They join the write scope for
   member terminals, Work and orchestrate launches (both providers), and
   compute jobs launched from them. Discuss, ingestion, and paper coach do not
   get them; their capabilities are unchanged.
4. **One source of truth.** The space machine record is the only place these
   paths live. `resolve_project_write_scope` reads them, and every backend
   consumes the resolved scope. No backend keeps its own list.
5. **Any team member may edit space settings.** Team spaces have no admin role,
   and this change does not add one.

## Where each current section goes

| Space Settings | Project Settings |
|---|---|
| Server status (team only) | Project home |
| Machines, including writable paths | Members |
| Provider logins (already space-wide in the API) | Project boundary: repositories, truth scope, and each repository's machine |
| Clear caches for every project | Compute connections |
| | Agent defaults, Skills & workflows, Project cache |

Display (theme, text size) is a per-device preference. It moves to the
identity menu, not to either Settings page.

## Design

### Data model

- New SQLite table for space machines: a stable random id, a display name,
  host, OS account, provider paths, compute block, and writable paths. This
  needs a schema migration, a restore fingerprint, and a transfer disposition.
- Machine identity for matching is `(host, os_account)`. An empty host is the
  RCP host itself. Two spellings of one host stay two machines; there is no
  automatic merge.
- A project machine alias stays in the manifest as `[[machines]] alias = "…"`.
  Aliases are historical identifiers: transfer already refuses a renamed
  alias (`src/rcp/transfer/configuration.py`). So the alias is kept, and a new
  SQLite binding table maps `(project_id, alias)` to a space machine id.
- One resolver builds the `Manifest` that consumers see. It fills each
  `MachineConfig` from the bound space machine. Most of the ~40 modules that
  read `manifest.machine_map` stay unchanged.

### Migration of existing projects

- On first load of a project with no bindings, RCP imports its inline machine
  definitions. Each `(host, os_account)` binds to an existing space machine or
  creates one from the manifest's fields. This writes SQLite only. It never
  publishes the state repository during a read.
- If two projects disagree about the same machine (for example, different
  provider paths), the first imported value wins. The machine shows a visible
  notice in space Settings naming the project whose value was not kept. It is
  never silent.
- Inline fields left in an old manifest are ignored after import. The next
  normal manifest publication drops them.
- Remote-state projects import on their first successful read. Startup never
  waits on SSH for this.

### Writable paths

- Each path must be absolute, and it is canonicalized on its machine at save
  time and again at every launch.
- Refused at both times: `/`, the RCP data directory, provider credential
  homes, any state repository, and any path that contains or sits inside a
  protected `.research` path. A refusal at launch fails the launch with its
  diagnostic; it is never dropped silently.
- The existing protected read-only mounts still win inside a writable path.
- A path missing on its machine refuses the launch with a message naming it,
  so a typo is visible.

### Enforcement per surface

| Surface | How the paths apply |
|---|---|
| Member terminal | Extra `BindPaths`; the preflight checks each one is writable |
| systemd compute job | Extra `ReadWritePaths` (already driven by `writable_roots`) |
| Codex Work / orchestrate | Extra sandbox writable roots |
| Claude Work / orchestrate | Extra `Edit(path)` allow rules; the shell stays unbounded as before |
| Slurm jobs | No change; they carry no mount containment |

Prompt text that describes write roots renders from the same resolved scope,
per the existing rule.

### Invariant 4

Invariant 4 says configuration cannot widen agent capability. Registered
repositories already set write roots through configuration, so the reading
is: the capability (which kinds of launch may write) is fixed in code, and a
human chooses the roots. Writable paths are more roots. A decision record
states this, so a later change cannot use the same reasoning to widen
Discuss or paper coach.

### UI

- Space Settings: Server, Machines (each with provider paths, compute,
  writable paths, and the migration notice), Provider logins, Clear all caches.
- Project Settings: each repository picks a space machine from a list. The
  setup flow can create a space machine inline, so a new user is never sent
  elsewhere to finish.
- The terminal and job error for a read-only path names the path and links to
  the machine's writable paths in space Settings.

### Also keyed by machine

Compute probes are stored per `(project_id, machine_alias)`. Terminal probes
are cached per machine. Both move to the space machine id, so one check
serves every project on that machine.

## Checks

- Unit: the resolver fills machines from the space; migration merges equal
  machines and reports a disagreement; each refused path class is refused at
  save and at launch; the resolved scope carries writable paths into the
  terminal, systemd, Codex, and Claude launch arguments.
- Migration on copies of real data: the personal desktop data and a team
  server copy. Every project opens, and each project's projection hash is
  unchanged.
- Transfer and restore: a personal-to-team transfer binds the aliases to the
  target space's machines, and a restore brings back the machines and
  bindings.
- Served app: open space Settings, add a writable path, then in a real
  terminal on the team server write into it. Repeat with a Codex Work turn and
  a systemd compute job. Inspect network, console, and server logs.

## Docs to update when this lands

`docs/specs/projects-spaces-and-operations.md` (the space owns machines),
`docs/specs/providers-and-containment.md` (writable paths in every scope),
`docs/specs/api-web-and-desktop-projections.md` and
`docs/specs/interface-and-visual-design.md` (the two Settings levels), and a
new decision record for the invariant 4 reading.

## Estimate

About 3 to 5 days: 1 day for the data model, resolver, and migration; 1 day
for the write scope and the four backends; 1 to 2 days for the two Settings
pages and setup; half a day or more for the migration and served-app drives.
