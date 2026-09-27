# Space settings: machine cards and writable paths

Date: 2026-09-27
Status: design confirmed by the human on 2026-09-27. Three Codex xhigh
reviews found earlier drafts not ready; this revision folds in the third
review's findings, and no fourth review is planned. The first draft moved
machine definitions out of project manifests; the human replaced it with this
smaller model. The human then settled the protection rule below. One question
remains open (Claude's file-tool rules). Nothing is implemented.

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
- **Existing projects fill the list at startup and on the first space Settings
  load**, from each registered project's accepted manifest (local or the
  validated remote mirror). The catalog does not open project services at
  startup, so without this an upgraded space would show no machines until each
  project was opened.
- **Adding a machine to an existing project** means running agents there; it
  adds no repository. It is a new additive operation through the canonical
  state workspace, which appends a machine alias and never changes existing
  aliases or repository placement.
- **Backup takes machines from the current manifest.** Today backup requires
  the manifest's machines and repositories to equal the setup-time
  provisioning record (`_provisioning_bound_configuration` in `projects.py`,
  and the machine equality in `BackupCheckoutRecoveryDescriptor` in
  `server_ops/backup_models.py`). That rule only made sense while nothing could
  change a project's topology after setup. The provisioning record stays the
  source only for what it uniquely holds: the evidence to rebuild each
  checkout (resolved path, deploy key). Machine entries come from the current
  manifest, so an added machine is backed up like any other. Old descriptors
  still validate. The check is add machine, then backup, restore, and backup
  again.

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
| Any machine | `~/.rcp` (jobs, credential locks, and after the move: task folders, pending inputs, command and SSH control sockets) | The account home; `credential_gate.py` already keeps its locks there |
| Remote machine | `~/.local/share/rcp` (deploy keys and generated Git identities) | `remote_credentials_root` and `git_identity.py` |
| Every repository | `.research` | Project manifests, as today |

The implementation completes this list by grepping every RCP storage owner
(`mkdtemp`, `mktemp`, `tempfile`, `/tmp`, `~/.rcp`, `.local/share/rcp`), not
from memory. Each entry's path comes from the function that creates it.

**RCP's transient files move under `~/.rcp`**, so that `/tmp` holds no RCP
state and one fixed rule covers folders created after a launch:

| Today | Moves to |
|---|---|
| Remote task folders `/tmp/rcp-run.*` | `~/.rcp/stages/` |
| Pending inputs `rcp-task-input-*`, `rcp-remote-inputs-*` in the temp dir | `~/.rcp/inputs/` |
| Command sockets `/tmp/rcp-command-*.sock` | `~/.rcp/sockets/` |
| SSH control sockets `/tmp/rcp-ssh-<uid>` | `~/.rcp/ssh/` |

Socket paths must stay under the 108-byte `AF_UNIX` limit. If a long home
would exceed it, launch refuses with that reason instead of falling back to
`/tmp`.

**Only new sessions use the new layout.** Existing chat and Auto-research
bindings are checked against their exact `/tmp/rcp-run.*` stage
(`runs/chat.py`, `runs/tasks/auto_research_stream.py`, and the durable binding
in `storage/agent_tasks.py`), so moving them would break invariant 10g. A
legacy stage keeps its location for attachment, collection, and cleanup until
it is released. While a legacy stage is live or retained, its path is added
to the protected list. The implementation tests a real pre-upgrade remote chat
and a waiting episode.

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
roots, protected paths, and exceptions. Backends only render those lists. The
same object drives admission checks and prompts too:

- The helper's preflight that refuses a `cwd` under a protected path
  (`runs/tasks/compute_commands.py`) evaluates the exceptions as well. A
  launch's own workspace is accepted, and its immutable inputs are refused.
- The prompt renderer (`agents/prompts.py`) renders grants and exceptions from
  the same object, so the rendered permissions match enforcement.

**A protected RCP path is added to a scope only when it sits inside one of that
scope's writable roots.** Repository roots never contain RCP storage, because
broad repository roots are already refused. So a scope with no grants is the
same scope as today, with the same fingerprint, and existing continuations are
unaffected. This follows the existing pattern of leaving `git_metadata_roots`
out of the fingerprint when empty.

