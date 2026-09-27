# Space settings own machines, and machines name extra writable paths

Date: 2026-09-27
Status: design confirmed by the human on 2026-09-27 (machines move to the
space; writable paths apply to agents as well as terminals). A Codex xhigh
design review found the first draft not ready (five blocking, five
should-fix). This revision answers every finding. It needs a second review
before implementation starts. Nothing is implemented.

Close this handoff when a team member adds `/data/shared/huggingface` to the
GPU machine in space Settings, and then a new member terminal, a Codex Work
turn, and a systemd compute job on that machine can each write into it, with
the checks below passing.

## The problem

A user on the team server ran a script that fills the shared Hugging Face
cache. It failed with "missing or not writable". No RCP surface could fix it:

- Every write scope is built from registered repositories only
  (`ProjectWriteScope.writable_roots` in `src/rcp/agents/write_scope.py`).
- The member terminal runs under `ProtectSystem=strict`, with only the
  checkout bind-mounted writable (`src/rcp/terminals/profile.py`).
- Systemd compute jobs get `ProtectSystem=strict` plus `ReadWritePaths` for
  the write roots only (`src/rcp/compute_jobs/backends/systemd_user.py`).
- Codex Work receives the same roots as its permission profile.
- Only Claude Work could write there, through its unbounded shell. That is the
  accepted gap from `docs/decisions/2026-09-13-claude-work-runs-without-the-os-sandbox.md`,
  not a feature.

The only way out was a shell outside RCP. That counts as a product defect.

A second problem sits under it. Machines are defined inside each project's
`manifest.toml` (`MachineConfig`: host, OS account, provider paths, compute).
Two projects on one host each carry their own copy. The Settings page mixes
these machine facts, space-wide provider logins, and real project settings.

## Settled

1. **Two settings levels.** Each space (personal, and each team) gets a space
   Settings page, opened from the identity menu next to the user profile.
   Project Settings keeps only what belongs to one project.
2. **The space owns machines.** A space machine holds host, OS account,
   provider paths, job manager, jobs root, and writable paths. Projects bind
   their machine aliases to space machines.
3. **Writable paths apply to agents too**, but only where a concrete launch
   owner admits them (see "Who gets writable paths").
4. **One source of truth.** The space machine is the only editable home of
   these facts. Old per-project machine-editing APIs are retired, not kept in
   parallel.
5. **Any team member may edit space settings.** Team spaces have no admin
   role, and this change adds none.

## Where each current section goes

| Space Settings | Project Settings |
|---|---|
| Server status (team only) | Project home |
| Machines: provider paths, compute, writable paths | Members |
| Provider logins | Project boundary: repositories and truth scope |
| Clear caches for every project (personal space only) | Compute connections |
| | Agent defaults, Skills & workflows, Project cache |

Team spaces keep cache clearing per project. The current spec forbids a
team-wide clear because project membership does not authorize clearing
another project's caches (`src/rcp/api/index.py` enforces it).

Display (theme, text size) is a per-device preference. It moves to the
identity menu.

## Data model

- **Space machine**: a random id, a display name, host, OS account, provider
  paths, compute block, writable paths, and a revision number that increases
  on every edit. `(host, os_account)` is unique within a space. An empty host
  is the RCP host itself. Two spellings of one host stay two machines.
- **Host and account are fixed once any project references the machine.**
  Changing where a project runs is a relocation (checkouts, credentials,
  canonical state location, active work). Relocation is out of scope. To move,
  a user creates a new machine, and a later relocation workflow rebinds.
  Provider paths, compute, and writable paths stay editable.
- **Binding**: `(project_id, alias) -> machine id`, in SQLite. The alias stays
  in the manifest because it is a historical identifier: transfer refuses a
  renamed alias, and repositories, agent profiles, and task records name it.
  All repositories sharing an alias share its binding by design.
- **Import marker**: per project, the alias set imported and the manifest
  commit it came from.

New tables need a schema migration, fresh and upgraded fingerprints, a
restore fingerprint, a transfer disposition, and an immutable upgrade fixture
(`docs/decisions/2026-08-27-server-schema-compatibility.md`).

## Resolving a manifest

Three separate operations, so no reader guesses:

1. **Raw parse** reads `manifest.toml` as written. It never fills machine
   facts and never turns an alias into a local machine by default. The
   current `host = ""` default stops applying to alias-only entries.
2. **Live resolution** binds each alias through the space's registry before
   path normalization and before `prepare_state_workspace` picks a local or
   remote state workspace. An unbound alias refuses with an action to bind it.
