# Space settings: machine cards and writable paths

Date: 2026-09-27
Status: design confirmed by the human on 2026-09-27. A first design, which
moved machine definitions out of project manifests, failed a Codex xhigh
review. The human then chose this smaller model: project manifests stay the
source of truth for their machines, and the space adds a machine list and
writable paths. This design needs a second review before implementation
starts. Nothing is implemented.

Close this handoff when a team member adds `/data/shared/huggingface` as a
writable path on the GPU machine in space Settings. After that, a new member
terminal, a Codex Work turn, and a systemd compute job on that machine must
each be able to write into it, with the checks below passing.

## The problem

A user on the team server ran a script that fills the shared Hugging Face
cache. It failed with "missing or not writable". No RCP surface could fix it:

- Every write scope is built from registered repositories only
  (`ProjectWriteScope.writable_roots` in `src/rcp/agents/write_scope.py`).
- The member terminal runs under `ProtectSystem=strict`, with only the
  checkout bind-mounted writable (`src/rcp/terminals/profile.py`).
- Systemd compute jobs get `ProtectSystem=strict` plus `ReadWritePaths` for
  the write roots only (`src/rcp/compute_jobs/backends/systemd_user.py`).
- Codex Work gets the same roots in its permission profile.
- Only Claude Work could write there, through its unbounded shell. That is the
  accepted gap from `docs/decisions/2026-09-13-claude-work-runs-without-the-os-sandbox.md`,
  not a feature.

The only way out was a shell outside RCP, which makes it a product defect.

The Settings page also mixes space-wide things (provider sign-in, the server)
with project things on one project page. A new project cannot reuse a machine
another project already set up.

## Settled

1. **Two settings levels.** Each space (personal, and each team) gets a space
   Settings page, opened from the identity menu next to the user profile.
   Project Settings keeps only what belongs to one project.
2. **The space keeps a machine list.** A space machine is a host, an OS
   account, a display name, and its writable paths.
3. **Project manifests stay the source of truth for their machines.** A
   project picks a machine card from the space list, and RCP copies its host
   and account into the manifest, as today. Nothing moves out of manifests,
   and no space edit writes into a manifest.
4. **Provider paths and compute stay per project.** They are the project's
   choice of which binary to run and how to run jobs. They are edited on the
   project's machine card. Only provider sign-in is space-wide.
5. **Writable paths live only in the space**, keyed by host and account. One
   list feeds every launch on that machine.
6. **Any team member may edit space settings.** Team spaces have no admin role,
   and this change adds none.

## Where each current section goes

| Space Settings | Project Settings |
|---|---|
| Server status (team only) | Project home, Members |
| Machines: cards with host, account, writable paths, and the projects using each | Machines: the cards this project picked, each with its provider paths and compute |
| Provider logins (sign-in) | Project boundary: repositories and truth scope |
| Clear caches for every project (personal space only) | Compute connections |
| | Agent defaults, Skills & workflows, Project cache |

Team spaces keep cache clearing per project, because the current spec forbids
a team-wide clear (`src/rcp/api/index.py` enforces it). Display (theme, text
size) is a per-device preference and moves to the identity menu.

## Space machines

- New SQLite table: a random id, a display name, host, OS account, writable
  paths. `(host, os_account)` is unique in a space. An empty host is the RCP
  host itself. The account is matched as written: an empty account is its own
  key, not a wildcard.
- **Filling the list.** Whenever a project manifest is loaded or saved, RCP
  inserts a space machine for any `(host, os_account)` it does not have yet.
  The insert is idempotent (a unique key plus insert-or-ignore), so concurrent
  loads are safe. It writes SQLite only, never a manifest. Existing projects
  therefore fill the list with no migration step.
- **Host and account cannot be edited on a machine a project uses.** Changing
  them would mean relocating the project, and relocation is out of scope. A
  machine in use cannot be deleted either. An unused machine can be edited or
  deleted.
- **Provider sign-in reads this list** (`src/rcp/api/provider_login.py`,
  `src/rcp/runs/provider_sign_in.py`). Today it iterates project manifests, so
  a machine no project uses yet cannot be signed in.

## Writable paths

### Who gets them

Writable paths extend repository write roots. A launch gets them exactly when
it already receives repository write roots:

| Launch | Repository write roots today | Gets writable paths |
|---|---|---|
| Member terminal | Its checkout | Yes |
| Work turn, both providers | Admitted repositories | Yes |
| Auto-research orchestrator episode | Admitted repositories | Yes |
| Compute job launched from one of the above | Inherited | Yes |
| Graph-only branch merge | None, scratch only | No |
| Discuss | None | No |
| Ingestion | None, scratch only | No |
| Paper coach | None, read-only | No |

This is structural, so the branch merge's check that its roots are exactly its
scratch workspace (`src/rcp/runs/branch_merge.py`) keeps holding.

### What a path may be

A writable path is the human's explicit space-wide grant: every eligible
launch on that machine, in every project, can write there. The page says so.
Broad paths such as `/home/rcp`, `/tmp`, or a folder holding other projects'
repositories are allowed.

