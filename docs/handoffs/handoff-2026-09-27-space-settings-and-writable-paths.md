# Space settings: machine cards and writable paths

Date: 2026-09-27
Status: design confirmed by the human on 2026-09-27. Two Codex xhigh reviews
found earlier drafts not ready. The first draft moved machine definitions out
of project manifests; the human replaced it with this smaller model. The
second draft protected too broad a set of paths; the human then settled the
protection rule below. This third revision answers both reviews and needs one
last review before implementation starts. Nothing is implemented.

Close this handoff when a team member picks `/data/shared/huggingface` as a
writable path on the GPU machine in Settings. After that, a new member
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
3. **Project manifests stay the source of truth for their machines.** Setup
   and project Settings pick a machine card, and RCP copies its host and
   account into the manifest. No space edit writes into a manifest.
4. **Provider paths and compute stay per project.** Only provider sign-in is
   space-wide.
5. **Writable paths live only in the space**, keyed by host and account. One
   list feeds every eligible launch on that machine. Both Settings pages edit
   that same list.
6. **Writable paths are picked with a small folder browser**, not typed.
7. **RCP constrains grants only to protect RCP itself.** Home folders, `/tmp`,
   `~/.ssh`, and other projects' repositories can all be granted. Only RCP's
   own storage stays read-only.
8. **Any team member may edit space settings.** Team spaces have no admin role,
   and this change adds none.

## Where each current section goes

| Space Settings | Project Settings |
|---|---|
| Server status (team only) | Project home, Members |
| Machines: cards with host, account, writable paths, and the projects using each | Machines: this project's cards, each with provider paths, compute, and the machine's writable paths |
| Provider logins (sign-in) | Project boundary: repositories and truth scope |
| Clear caches for every project (personal space only) | Compute connections |
| | Agent defaults, Skills & workflows, Project cache |

Team spaces keep cache clearing per project, because the current spec forbids
a team-wide clear (`src/rcp/api/index.py` enforces it). Display (theme, text
size) is a per-device preference and moves to the identity menu.

## Space machines

- New SQLite table: a random id, a display name, host, OS account, writable
  paths. `(host, os_account)` is unique in a space. An empty host is the RCP
  host itself. The account is matched as written; an empty account is its own
  key and does not select an account.
- **Filling the list happens only at live catalog boundaries**: after a
  project is registered, opened by the live catalog, or has a manifest
  publication accepted. It never happens in raw `load_manifest`, retained-
  research preflight, history reload, branch reload, backup snapshots, or
  upgrade rehearsals. Those stay pure. The insert is idempotent (unique key
  plus insert-or-ignore).
- **In use** means some registered project's accepted manifest names the
  machine. Host and account cannot be edited, and the machine cannot be
  deleted, while it is in use. If a registered project's manifest cannot be
  read (for example, its remote host is down), those edits refuse instead of
  guessing. Writable paths stay editable either way.
- **Adding a machine to an existing project** is a new additive operation
  through the canonical state workspace. It appends a machine alias and never
  changes existing aliases or repository placement. The backup check compares
  the manifest with the completed provisioning proof (`projects.py`); it
  changes to accept authorized additions while still verifying every
  provisioned machine and checkout placement.

## Provider sign-in

- Account discovery and sign-in read the space machine list, so a machine no
  project uses yet can be signed in (`src/rcp/api/provider_login.py`,
  `src/rcp/runs/provider_sign_in.py`).
- **Finding the binary.** If projects on the machine name one provider path,
  sign-in uses it. If they name different paths, sign-in uses the first one
  and shows which one it used. The login is per account, so any of them signs
  in the same account. If no project names a path, sign-in resolves the
  provider on the machine's `PATH`.
- Logins stay keyed by provider and host, as today. Two cards on one host with
  different expected accounts show a conflict notice. A card with a non-empty
  account is checked against the account SSH actually reached before sign-in,
  before a writable-path save, and before an eligible launch. Terminals already
  do this check (`transport/remote_terminal.py`). Compute resolution gains it.

## Writable paths

### Who gets them

A launch gets writable paths exactly when it already gets repository write
roots. A launch that writes only its scratch folder, or nothing, gets none:

| Launch | Gets writable paths |
|---|---|
| Member terminal | Yes |
| Work turn with admitted repositories, both providers | Yes |
| Auto-research orchestrator episode with admitted repositories | Yes |
| Compute job launched from one of the above | Yes, inherited |
| Scratch-only Work, such as episode reports (`runs/tasks/episode_report.py`) | No |
| Graph-only branch merge | No; its scratch-only check (`runs/branch_merge.py`) keeps holding |
| Discuss, ingestion, paper coach | No |