3. **Frozen resolution** takes an explicit machine snapshot instead of the
   live registry. Backup capture, restore, upgrade rehearsals, offline replay,
   and retained-state preflight use it. It never reads or writes the live
   registry.

Every path that loads a manifest goes through one of these, including history
reloads and branches (`history/manager.py`, `history/branches.py`), bootstrap
mirrors, unopened-project inventory, previews, background admission, and
operator reads. The catalog already stores each project's state location, so a
remote-state project can be reached before its manifest is resolved.

A project met on a server without its bindings (a copied data directory, a
restored older backup) needs an explicit bind, or it refuses. It never
becomes local through defaults.

## Migration of existing projects

- **One transaction per project, under one space-wide import lock.** Matching
  on `(host, os_account)`, creating machines, choosing values, recording
  conflict notices, writing bindings, and writing the import marker commit
  together or not at all.
- **First writer wins on disagreements.** If two projects give different
  provider paths or compute for the same machine, the machine keeps the value
  already stored, and space Settings shows a notice naming the project whose
  value was not kept. It is never silent.
- **When the import runs**: on the first load of a project with no import
  marker. For a remote-state project, on its first successful state read.
  Startup never waits on SSH for this. The import writes SQLite only; it never
  publishes the state repository.
- **After import, inline machine fields are inert.** They are migration input,
  not an editing interface. The next normal manifest publication removes them.
- **Alias set changes after import.** A new alias (for example from a
  transferred history) imports from its inline fields if present; otherwise it
  refuses until bound. A removed alias keeps its binding, unused.
- **Old write APIs**: `machine_provider_paths` and `machine_compute` in
  `ProjectSettingsRequest` return 422 with a pointer to space Settings once
  the project is imported. Old clients see a visible refusal, not a write
  that silently does nothing.

## Writable paths

### Who gets them

The concrete launch owner decides, never the capability name alone:

| Launch | Gets machine writable paths |
|---|---|
| Member terminal | Yes |
| Work turn (`work_auto`), both providers | Yes |
| Auto-research orchestrator episode | Yes |
| Compute job launched from one of the above | Yes, inherited from its scope |
| Graph-only branch merge (`orchestrate` capability) | **No.** Its check that `writable_roots` equals only its scratch workspace stays (`src/rcp/runs/branch_merge.py`) |
| Discuss, ingestion, paper coach | No |

### What a path may be

A writable path is an external directory. It must not overlap RCP-owned or
credential state. On the execution host, and for both the declared path and
its canonical target, RCP refuses a path that equals, contains, or sits
inside any of:

- `/`, the account home directory itself, and broad temporary roots (`/tmp`,
  `/var/tmp`)
- the RCP data directory and the installed service tree
- provider credential homes, Git configuration and identity files, and
  `~/.ssh`
- any registered repository or conversation worktree, in any project in the
  space, admitted or not
- any state repository and any `.research` path

This reuses the ownership-overlap and broad-root checks that repository roots
already get in `write_scope.py`.

- **Symlinks are pinned.** At save, RCP records the canonical target. At every
  launch it canonicalizes again. If the target changed, the launch refuses
  and names both paths. It never follows a retargeted link silently.
- **Encoding**: paths with colons, control characters, or `$` are refused at
  save. The terminal mount syntax and older systemd both reject or expand
  these. Claude's `Edit(...)` rules go through the existing
  `_claude_absolute_pattern` escaping.
- **A missing path refuses the launch** with a message naming it.
- Existing protected read-only mounts still win inside a writable path.

### Enforcement per surface

| Surface | How the paths apply |
|---|---|
| Member terminal | The terminal builds its mounts from `resolve_repository`, not from `ProjectWriteScope`. It calls the same writable-path validator and passes the result through both the local and the remote launcher as extra `BindPaths`. The preflight checks each one is writable, next to its existing checks. |
| systemd compute job | Extra `ReadWritePaths` through the existing `writable_roots` path, under mirrored containment. The helper launch revalidates every inherited root, not only `cwd`. |
| Codex Work / orchestrator | Extra roots through the shared permission-profile renderer, with protected read-only overrides kept. |
| Claude Work / orchestrator | Extra `Edit(path)` allow rules. The shell stays unbounded, as before. |
| Slurm jobs | No change; they have no mount containment. |

The prompt renderer (`src/rcp/agents/prompts.py`) lists repository and Git
metadata roots field by field. It gets a writable-paths field rendered from
the same resolved scope object.

