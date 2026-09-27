# Space settings: machine cards and writable paths

Date: 2026-09-27
Status: design confirmed by the human on 2026-09-27, after three Codex
design reviews. Nothing is implemented.

Close this handoff when a team member picks `/data/shared/huggingface` as a
writable path on the GPU machine, and a new terminal, a Codex Work turn, and a
systemd compute job on that machine can each write into it.

## The problem

A user could not write to a shared Hugging Face cache from any RCP surface.
Every write scope is built from registered repositories only
(`ProjectWriteScope.writable_roots` in `src/rcp/agents/write_scope.py`), so
the terminal, compute jobs, and Codex Work could not write there. The only way
out was a shell outside RCP.

The Settings page also mixes space-wide things (provider sign-in, the server)
with project things, and a new project cannot reuse a machine another project
already set up.

## Design

1. **Two settings levels.** Each space gets a space Settings page, opened from
   the identity menu next to the user profile. Project Settings keeps project
   things.
2. **Space machine list.** A new SQLite table: id, display name, host, OS
   account, writable paths. `(host, os_account)` is unique. It is filled from
   registered projects' manifests at startup and whenever a project is
   registered or its manifest changes.
3. **Machine cards.** Setup and project Settings pick a card; RCP copies its
   host and account into the manifest, as today. Manifests stay the source of
   truth for a project's machines. **Add machine** on an existing project
   appends a machine alias through the canonical state workspace.
4. **Provider paths and compute stay per project**, edited on the project's
   card. Provider sign-in lists machines from the space table instead of
   project manifests.
5. **Writable paths** belong to a space machine and are edited on both the
   space card and the project card, which edit the same record. Any team
   member may edit them.
6. **Folder picker.** One small component and one endpoint that lists one
   directory on a machine, locally or over SSH, with a name filter and paging.
   It generalizes setup's SSH browser (`browse_ssh_repository_paths`), and
   setup switches to it.

| Space Settings | Project Settings |
|---|---|
| Server status (team only) | Project home, Members |
| Machines: cards with writable paths | Machines: this project's cards, with provider paths, compute, and writable paths |
| Provider logins | Project boundary |
| Clear caches for every project (personal space only; the spec forbids it for teams) | Compute connections, Agent defaults, Skills & workflows, Project cache |

Display (theme, text size) moves to the identity menu.

## Writable paths

**Who gets them:** every launch that already gets repository write roots
(member terminals, Work turns, Auto-research episodes, and compute jobs from
them). Launches without repository write roots get none: branch merge,
Discuss, ingestion, paper coach, and scratch-only Work such as episode
reports. The branch merge's scratch-only check stays.

**One list:** the machine's writable paths are appended to `writable_roots`.
systemd (`ReadWritePaths`), Codex (profile write roots), and Claude
(`Edit(path)` allow) already render that list. The terminal adds them as
`BindPaths` through both its local and remote launcher. The prompt renderer
lists them.

**The only constraint is protecting RCP's own data.** Anything else can be
granted, including home folders, `/tmp`, and `~/.ssh`. RCP's data stays
read-only inside any grant:

| Path | Computed from |
|---|---|
| The RCP data folder | The running process's data directory |
| On a team server: `releases/`, `source/`, `credentials/`, `update-checkpoints/`, `restore-operations/`, `/etc/rcp`, `/run/rcp` | The loaded `ServerLayout` |
| `~/.rcp` on every machine | The account home |
| `~/.local/share/rcp` on remote machines | `remote_credentials_root` |
| `.research` in every repository | Project manifests, as today |

- A grant inside one of these is refused. A grant containing one is allowed,
  with the protected path kept read-only inside it.
- The launch's own scratch and job folder stay writable.
- A protected path is added to a scope only when a grant covers it. Scopes
  with no grants are unchanged, so existing continuations keep their
  fingerprints.
- Claude's deny rules beat its allows. A protected folder that holds the
  launch's own scratch is therefore left undenied for Claude. Claude's shell
  is unbounded anyway (the 2026-09-13 decision).
- The helper job's check that refuses a `cwd` under a protected path accepts
  the launch's own workspace.

**RCP stops writing to `/tmp`.** Today RCP puts its own files in `/tmp` and
the system temp directory: remote task folders (`/tmp/rcp-run.*`), command
sockets (`/tmp/rcp-command-*.sock`), SSH control sockets (`/tmp/rcp-ssh-<uid>`),
a Git probe folder, and about 25 short-lived `tempfile` calls. That is wrong
for an app that owns a data folder. Every one of them moves under `~/.rcp` on
the machine where it is created: task folders to `~/.rcp/stages/`, sockets to
`~/.rcp/sockets/` and `~/.rcp/ssh/`, and everything else to `~/.rcp/tmp/`
(each `tempfile` call gets that `dir=`). `~/.rcp` is short enough for socket
path limits (104 bytes on macOS, 108 on Linux) and is already protected.
Existing `/tmp/rcp-run.*` task folders keep working where they are until they
are cleaned up. After this, `/tmp` holds nothing of RCP's, and a human can
grant it like any other path.

**`/tmp` is writable by default.** Once RCP keeps nothing there, every launch
that gets writable paths also gets `/tmp` and `$TMPDIR` writable, with no grant
needed. Agents routinely make worktrees and scratch files there. Measured and
read from code on 2026-09-27:

| Launch | `/tmp` today | After |
|---|---|---|
| Claude Work | Writable through the shell, which is unsandboxed | Unchanged, and `Edit` rules allow it too |
| Codex Discuss | Writable: Codex's standard `workspace-write` mode always allows `/tmp` (probed) | Unchanged |
| Codex Work | **Not writable.** RCP's own permission profile left `/tmp` out; `git worktree add /tmp/...` fails with "Operation not permitted" (probed with codex-cli 0.157.0, macOS; confirm on Linux) | Writable, via profile write roots |
| Member terminal | A private `/tmp` (`PrivateTmp=yes`) that agents cannot see and that disappears with the terminal | The real `/tmp`, shared with agents: `PrivateTmp` is dropped |
| systemd compute job | Read-only under `ProtectSystem=strict` | Writable, via `ReadWritePaths` |

Codex Work losing `/tmp` was a regression from switching to RCP's own profile,
not a decision.

## Backup

Backup snapshots the project as it is. The check that refuses a team project
whose machines or repositories differ from its setup-time provisioning record
is removed (`_provisioning_bound_configuration` in `projects.py`, and the
machine equality in `BackupCheckoutRecoveryDescriptor` in
`server_ops/backup_models.py`). Each checkout's re-clone recipe comes from its
current state. The space machine table is included in backup.

## Checks

- Unit: the machine list fills from manifests; each eligible launch carries
  the grants into the terminal, systemd, Codex, and Claude arguments;
  scratch-only launches carry none; a grant inside RCP data is refused; RCP
  data inside a grant stays read-only; scopes with no grants keep their
  fingerprint.
- On the team server: write into a granted path from a new terminal, a Codex
  Work turn, and a systemd compute job; a write into the RCP data folder under
  a `/home/rcp` grant fails.
- Add a machine to a project, then backup and restore it.
- Served app: pick a path with the picker on both Settings pages, pick a card
  in setup, add a machine to a project, sign in on a machine.

## Docs to update when this lands

`docs/specs/providers-and-containment.md` and `docs/design.md` (writable paths
and protected RCP data), `docs/specs/projects-spaces-and-operations.md` (space
machines, backup), `docs/specs/server-and-machine-operations.md` (the removed
backup check), the two web specs (two Settings levels, cards, picker), and a
decision record amending invariant 4's wording and the 2026-09-13 Claude
decision.
