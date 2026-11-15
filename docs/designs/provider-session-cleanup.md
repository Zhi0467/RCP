# Provider session cleanup

Extend the stage sweep to provider sessions, hidden-read launch folders, and
the homes of removed logins and machines. Checked on `origin/main` 9e9cb3297
and against Codex 0.167.0, OpenCode 1.18.30, Claude Code 2.1.325, agy 1.5.9.

## Rule

RCP keeps an unused provider session for 30 days. A session is held, and never
swept, while any of these names it:

- a `graph_runs` row with status `queued`, `running`, `pausing` or `paused`, by
  `native_session_id` or by `request_json.fork_session_id`;
- a non-detached `chat_forks` row whose fork chat has no `graph_runs` row with a
  `native_session_id` yet; it holds its source turn's session
  (`origin.source_operation_id`, storage/chat_forks.py:20-47);
- a `questions` row with `state='pending'`, by `origin_json.native_session_id`
  (storage/question_models.py:31-45);
- a `writing_sessions` row (Paper prunes those at 180 days);
- a `paper_change_groups` row in state `pending` or `outdated`, through its
  `task_id`. Coach Refresh resumes that task's session (api/paper_changes.py:89-113).
  The Coach-as-chat PR deletes this path; whichever PR lands second drops the rule;
- an open episode, through `graph_runs.episode_id`, `experiment_episode_state`, or
  `episode_wrapups`. Exactly: `episodes.status IN ('queued','running','stopping',
  'wrapping_up','needs_action') OR episodes.wrapup_state IN ('pending','running')`
  (storage/models.py:2325-2344). An `episode_wrapups` row with `state IN
  ('pending','running')` holds its `native_session_id` regardless.

Otherwise it is swept once the newest `graph_runs.updated_at` among rows naming
it is over 30 days old, which covers "the chat's latest turn names it".

## Only sessions RCP started

RCP deletes only ids recorded in `graph_runs.native_session_id` or
`writing_sessions`. A client cannot supply a chat session: admission clears
`session_id` (api/tasks.py:1606-1612), and launch refuses a session the chat did
not start (runs/chat.py:519-531). So every recorded id came from a process RCP
launched. Sessions people start in their own or RCP member terminals are never
recorded, so never touched. The sweep never lists or globs a provider store. It
checks each id's shape (Codex UUID, OpenCode `ses_` plus alphanumerics) and
passes it to the provider's own delete command.

## Providers

- **Codex** has no retention. `codex delete --force <uuid>` needs no sign-in. On a
  copy of real state it removed the rollout, its `state_5.sqlite` `threads` row,
  its `thread_history_1.sqlite` turns, and its `session_index.jsonl` line, also
  from `archived_sessions`. Deleting files by hand would leave those rows, so RCP
  uses the CLI with the account's own Codex home; added-login homes link there
  (providers/codex/homes.py:25-56). A missing id exits 1 ("failed to delete
  session"). Any nonzero exit records `last_error` and retries on the next pass.
- **OpenCode** has no pruning. `opencode session delete <id>` works; a missing id
  prints "Session not found", which counts as gone.
- **Claude** prunes itself (`cleanupPeriodDays`), but only from interactive
  sessions: a print-mode probe kept a 44-day-old transcript with and without the
  setting in `--settings` (claude/profile.py:568-611). The setting is also
  account-wide. Claude has no delete command, so RCP deletes its own transcripts
  by exact path: `<config>/projects/<key>/<id>.jsonl` and the folder
  `<config>/projects/<key>/<id>/` (it holds `subagents`). `<config>` is
  `CLAUDE_CONFIG_DIR` or `~/.claude` on the execution account. `<key>` is
  `claude_project_directory(cwd)` (providers/claude/remote.py:48-63). The fork
  path builds the same path (remote.py:80-82). The cwd is the chat's stage
  workspace (`_chat_stage_workspace`, agent_tasks.py:2300-2308). Only node and
  project chats have it; other Claude sessions are kept and counted in the log.
- **agy** has no delete or retention command; RCP leaves it, and logs so.

## Missing sessions

Partly handled today. A chat drops its session when the latest row is classified
`stale_session` (storage/agent_tasks.py:1732-1743, 6604-6622); the next turn
starts fresh, and Retry too (background.py:1031, 1127-1139). But
`classify_terminal_error` (providers/__init__.py:136-159) knows only "no thread
with id". Probes add Codex "no rollout found for thread id", Claude "No
conversation found with session ID", and OpenCode "Session not found". With
them, a session the provider pruned costs one failed turn, not every turn.

A swept session costs none. A backend-local table
`provider_session_sweeps(provider, execution_machine, native_session_id,
marked_at, deleted_at, last_error)` records it. `_SESSION_DROP_COLUMNS` returns
`session_swept` for ids in it, which covers the chat's current session, branch
sources and orchestrator seeds (agent_tasks.py:1705, 1754, 1817, 2133).
`_native_resume_unavailable` (background.py:3581) and the question-origin check
(agent_tasks.py:382-390) read it too.

One `BEGIN IMMEDIATE` transaction re-checks the rule and inserts the mark; the
delete runs after. An earlier turn has a live row, so the session is held; a
later one starts fresh. A turn that read the session just before the mark fails
as a classified stale session, and its Retry starts fresh.

## Where the sweep runs

`runs/provider_session_sweep.py` owns one pass: at startup as maintenance after
recovery, then every `PROVIDER_SESSION_SWEEP_INTERVAL_SECONDS`. Per machine it
reads up to `PROVIDER_SESSION_SWEEP_BATCH` eligible ids, marks them, and deletes
them with the launch's binary (`AgentLauncher.provider_binary`, launcher.py:2149,
else discovery). Local runs in-process. Remote uses one SSH call to a shipped
script that loops over ids from stdin. Each result sets `deleted_at` or
`last_error`. An unreachable host keeps its marks for the next pass. Each pass
logs one line per machine: counts per provider, failures, skipped providers.