### RCP's protected storage

Code computes this list at every launch, from the same functions that create
each folder. It is never a setting.

| Where | Path | Computed from |
|---|---|---|
| RCP host | The RCP data folder | The running process's data directory |
| Team server | `releases/`, `source/`, `credentials/`, `update-checkpoints/`, `restore-operations/` under the server root; `/etc/rcp`; `/run/rcp` | The `ServerLayout` loaded from `/etc/rcp/server.toml`. Not `projects/`, which holds repositories. |
| Any machine | Provider token files: `auth.json` under the Codex home, `.credentials.json` under the Claude home | The provider homes the credential gate already locks |
| Remote machine | `~/.rcp` (jobs, task folders, command sockets) | The remote home, resolved as job folders are today |
| Remote machine | A project's custom jobs root, if set | The project's compute settings |
| Remote machine | `~/.local/share/rcp/credentials` | `remote_credentials_root` |
| Every repository | `.research` | Project manifests, as today |

**Remote task folders move** from `/tmp/rcp-run.*` to `~/.rcp/stages/`, and
command sockets from `/tmp/rcp-command-*.sock` into `~/.rcp`. A fixed folder
is covered by one rule, including folders created after a terminal opened,
and `/tmp` holds no RCP state. The socket path must stay under the 108-byte
`AF_UNIX` limit. If a long remote home would exceed it, launch refuses with
that reason rather than falling back to `/tmp`. The existing `/tmp` sweep
stays for one release to clean up old folders.

### The effective policy

1. A grant **inside** a protected path is refused, both in the folder picker
   and at save.
2. A grant that **contains** protected paths is allowed. They stay read-only
   inside it.
3. A grant never reopens a protected path.
4. **The only writable exceptions inside protected storage are the current
   launch's own stage workspace and job folder.** RCP adds them; users cannot.
5. Reading is never blocked. Agents can still read chats, logs, and records.

This policy is computed once, as three lists on the resolved scope: writable
roots, protected paths, and exceptions. Backends only render those lists.

### Rendering per backend

| Backend | Writable | Protected | Exceptions inside protected |
|---|---|---|---|
| Member terminal (mirrored) | `BindPaths` | `BindReadOnlyPaths` + `ReadOnlyPaths`; an absent directory gets a read-only empty mount, as today | None; a terminal has no stage |
| systemd job (mirrored) | `ReadWritePaths` | `ReadOnlyPaths`; absent directories get read-only empty mounts, like the terminal | `ReadWritePaths` on the exception, which systemd applies over its read-only parent |
| Codex | profile `write` roots | profile `read` entries | a more specific `write` entry. The implementation proves on the pinned Codex version that the more specific entry wins, and fails closed if not. |
| Claude | `Edit(path)` allow | `Edit(path)` deny | Claude's deny beats any allow, so a protected folder containing an exception is denied child by child, skipping the exception's ancestors. The children are enumerated at launch, so folders created later are covered only by the OS backends. That is inside the accepted Claude gap. |

