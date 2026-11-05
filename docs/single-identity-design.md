# Single machine identity

A machine is its space card: one `space_machines` row with `machine_id`,
`name`, `host`, `os_account` and settings. That row is the only owner. Every
other record stores `machine_id` and reads host and account from the card when
it needs them. Editing a card's host or account writes only that row. The
per-project machine alias disappears from code, data, routes and wording. Data
names a machine by id; the UI shows its name.

This PR covers machines only. All paths are under `src/rcp/` unless they start
with `web/`, `docs/` or `tests/`.

## 1. Where the machine has more than one identity

| Identity | Today's owner | Copies used as keys |
|---|---|---|
| `machine_id`, `name` | `space_machines` (`storage/base.py:2980-2992`) | Space tasks already set `run_on = machine_id` (`runs/space_chat.py:137`) |
| `host`, `os_account` | Card and each manifest `[[machines]]` (`config.py:51-95`, `688-705`) | Card lookup by host: `space_machine_for` (`storage/space_machines.py:37`), `agents/launcher.py:2100`, `runs/shared.py:824`, `setup.py:1209` |
| Per-project alias | Manifest `[[machines]] alias` | `RepositoryConfig.machine`, `run_on`, `base_machine` (`config.py:196-232, 409-415`); generated `laptop` / `remote-N` (`setup.py:1366-1426`) |

The alias is also stored in:

- **Runtime records.** `graph_runs.request_json` `run_on`;
  `chat_session_contexts`, `writing_sessions`, `agent_threads`,
  `compute_backend_probes`, `compute_jobs`, `experiment_episode_state` and
  `episode_wrapups`; worktree and hidden-read bindings (`core/models.py:232, 356`);
  terminal metadata (`terminals/manager.py:79-112`).
- **Provisioning and transfer JSON.** `storage/models.py:424-608, 1033-1184`.
- **The backup archive.** `server_ops/backup_models.py:592-629, 838-868`.
- **Server CLI.** `server_ops/models.py:165`.
- **Routes.** `api/project_state.py:470-570` and `api/app.py:547-584`.
- **Agent context.** `agents/prompts.py:514-531`, `compute.py:200-243` and
  `agents/context.py:24-28`.
- **The web.** `web/src/core/types.ts:180-452, 1971-1980, 3486` and
  `web/src/core/api.ts:513-528, 848, 1150`.

The host is used as the machine key in the provider login tables
(`storage/provider_logins.py:39-64`), skill inventories, `projects.state_location`,
every `stage_host` and `execution_host` column, the readiness deadline key
(`storage/resource_deadlines.py:150`) and `memory_deletions`
(`storage/memory.py:14-16`). On disk it keys credential folders
(`agents/provider_environment.py:131-136`), the state mirror
(`transport/state.py:3336`) and bootstrap manifests.

Canonical history holds aliases in three places, and invariant 1 forbids
rewriting them:

- `RepositoryDescriptor.machine` (`core/operations.py:201-204`)
- `SourceRef.machine` (`core/models.py:445`)
- the cursor key `repo/machine/provider/session` (`sources/indexer.py:214, 289, 326`)

## 2. Target data shape

### Manifest

```toml
machines = ["3f2a...", "9c41..."]

[[repositories]]
alias = "code"
machine = "3f2a..."   # "" means unassigned
path = "/home/x/code"
```

- `base_machine` is the state repository's `machine`.
- Agent profiles drop `run_on`. Each profile must already equal the base
  (`config.py:367-376`).
- Task `run_on` and every `execution_machine` hold a `machine_id`.
- A repository with `machine = ""` is listed. Launches that need its machine
  refuse with "Assign a machine".

### SQLite (migration `(77, "machine_identity_v1")`)