**Grants override the terminal's non-RCP read-only mounts.** Today the
terminal mounts `~/.ssh`, `~/.gitconfig`, and `~/.config/git` read-only
(`terminals/git_access.py`) only because nothing grants them. A grant covering
them makes them writable. RCP-owned credentials and generated identities stay
read-only, because they are on the protected list.

### Rendering per backend

| Backend | Writable | Protected | Exceptions inside protected |
|---|---|---|---|
| Member terminal (mirrored) | `BindPaths`; the preflight checks each grant is writable | `BindReadOnlyPaths` + `ReadOnlyPaths` | None; a terminal has no stage |
| systemd job (mirrored) | `ReadWritePaths` | `ReadOnlyPaths` | `ReadWritePaths` on the exception, which systemd applies over its read-only parent |
| Codex | profile `write` roots | profile `read` entries | a more specific `write` entry. The implementation proves on the pinned Codex version that the more specific entry wins, and fails closed if not. |
| Claude | `Edit(path)` allow | `Edit(path)` deny | **Open; see "Open question".** |

**Absent protected paths are masked by type.** A missing folder gets a
read-only empty folder mount, as the terminal does today. A missing file,
such as a token file before sign-in, gets a read-only empty file mount, so it
cannot be created. The terminal preflight stops requiring protected files to
exist. A missing deny is never silently skipped.

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
- **Continuations.** A scope with no grants keeps today's fingerprint (see
  above). A changed grant set follows the existing changed-scope rule for
  resumed sessions. This is tested with a real pre-change continuation.
- **Running jobs** keep the scope they were launched with.

## Open question: Claude's file-tool rules

Claude's deny rules beat its allow rules. When a grant covers a protected
folder that also holds the launch's own scratch (for example, `/home/rcp`
covers the data folder), RCP cannot deny the folder and still allow the
scratch inside it.

- **Simplified (chosen by the human on 2026-09-27, before the third review):**
  leave a protected folder that holds the launch's own scratch undenied. The
  third review found this lets Claude's file tools, not only its shell, edit
  other stages there. The 2026-09-13 decision keeps file tools bounded, so
  this option also needs that decision amended.
- **Guard:** an RCP-owned `PreToolUse` hook checks every file edit against the
  resolved policy, including folders created after launch. RCP sets
  `disableAllHooks` today, so this changes how Claude launches. About half a
  day.

## Folder picker

One small component, `PathPicker`, used on the space machine card and the
project machine card. It edits the same space record in both places. The
project card says the list applies to every project on that machine.

- It shows breadcrumbs, one folder level at a time, and a **Use this folder**
  button. Folders only.
- A filter box narrows the current folder by name, and long folders page.
  The existing browser stops at 200 entries, counting files, and tells users
  to type the path (`transport/remote_repository_browser.py`). With no typing,
  every folder must stay reachable. The check is a folder with more than 200
  entries, most of them files.
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
  metacharacters; a symlinked grant; sign-in on an unused card; a scope with no
  grants keeps its fingerprint; helper admission accepts the own workspace and
  refuses its inputs; the picker reaches every entry of a 200-plus folder;
  startup seeding shows machines without opening a project; a changed grant marks an open terminal restart-needed
  without retiring it.
- Behavior on Linux, in mirrored mode: a write under a granted parent works;
  a write to a protected child, another stage in `~/.rcp/stages`, and a token
  file fails, including a sibling stage and a token file created after launch;
  the launch's own stage stays writable; a pre-upgrade remote chat and a
  waiting episode continue on their legacy `/tmp` stage.
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

About 5 to 6 days: 1 for the machine list, seeding, adding machines, backup,
and sign-in; 2.5 for writable paths, protected storage, moving transient files
with legacy stages kept, and the four backends; 1.5 for the Settings pages,
cards, and picker; 1 for the Linux behavior, upgrade, restore, and served-app
drives.