Saving refuses only:

- a relative path or a path that does not exist on the machine
- `/` itself, which would remove the mount profile altogether
- colons, control characters, or `$`, which the terminal mount syntax and older
  systemd reject or expand

### Protected paths stay read-only inside any writable path

Some paths must stay read-only whatever the grant, because invariants depend
on them. They are carved out as read-only inside a writable path, the same way
`.research` is already read-only inside a writable checkout:

- every registered repository's and state repository's `.research`
  (invariants 1, 2, 6)
- the RCP data directory and the installed service tree (invariants 6, 8)
- provider credential homes (`docs/decisions/2026-09-14-provider-logins-are-kept-alive.md`:
  a stray write breaks the shared login)
- SSH keys and Git identity files (terminals already mount these read-only)

This protected set is one list built next to `protected_repository_paths` in
`write_scope.py`. Each backend already supports read-only inside writable:

| Surface | Writable | Read-only inside it |
|---|---|---|
| Member terminal | `BindPaths` | `BindReadOnlyPaths` + `ReadOnlyPaths` |
| systemd compute job | `ReadWritePaths` | `ReadOnlyPaths` |
| Codex | permission-profile write roots | profile read-only overrides |
| Claude | `Edit(path)` allow | `Edit(path)` deny; the shell stays unbounded, as before |

Paths are canonicalized on the execution host at every launch. Mounts and the
protected carve-outs use canonical paths, so a symlink cannot route around a
carve-out. A path missing at launch refuses the launch with a message naming
it and linking to the machine card.

### One list, every launch

`ProjectWriteScope` gains the machine's writable paths, and its
`writable_roots` and protected paths include them. systemd, Codex, and Claude
already read those two lists. The member terminal builds its own mounts from
`resolve_repository` today; it changes to read the same two lists from the same
resolver, through both its local and remote launcher. The terminal preflight
checks that each writable path is writable, next to its existing checks. The
prompt renderer (`src/rcp/agents/prompts.py`) gets a writable-paths line
rendered from the same scope object.

### When a change takes effect

- **New launches only.** A running terminal keeps its mounts. The terminal's
  reuse check (`manifest_registration` in `terminals/manager.py`) includes the
  resolved writable paths, so a stale terminal shows "restart to apply" instead
  of being reused silently.
- **Continuations**: the scope fingerprint includes writable paths only when
  there are any, so every existing fingerprint stays valid. A changed set
  follows the existing changed-scope rule for resumed sessions.
- **Running jobs** keep the scope they were launched with.

## Backup, restore, transfer

- The space machine table is space data. Backup includes it, and restore
  brings it back. It needs a schema migration, fresh and upgraded
  fingerprints, a restore fingerprint, and a transfer disposition.
- An older backup has no table. Restore leaves it empty and it refills from
  manifests on load, with no writable paths.
- A personal-to-team transfer does not carry writable paths. The team grants
  its own. Target machines appear from the transferred manifest on load.
- Project manifests, provisioning, and history are unchanged.

## UI

- **Space Settings**: Server (team), Machines as cards (name, host, account,
  writable paths editor, projects using it, **+ New machine**), Provider logins,
  and Clear all caches (personal only).
- **Project Settings**: Machines shows the cards this project uses, each with
  its provider paths and compute. **Add machine** picks a card from the space
  list or creates a new one.
- **Setup and Add repository** pick a machine card, or create one inline, so a
  new user never leaves the flow.
- A terminal or job error for a read-only path names the path and links to the
  machine card.

## Checks

- Unit: loading a manifest fills the machine list idempotently; editing host
  or account on a machine in use is refused; each refused path class is
  refused at save; each eligible launch carries the paths and the protected
  carve-outs into the terminal, systemd, Codex, and Claude arguments; the
  branch merge, Discuss, ingestion, and paper coach scopes carry none; a
  symlinked path mounts its canonical target with carve-outs intact.
- Upgrade: an immutable pre-change database upgrades and serves.
- Backup and restore of a new backup and of an older one.
- Served app: in space Settings add a writable path, then write into it from a
  new terminal on the team server, a Codex Work turn, and a systemd compute
  job. Confirm a write to a `.research` path under it still fails and an open
  terminal shows the restart notice. Pick a machine card in a new project's
  setup. Inspect network, console, and server logs.

## Docs to update when this lands

- `docs/design.md` and `docs/specs/providers-and-containment.md`: writable
  paths, who gets them, and the protected carve-outs.
- `docs/specs/projects-spaces-and-operations.md`: the space machine list.
- `docs/specs/api-web-and-desktop-projections.md` and
  `docs/specs/interface-and-visual-design.md`: the two Settings levels and
  machine cards.
- A new decision record: invariant 4 fixes which launches may write; humans
  choose the roots; protected paths stay read-only inside any grant.

## Estimate

About 4 to 5 days: 1 for the machine list and provider sign-in, 2 for writable
paths across the four launches, 1 to 2 for the two Settings pages, cards, and
setup, and half a day for the upgrade, restore, and served-app drives.