| Table | Final key columns | Dropped | Primary key or unique index |
|---|---|---|---|
| `provider_login_states` | `machine_id` | `host` | PK `(provider, machine_id, login_id)`; member index `(provider, machine_id, member_id)` |
| `provider_login_assignments` | `machine_id` | `host` | `(provider, machine_id, login_id)` |
| `provider_login_settings` | `machine_id` (`'*'` stays) | `host` | `(user_id, provider, machine_id)` |
| `provider_readiness_snapshots` | `machine_id` | `host` | `(provider, machine_id, login_id, binary)`; cleared |
| `provider_skill_inventories` | `machine_id` | `host` | `(provider, machine_id, configured_binary)`; cleared |
| `chat_session_contexts` | `machine_id` | `execution_machine` | `(provider, machine_id, native_session_id)` |
| `writing_sessions` | `machine_id` | `execution_machine` | unchanged |
| `agent_threads` | `agent_run_on` holds an id | none | unchanged |
| `compute_backend_probes` | `machine_id` | `execution_machine` | `(project_id, machine_id, route)` |
| `compute_jobs` | `machine_id` | `execution_machine`, `execution_host` | `job_id` |
| `watchers` | `machine_id` | `execution_host` | unchanged |
| `browser_owners` | `machine_id` | `execution_host` | `owner_token` |
| `graph_runs` | `stage_machine_id`; `request_json.run_on` is an id | `stage_host` | unchanged |
| `result_views` | `run_on` (id), `stage_machine_id` | `stage_host` | `view_id` |
| `artifact_revision_candidates`, `episode_wrapups`, `experiment_episode_state` | `stage_machine_id`, `machine_id` | `stage_host`, `execution_host`, `execution_machine` | unchanged |
| `projects` | `state_machine_id`, `state_path` | `state_location` (`state_remote` is derived) | unchanged |
| `memory_deletions` | `state_machine_id`, `state_path`, `machine_id` | `state_location`, `host_key` | `(project_id, state_machine_id, state_path)` |
| `project_provisioning_requests` | see §3.4 | none | unchanged |

- `space_machines` keeps `UNIQUE(host)`. SSH chooses the account from the host
  route, so two cards for one host could not be told apart.
- A relative `state_location` such as `project/.research` belongs to the
  backend card.
- The JSON lookups on `$.stage_host` (`storage/agent_tasks.py:2318, 2412, 4521`)
  become `$.stage_machine_id`.

### Files on disk

- **Credential folders** become `machine-<machine_id>`. They are renamed, not
  cleared.
- **Caches** are cleared, not migrated. This covers readiness snapshots, skill
  inventories, and bootstrap manifests. It also covers the state mirror, which
  is now keyed by `project_id` (`transport/state.py:3336`), and the indexer
  cache `cache_root/<alias>/<provider>` (`sources/indexer.py:738`), which
  becomes `cache_root/<machine_id>/<provider>`.

### Canonical history

- New `RepositoryDescriptor` values leave `machine` out. The field becomes
  optional and is omitted when empty, so stored bytes replay unchanged.
- Agents run on the base machine, so accepting an agent-proposed repository
  writes the base `machine_id` into the manifest. A human add writes the chosen
  card's id.
- `_synchronize_manifest_from_history` (`history/manager.py:1820-1840`) keeps a
  descriptor's `machine` only when it is a card id this space knows. Otherwise
  it adds the repository with `machine = ""`.
- `SourceRef.machine` holds a `machine_id` in new refs. Old values stay as
  inert text, and the UI shows the card name when one resolves.
- **Cursor keys.** New keys are `repo/provider/session`. When `cursors.json` is
  built (`core/materialize.py:162`, `history/engine.py:1029`), each four-part
  key is converted to three parts. The latest revision wins. Seed and refresh
  receive only three-part keys (`agents/context.py:252`).
- **Indexer.** It stops reading `refresh.run_on` (`sources/indexer.py:162-164`).
  It filters remote repositories by `machine_id` (`:675`) and caches by
  `machine_id` (`:738`).

## 3. Upgrade

### 3.1 The boundary module

`machine_upgrade.py` is the only code that may name the legacy key. It holds:

- `read_legacy_manifest(path)`
- the single resolution rule
- the legacy provisioning digest check
- the v1 backup archive reader

`load_manifest` refuses a document that has `[[machines]]` tables.