## Hidden-read launch folders

Each launch writes `<home>/.rcp/hidden-read/<fingerprint>/` (core/models.py:317-323,
agents/provider_environment.py:391-406). Nothing removes them. This Mac has
46,914 folders (982 MB), and every sampled policy names `pytest-of-*` paths.

- Sweep folders older than `RUN_STAGE_RETENTION_DAYS`, skipping a protected set,
  as `sweep_stages` skips live stages. The set is the fingerprints of `queued`,
  `running`, `pausing` and `paused` rows on that machine. Rows do not record the
  fingerprint, so launch stores it in a new nullable `graph_runs` column beside
  `write_scope_fingerprint` (background.py:345-347). Locally the pass above
  sweeps, off the launch path. Remotely `sweep_stages`
  (transport/remote_stage_root.py:145) takes the folder and the set.
- Test gap: `isolate_host` (tests/helpers.py:790) patches `rcp_home`, but
  `host_facts` (staged_hidden_read.py:78) reads `expanduser("~")`. `isolate_host`
  will set `HOME` to a per-test folder; fifteen test files already set it.

## Removed logins and machines

Login removal deletes only the credential (server_ops/provider_cleanup.py:78-118),
and startup re-prepares every login's Codex home (api/app.py:2063-2089).

- Once the credential is gone and no turn runs on the login, RCP walks
  `provider-homes/<provider>/<login_id>` without following links. It unlinks
  each symlink and the profile's `login_home_credentials`, then removes folders
  left empty. Any other real file stays, and so does its folder.
- RCP never deletes `<login_id>-retained` or its UUID-named siblings
  (providers/login_homes.py:28-35). They can hold the only copy of a rollout.
- An unreachable host keeps `_CLEANUP_PENDING`, which `reconcile_login_removals`
  (runs/provider_login.py:472) retries.
- Startup prepares only `removed_at IS NULL OR detail = _CLEANUP_PENDING`.
- Machine removal (api/space_machines.py:944-993) runs this cleanup for the
  machine's logins when reachable. Another backend may share the account's
  `~/.rcp`, so nothing else is removed; the sweeps age it out. The route returns
  only `{"deleted": id}` (line 993), so an unreachable host is logged only, with
  the machine id and the count of leftover login folders.

## Backup and restore

Both new tables are in backups, like the rest of the app database. They join
`TRANSFER_GLOBAL_TABLES` (transfer/archive.py:87), so they never travel with a
project, and `_EXEMPT_TABLES` (tests/test_resource_versions.py:215), which CI
checks against every table. A restored database can be older than the hosts.
`detach_restored_lifecycle` (storage/restore_detachment.py:18-60) records its
`detached_at` in a singleton `provider_session_sweep_state.restored_at`. The
sweep then considers only ids whose newest `graph_runs` row is newer.

## Limits

`limits.py`: `PROVIDER_SESSION_RETENTION_DAYS = 30`, `..._SWEEP_INTERVAL_SECONDS = 86400`,
`..._SWEEP_BATCH = 500` per machine per pass, `..._DELETE_TIMEOUT_SECONDS = 30`.

## Branching from a swept chat

Branching keeps refusing a chat whose session was dropped
(`_usable_chat_fork_source`, agent_tasks.py:1745-1768). Today that is a dead end.
The routes return the raw text "The source chat turn has no settled, usable
native session." (api/chats.py:79-88, 120), shown only as the hover title of a
failed pick (web/src/chat/ChatBranchPicker.tsx:69, 79). It says neither why nor
what to do, though a way exists: a new conversation on the branch.

Smallest fix: a dropped session raises the coded refusal `chat_session_expired`;
both branch routes return `{code, message}`. `TURN_REFUSAL_REASONS`
(web/src/core/api.ts:387-404) gains one entry: the agent session expired after
30 idle days; start a new conversation on the branch. No new UI.

## Checks

- Store: each hold above, the exact open-episode condition, pending wrapups,
  age, the restore cutoff, swept ids and unrecorded ids.
- Fork: with a real Codex and a real OpenCode, fork a chat, launch the fork's
  first turn, delete the source session, then send another fork turn; it works.
- Guard: fake `codex` and `opencode` on PATH record argv; with an old RCP session
  and a human session in one home, only the RCP id is deleted.
- Chat: age a chat, sweep, send a turn; it starts fresh with no failed turn.
  Branching it returns `chat_session_expired`.
- Claude: the exact transcript and its folder go; a sibling session stays.
- `classify_terminal_error` returns `stale_session` for the three probed texts.
- Hidden read: old folders go, a live row's fingerprint stays, remote sweeps
  too, and tests leave the real `~/.rcp/hidden-read` count unchanged.
- Logins: links and credentials go and links are not followed; a real file, the
  `-retained` folder and its UUID siblings stay; startup skips finished
  removals; unreachable stays pending.
- Docs: providers-and-containment.md gains "Provider session retention"; its
  Codex-home (line 1404) and login-removal paragraphs change.