### When a change takes effect

- **New launches only.** A running terminal keeps its mounts. The terminal's
  reuse check (`manifest_registration` in `terminals/manager.py`) gains the
  resolved writable paths, so a stale terminal shows "restart to apply" rather
  than being reused silently.
- **Continuations**: the scope fingerprint includes writable paths only when
  there are any, so every existing fingerprint stays valid. A changed set
  follows the existing changed-scope refusal for resumed sessions.
- **Running jobs** keep the execution identity they were launched with.

### Readiness probes

Compute probes are keyed today by project, alias, and route (scheduler or
helper). Machine-level probes move to `(machine id, route, machine revision)`.
A result for an older revision never becomes current. An edit invalidates the
machine's probes, terminal probe, and provider inventory in every project
bound to it. Project compute-connection probes stay per project.

## Provider logins

Account discovery and remote sign-in read the space machine registry
directly. Today they iterate project manifests
(`src/rcp/api/provider_login.py`, `src/rcp/runs/provider_sign_in.py`), so a
machine with no project yet cannot be signed in. The provider account is still
selected by the SSH destination; `os_account` stays a verification of that
account, not a way to pick it. Two machines that reach the same destination
with different expected accounts show a conflict notice.

## Backup, restore, rehearsal, transfer

- **Backup** records the machine snapshot and bindings from the database
  revision it copies, and checks recovery descriptors against that snapshot.
  It never mixes in live registry state.
- **Restoring an older backup** (no registry) rebuilds machines and bindings
  from its archived recovery descriptors before bootstrap and workspace
  creation. Old archive shapes stay valid.
- **Upgrade rehearsals** get an isolated machine snapshot that points at their
  disposable roots, so resolution cannot undo the local-rewrite safety overlay
  (`server_ops/application_validation.py`).
- **Provisioning** keeps its reviewed machine snapshot in the review digest,
  and revalidates it against the machine revision before execution and
  before activation.
- **Transfer (personal to team)**: the source review hash covers the resolved
  machine configuration, not only manifest bytes. It is revalidated before
  the source is fenced and before target activation. The target keeps
  historical aliases and builds its own bindings from its reviewed
  provisioning. Source machine ids and source writable paths are never
  imported. Team-to-personal transfer stays refused. Test both old-source
  with new-target and new-source with old-target.

## Implementation order

1. Space machine registry, the three resolution operations, migration,
   backup/restore/rehearsal/transfer contracts, provider logins reading the
   registry, probe keys. No behavior change for users besides the moved
   settings.
2. Writable paths: validator, per-owner admission, the four backends, prompt
   field, fingerprints, terminal restart notice.
3. The two Settings pages, the setup flow creating a space machine inline, and
   error messages that link to a machine's writable paths.

## Checks

- Unit: raw parse never defaults an alias to local; live and frozen resolution
  agree on the same snapshot; migration is atomic and reports a disagreement;
  the retired settings fields return 422 after import; each refused path
  class is refused at save and at launch; a retargeted symlink refuses; the
  branch merge scope stays scratch-only; each admitted launch carries the
  paths into terminal, systemd, Codex, and Claude arguments.
- Upgrade fixture: an immutable pre-change database and state repository
  upgrade and open.
- Migration on copies of real data: the personal desktop data and a team
  server copy. Every project opens, and each project's canonical graph and
  history hash is unchanged (the full project projection changes on purpose).
- Backup and restore of a new backup and of an older one; a personal-to-team
  transfer; an upgrade rehearsal.
- Served app: open space Settings, add a writable path, and write into it
  from a new terminal on the team server, a Codex Work turn, and a systemd
  compute job. Confirm an open terminal shows the restart notice. Inspect
  network, console, and server logs.

## Docs to update when this lands

- `docs/design.md` and `docs/specs/providers-and-containment.md`: capability
  descriptions and writable paths per launch owner.
- `docs/specs/projects-spaces-and-operations.md`: the space owns machines,
  bindings, and migration.
- `docs/specs/api-web-and-desktop-projections.md` and
  `docs/specs/interface-and-visual-design.md`: the two Settings levels.
- A new decision record: invariant 4 fixes which launches may write in code,
  and humans choose the roots. Graph-only launches stay excluded.

## Estimate

About 7 to 10 days: 3 to 4 for step 1, 2 to 3 for step 2, 1 to 2 for step 3,
and 1 for the upgrade, migration, transfer, and served-app drives.