**The resolution rule.** A legacy reference maps to the card whose `host`
equals the reference's host, or to `""`.

- An alias gets its host only from its own legacy manifest.
- A host-keyed row uses its host.
- There is no fallback from an alias to a row's host column.

The upgrade never creates a card from a row. Cards come only from manifests,
through `ensure_space_machines`. An empty host means the backend card. A
durable row that resolves to `""` is shown as "Removed machine".

### 3.2 Order

1. **SQLite migration 77.** It adds the new columns and rekeys host-keyed
   durable rows through the rule. It clears caches, renames credential folders
   and sets `projects.machine_ids_pending = 1`. On a key collision, such as
   the `chat_session_contexts` primary key, it keeps the newest row by
   `updated_at` and drops the rest.
2. **`fill_space_machines`** runs before `background_tasks.recover_at_startup()`
   (`api/app.py:2109-2111`). Its steps, in order:
   1. It reads every pending manifest through `read_legacy_manifest`.
   2. It ensures cards and merges legacy settings, as it does today.
   3. It settles cards only when every manifest was read.
   4. It runs the per-project rewrite for each pending project.
3. **The per-project rewrite.**
   1. Build `{alias: machine_id}` through the rule.
   2. Write the new manifest with a `HistoryManager` method. The method takes
      the workspace transaction and calls `publish([manifest.toml])`. It does
      not call `require_writable` or `_require_writable_home_locked`, so
      read-only and non-home projects upgrade too. `manifest.toml` is
      published state, not Patch history.
   3. Rekey the project's alias-keyed rows in the same pass, keeping the
      newest row on any collision.
   4. Clear the pending flag.

   A project is skipped while its state cannot be reached, or while it has an
   open transfer or provisioning request. It stays pending until that settles.
   Opening a pending project runs the same rewrite first, so a project whose
   state comes back is upgraded on open without a restart.
4. **Recovery** skips pending projects, and a pending project does not open.
5. **Candidate proof.** An update or restore candidate runs the per-project
   rewrite for local pending projects inside its copy, then reads them as
   usual. Remote pending projects stay pending; the proof checks only that
   they are recorded, since remote manifests are rewritten after rollback
   authority ends (§3.3).

### 3.3 Rollback

The manifest has no version field, and `MachineConfig.alias` is required
(`config.py:60`). An older build therefore cannot open an upgraded project.

- **Team server.** Update rollback restores the data directory and local
  canonical roots (`docs/specs/server-and-machine-operations.md:360-366`).
  Remote projects open only after durable selection (`:372-374`), so remote
  manifests are rewritten only after automatic rollback authority has ended.
- **Desktop.** A manual downgrade cannot read upgraded projects. The release
  notes must say so.

### 3.4 Provisioning requests

These are the new JSON shapes in `storage/models.py`:

```json
{"machines": [{"machine_id": "3f2a...", "central_root": "/srv/rcp/projects"}],
 "repositories": [{"alias": "code", "machine_id": "3f2a...", "...": "..."}],
 "provider_checks": [{"provider": "codex", "machine_id": "3f2a...", "...": "..."}]}
```

- `ProjectProvisioningMachineRecord` adds `resolved_central_root`.
- Location, host and account come from the card.

Handling by state:

- **Completed requests** are frozen receipts. Their stored bytes stay as they
  are. `machine_upgrade.py` verifies them the way
  `_legacy_project_provisioning_review_digest` does
  (`storage/provisioning.py:185`), then maps them through the rule in memory.
- **Pending reviewed requests** return to review in the new shape.
- **New requests** name cards. Team setup creates cards first.

### 3.5 Transfer

- `TRANSFER_RECORD_SCHEMA_VERSION` becomes 8 (`transfer/records.py:35`).
  `ProjectTransferRepositorySource` drops its `machine` field, and
  `ProjectTransferSourceConfiguration` drops `machine_aliases`.
- The target maps each repository to one of its own cards (§4).
- There is no v7 reader. A transfer in flight across the upgrade is refused
  with "Restart the transfer".