- **Files and folders are distinct entries.** A protected file renders an
  exact-file rule. A folder renders `path/**`. The Claude renderer escapes
  gitignore metacharacters (`*`, `?`, `[`, `]`, `\`) in both allow and deny.
- **Canonical paths.** Grants and protected paths are canonicalized on the
  execution host at every launch, and both the declared and canonical
  spellings are protected. Writable-root validation (an existing directory)
  stays separate from protected-path resolution, which allows files and absent
  paths.
- **Helper jobs revalidate every inherited root** at job launch, not only
  `cwd` (`runs/tasks/compute_commands.py`).
- **A grant missing at launch** refuses the launch with a message naming it and
  linking to the machine card.

### Where enforcement does not exist

Cooperative launches have no filesystem enforcement today: non-Linux
terminals, cooperative systemd after a mirrored probe fails, and launchd jobs.
There, a grant adds nothing (everything the account can write is already
writable), and protected paths are not enforced. That is existing behavior.
The machine card says "not enforced on this machine" for those modes. Checks
of the carve-outs run in mirrored mode only.

### When a change takes effect

- **New launches only.** A running terminal keeps its mounts. The terminal
  record gains a separate captured copy of its grants (old records decode as
  empty). A mismatch shows "restart to apply" and keeps the session. It is
  kept apart from the repository-identity check in `terminals/manager.py`,
  whose mismatch still retires the session as today.
- **Continuations.** The larger protected list changes the scope fingerprint
  even with no grants. New scopes use schema generation 2. A continuation
  bound to a generation-1 scope is accepted and rebound when its repositories
  and writable roots are unchanged and only protected paths were added. That
  only narrows the scope. Anything else follows the existing changed-scope
  refusal. This is tested with a real generation-1 continuation.
- **Running jobs** keep the scope they were launched with.

## Folder picker

One small component, `PathPicker`, used on the space machine card and the
project machine card. It edits the same space record in both places. The
project card says the list applies to every project on that machine.

- It shows breadcrumbs, one folder level at a time, and a **Use this folder**
  button. Folders only.
- Protected folders appear locked, with the reason. Choosing a path inside
  one is refused, with the same rule as save.
- One endpoint, `POST /api/space/machines/{id}/directories`, lists one
  directory on that machine, locally or over SSH. It is generalized from
  setup's existing SSH browser (`browse_ssh_repository_paths`,
  `POST /api/project-setup/ssh-paths`). Setup's browser switches to the same
  component and endpoint, so there is one browser, not two.
- It is space-level, so any team member can use it. Listing is read-only and
  bounded to one directory, like today's browser.

## Backup, restore, transfer

- The space machine table is space data. Backup includes it and restore
  brings it back. It needs a schema migration, fresh and upgraded
  fingerprints, a restore fingerprint, and an immutable upgrade fixture.
- An older backup has no table. Restore leaves it empty, and it refills at the
  live boundaries with no grants.
- Upgrade rehearsals get an empty grant list, so copied production grants
  cannot reach disposable roots.
- Transfer classifies the table as excluded space data. The target's cards
  come from the rebuilt target manifest, not source provenance. Grants are
  never carried; the team grants its own.
- Project manifests, provisioning records, and history are unchanged, apart
  from the backup check accepting added machines.

## UI

- **Space Settings**: Server (team), Machines as cards (name, host, account,
  writable paths with the picker, projects using it, **+ New machine**),
  Provider logins, and Clear all caches (personal only).
- **Project Settings**: this project's machine cards, each with provider
  paths, compute, and the machine's writable paths with the picker. **Add
  machine** picks a card or creates one.
- **Setup** picks a machine card or creates one inline.
- A terminal or job error for a read-only path names the path and links to the
  machine card.

## Checks

- Unit: live boundaries fill the list, and raw loads, preflight, history
  reload, and backup snapshots do not; in-use edits refuse, including when a
  manifest is unreadable; adding a machine keeps backup passing; grants inside
  protected paths refuse; each eligible launch renders grants, protected
  paths, and its own exceptions for terminal, systemd, Codex, and Claude;
  scratch-only launches render none; exact-file Claude rules and escaped
  metacharacters; a symlinked grant; sign-in on an unused card; a generation-1
  continuation rebinds; a changed grant marks an open terminal restart-needed
  without retiring it.
- Behavior on Linux, in mirrored mode: a write under a granted parent works;
  a write to a protected child, another stage in `~/.rcp/stages`, and a token
  file fails; the launch's own stage stays writable.
- Upgrade fixture, and backup/restore of a new backup and of an older one.
- Served app: pick a writable path with the picker on both Settings pages,
  write into it from a new terminal on the team server, a Codex Work turn, and
  a systemd compute job, and confirm an open terminal shows the restart
  notice. Pick a machine card in setup and add one to an existing project.
  Inspect network, console, and server logs.

## Docs to update when this lands

- `docs/design.md` and `docs/specs/providers-and-containment.md`: writable
  paths, who gets them, protected storage, and the cooperative caveat.
- `docs/specs/projects-spaces-and-operations.md`: the space machine list,
  adding machines, and remote task folders under `~/.rcp`.
- `docs/specs/api-web-and-desktop-projections.md` and
  `docs/specs/interface-and-visual-design.md`: the two Settings levels,
  machine cards, and the picker.
- A new decision record: invariant 4 fixes which launches may write; humans
  choose the roots; RCP's own storage stays read-only inside any grant.

## Estimate

About 6 to 7 days: 1 for the machine list, adding machines, and sign-in; 3 for
writable paths, protected storage, the stage move, and the four backends; 1.5
for the Settings pages, cards, and picker; 1 for the Linux behavior, upgrade,
restore, and served-app drives.