- Retained `.research/` from another space is adopted the same way: each
  repository is mapped to a card.

### 3.6 Backups

- `BACKUP_MANIFEST_SCHEMA_VERSION` becomes 2. Machines are stored as
  `machine_id` and `resolved_central_root` only.
- `backup_recovery.py:74-79` only compares machine records by key, so it needs
  no host.
- `restore._recovery_machine` (`server_ops/restore.py:1044-1067, 1245`) needs
  host and account. It reads them from the archived `space_machines` rows,
  since restore already opens the archived database (`:1571-1620`).
- A v1 archive restores through `machine_upgrade.py` with the rule.

## 4. API and type contract (M4)

| Surface | New shape |
|---|---|
| Project machine routes | `DELETE`, `POST .../compute/check` and `GET .../browser` on `/api/projects/{id}/machines/{machine_id}` |
| `AddProjectMachineRequest` | `{machine_id}` |
| Compute selector (`api/app.py:547-584`) | `machine_id` |
| Project machine view | `{machine_id, removable, ...}` with no alias; name and host come from the card list |
| Repository view | `machine: machine_id \| ""` |
| `PATCH /api/space/machines/{id}` | adds `host?` and `os_account?` (§5) |
| Transfer v8 target request | `repositories: [{alias, machine_id}]` (one per source repository) and `state_machine_id` |
| Agents | Prose shows the card name; data such as `RepositoryPointer.machine` and compute `id` holds the id |
| Server CLI | `ServerCommandRequest.machine_id`. `SERVER_CLI_PROTOCOL_VERSION` goes to 2 (`server_ops/models.py:25`). The CLI takes an id, or a name when it is unique |

## 5. Edit operation

`PATCH /api/space/machines/{machine_id}` (`api/space_machines.py:626`) has
one check: the new host is reachable as the account. It runs
`login_account(host)` (`machine_login.py:67`) and returns its existing failure
codes as 422: `host_key_unknown` with `fingerprint`, which leads to Trust and a
retry, plus `host_key_changed`, `authentication_failed` and `unreachable`. A
blank account is filled from the login in a team space, as create does
(`:556-583`). The `UNIQUE(host)` constraint still refuses a host another card
holds.

The write touches only `space_machines`. It then calls
`invalidate_machine(machine_id)`, which clears readiness, skill inventories,
compute probes for every project on the card, and those projects' display
snapshots.

Nothing is torn down. SSH control sockets are per destination
(`transport/ssh.py:72`). Running processes keep their connection, and stored
records hold only the id. After a restart, recovery reaches the card's current
host. A missing stage takes the existing recovery failure paths. There is no
new state.

On the card, the one pencil next to the name replaces today's rename. It turns
the header into the Add machine fields, Name, SSH host and Account, prefilled,
with Save and Cancel; the folds stay below. `host_key_unknown` shows the
fingerprint and Trust exactly as Add machine does, then Save retries. Any other
failure keeps the form open with its message.

## 6. Slices

**M0 shared contracts.** It owns:

- `config.py`, `machine_upgrade.py` and `machine_cards.py`
- in storage: `models.py`, `base.py` (migration 77), `space_machines.py`,
  `provider_logins.py`, `resource_deadlines.py`, `memory.py` and
  `resource_versions.py`
- `core/models.py` and `core/operations.py`
- `web/src/core/types.ts`

`machine_cards.py` holds the shared functions: `card(store, id)`,
`host_of(store, id)`, `LoginKey(provider, machine_id, login_id)` and
`invalidate_machine`.

Checks:

```
uv run pytest -n0 tests/test_config.py tests/test_canonical_machine.py tests/test_stored_request_compat.py
```

The other slices depend on M0 and run in parallel:

| Slice | Owns | Focused checks |
|---|---|---|
| M1 Runtime | `runs/**`, `agents/**`, `storage/agent_tasks.py`, `storage/agent_threads.py`, `compute.py`, `compute_jobs/**`, `conversation_worktrees.py`, `transport/**`, `terminals/**`, `sources/indexer.py`, `browser/**`, `core/materialize.py`, `history/engine.py` | indexer, write-scope, worktree and compute tests; `tests/test_compute_jobs_config.py`; `tests/test_api_terminals.py`; `tests/lifecycles` |
| M2 Catalog and API | `projects.py` (the per-project rewrite, the single owner), `storage/machine_rekey.py`, `history/manager.py`, `service.py`, `setup.py`, `api/project_state.py`, `api/space_machines.py` (`:672-673` probes by id, plus the edit route), `api/app.py`, `api/terminals.py`, `storage/projects.py` | `tests/test_setup.py`, `tests/test_space_machines_api.py`, `tests/test_api_project_state.py`, `tests/test_machine_login.py` |
| M3 Server ops | `server_ops/**`, `api/project_provisioning.py`, `provisioning_repositories.py`, `transfer/**`, `project_transfer.py` | `tests/test_project_provisioning_*.py`, `tests/test_backup_*.py`, `tests/test_restore_archive_compatibility.py`, `tests/test_transfer_*.py` |
| M4 Web | `web/src/**` except types | the unit tests `spaceSettings`, `projectSetup`, `projectProvisioning`, `computeRouteNotice`, `webmcpReads`, `providerReadiness` and `desktopRuntime`; the browser tests `externalJobs`, `projectTransfer`, `artifacts`, `projectReconnect`, `experimentRecovery`, `settingsRail` and `settingsRequestRaces`; `npm --prefix web run build` |
| M5 Integration | `docs/**`, `tests/server_upgrade_fixture_builder.py`, `tests/test_server_upgrade.py` | the regression below; a served journey that edits a card's host, launches a turn and runs a compute check; 390px; `scripts/quality_delta.py` |

**Keeping the branch green.** M1–M4 each work in a worktree cut from M0 and
run only their own focused checks. They merge into the feature branch together
before the first push, so CI runs only the integrated head. M0 alone is never
pushed.

**Regression.** `upgrade_fixture` (`tests/server_upgrade_fixture_builder.py:171-197`)
reads manifests through `machine_upgrade` instead of `load_manifest`. A new
case in `tests/test_server_upgrade.py` starts from an old-writer seed and
asserts:

- manifests hold card ids
- a running task's `run_on` is rewritten
- a completed provisioning digest still verifies
- credential folders are renamed
- old four-part cursors still suppress re-ingestion

## 7. Spec updates (M5)

- `projects-spaces-and-operations.md` §Space machines (`:39-80`): cards own
  host and account, manifests hold ids, and the edit is described.
  §Repository and truth scope (`:288-372`) covers unassigned machines.
- `machines-and-compute.md` §Base and project machines (`:9-24`), §Readiness
  and setup (`:91`), and §Durable state and transfer (`:214`).
- `api-web-and-desktop-projections.md` §Compute setup and job APIs
  (`:622-661`) and §Paper, Settings, and History (`:1790-1821`).
- `providers-and-containment.md` `:1055-1083` and §Login state and failure
  (`:1315`).
- `server-and-machine-operations.md` provisioning (`:536`), transfer
  (`:571-623`), and backup and restore (`:664`).
- `graph-history-and-transitions.md` §Replay and compatibility (`:295`):
  descriptor machine and cursor key conversion.
- `conversations-episodes-and-watchers.md:200, 387, 521-534, 719`,
  `interface-and-visual-design.md:388-390`, and `docs/server.md:260`.

## Follow-up PRs

- **Project** (`storage/projects.py`, `projects.py`): finish pending file moves,
  drop `project_aliases` and the `manifest.name` id fallback, and compare ids in
  name guards.
- **Space** (`web/src-tauri`, backup and restore): the desktop refreshes the
  space name on reconnect, and guards compare `space_id`.
- **Member** (`storage/bulletin.py`, `storage/lessons.py`, run tables): store
  `user_id` and read display names live.
- **Branch** (`storage/branch_names.py`, `storage/transfer.py`,
  `storage/chat_names.py`): drop `branch_names`, export from `graph_branches`,
  and read auto-research chat titles live.
