# The Paper is a folder; the Coach is an ordinary chat

The Paper becomes a folder of files on the base machine. Those files are the
Paper. The Paper editor is an editor over them, and the Coach is an ordinary
project chat that edits them like any coding agent edits a repository. No
staging, no mirror, no change cards.

## Problem

- **Two copies.** Paper content lives in SQLite (saved snapshots and a Yjs log)
  and is republished as a whole tree into `.research/paper` after idle editing.
  Agents read a copy that can trail the Paper.
- **Two launch paths.** The Coach has its own task kind (`paper_coach`),
  request (`CoachRequest`), permission profile per provider (`paper_suggest`,
  `rcp_paper`), sessions (`WritingSession`), steering store
  (`paper_coach_steers`), remote recovery, refresh rounds, and Review/Apply
  modes. Most of it duplicates ordinary chat Work (`runs/tasks/coach.py`).
- **Staging and cards.** Each Coach round reads a frozen snapshot, writes whole
  files to a changes folder, and RCP diffs them into cards. A slow turn applies
  late and clashes with anything typed meanwhile.

## Settled decisions

Decided with the human in chat:

1. **One file system.** The Paper is files on disk. Every Coach turn edits the
   same files the human edits.
2. **Read.** Every agent reads the project repositories and the Paper.
3. **Write.** Only the Coach writes the Paper. Other agents keep today's write
   scope.
4. **One path.** The Coach is an ordinary project chat with a writing prompt and
   one more write root. Its own path, modes, refresh rounds, and cards are
   deleted. It behaves like a normal coding agent.
5. **Review in chat.** A turn's Paper edits show in the chat as a folded list of
   changed files. A row opens its diff beside the chat; any line takes a shared
   comment, and Send returns it to the agent. Red rows (clashes) sort first.
6. **Clash rule.** When the human and the Coach change the same lines, the
   human's text wins. The Coach's version shows in the red row.
7. **Paper Git.** On an overlap between GitHub and the Paper, GitHub wins.
8. **Self-contained output.** Prompts say the Paper preview cannot build sites;
   when something does not render, make a self-contained HTML version or an
   artifact. Artifacts get **Save to Paper**.

## Non-goals

- No Undo button on a turn. History and the Coach revert.
- No site builds in the Paper preview.
- No dependency on a live file watcher.
- No Git repository inside the folder.

## The folder

The folder follows the project memory folder (`agents/memory_file_io.py`,
`agents/memory_files.py`), which is already a code-defined write root inside
RCP-owned storage.

- **Local base** (a team project on the server, or a desktop project):
  `<data_dir>/paper/<project_id>/`.
- **SSH base:** `~/.rcp/paper/<project_id>/` on the base host, created by a
  shipped script like `RemoteRunStage.project_memory`.

Both sit under `rcp_owned_paths`, outside every repository and outside
`.research`, so no repository grant reaches them. Agents run on the base
machine, so the Coach edits local files. Project rename and delete follow
`change_project_memory`. Backup and transfer list `paper` as an app-data root;
an SSH base is read through the file module below.

**File module.** New `paper/folder_io.py` is stdlib-only, runs in-process
locally, and ships over SSH as `memory_file_io.py` does. Operations:
- `scan`: stat data only (path, size, `mtime_ns`, inode) via `lstat`. It skips a
  code-defined ignore list (swap files, `.DS_Store`, `__pycache__`, LaTeX and
  Typst outputs). A symlink, hard link, or special file is skipped and
  reported; the tree shows it as unsupported and the turn lists it.
- `read(paths)` and `hash(paths)`.
- `write(path, bytes)`: an existing path is replaced by a temp file and an
  atomic swap (`renameat2(RENAME_EXCHANGE)` / `renamex_np(RENAME_SWAP)`, as
  `_select_paper_tree` does today). The swapped-out inode is held until its
  stat is stable for one interval, then re-read; if it changed after the swap,
  those bytes are merged as a foreign write. A new path is created no-clobber
  with `os.link`, as `memory_file_io._write` does.
- `delete` and `rename`.

## What SQLite keeps

SQLite keeps caches, history, and identity. Losing them loses no content.

| Table | New role |
|---|---|
| `paper_files` | Identity index only: `file_id`, `path`, `kind`, `deleted_at`. Comments and editor document keys target `file_id`. Rebuilt from scans; the disk wins. |
| `paper_doc_states` | The editor's buffer cache, plus `text_sha256` of the disk text it equals, so browser IndexedDB stores still match after a restart. |
| `paper_versions`, `paper_version_texts`, `paper_blobs` | History, unchanged, now snapshotted from the folder. Restore writes files. |
| `paper_git_links` | Unchanged. `ours` is a folder snapshot; applies write files. |
| `paper_state` (new) | `(project_id, revision)`: the per-project counter. |
| `paper_turn_files` (new) | `(operation_id, path, base_sha, coach_sha, clash, kind)`; bytes in `paper_blobs`. |
| `paper_migrations` (new) | `(project_id)`: written after a project's last migration step. |

Deleted after migration: `paper_saved_snapshots`, `paper_revisions`,
`paper_publication`, `paper_updates`, `paper_change_groups`, `paper_changes`,
`paper_change_receipts`, `paper_drafts`, `paper_coach_steers`.

**Revision.** `paper_state.revision` is bumped by every save and by every scan
that finds a change. "Current" or a `version_id` addresses a past state; a
bare revision no longer does.
- `paper_versions.revision`: the counter when the version was taken.
- `paper_git_links.synced_revision`: the counter at the last sync.
- `paper_doc_states.through_revision`: the counter the cache equals.
  `through_seq` goes with `paper_updates`.
- The `revision` query param on the raw, preview, and HTML-preview routes in
  `api/paper.py`, and `PaperCompileRequest.revision`, become `version_id`;
  absent means current.
- `Receipt(revision=…)`: the counter after the write.
- `PaperLive.apply` drops `expect`. `PaperLive.snapshot` and
  `PaperLive.text_at` take a `version_id` or nothing for current.

## The editor loop

One per-project loop in `PaperLive` replaces the publisher. Rooms, `receive`,
framing, acks, and broadcast stay.

- **Load.** Read the disk text. If the cache's `text_sha256` matches, use the
  cached Yjs state. Otherwise apply `text_replacement(cached, disk)` so the
  disk wins.
- **Save** (about 1 s after typing stops, on room close, and before any
  snapshot): `write`, then the cache, then the ack. An ack now means "on
  disk". Unacked text stays in the browser's IndexedDB as today.
- **Reload.** A poll scans the folder while the project has viewers or a Paper
  turn runs. It hashes only paths whose stat changed, and reads a changed file
  once its stat is stable for one interval. New constants in `rcp/limits.py`:
  `PAPER_POLL_SECONDS` (1 s), `PAPER_REMOTE_POLL_SECONDS` (2 s),
  `PAPER_POLL_MAX_FILES`, and `PAPER_POLL_MAX_HASH_MB` (per poll; the rest
  waits for the next poll). A disk hash that differs from `last_known` is a
  foreign write. For an open file it merges in memory with
  `text_merge(base, ours=buffer, theirs=disk)`; on overlap the buffer wins.
  The merged text goes to viewers as a Yjs update and is saved if it differs
  from disk. Any change bumps the `paper` watermark so the file tree refreshes.
- **Merge base for agent writes.** For each path, the base is the agent's
  previous write to that path in this turn, or the turn-start version if
  there is none. Human edits the agent never saw always count as ours. No
  time window. A foreign change to a binary file: the agent's bytes win,
  unless a human uploaded that path during the turn; that is a clash and the
  human wins.
- **Foreign delete or rename.** If the file is open and its buffer changed
  since its base, it is a clash: the human wins, the file is recreated, and
  the turn gets a red row. Otherwise the delete is accepted.
- **Renames.** In one scan, a vanished path and a new path with the same hash
  keep the `file_id`, so comments survive.
- **Outside a turn.** A foreign write with no Paper turn running still merges
  (the human wins) and is listed on the next Paper turn as "outside a turn".

**Multi-file writes** (tree routes, upload, rename, History restore, Paper Git
apply) write files in path order. Each file is a three-way merge: base is what
the operation read, theirs is the operation's result, ours is disk or buffer
now. On overlap the operation wins for human actions (restore, upload,
rename) and GitHub wins for Paper Git apply; non-overlapping keystrokes
survive. An I/O failure stops the write and reports the files already
written; rerunning is idempotent. The client no longer sends `expect`. These
writes go through the save, so only agent writes count as foreign.

One poll path serves local and SSH; there is no watcher dependency.

## The Coach chat

- **Focus.** A chat has `focus`, `null` or `"paper"`, fixed when it is created.
  It is stored as `agent_threads.focus TEXT CHECK (focus IS NULL OR focus =
  'paper')`. The request that creates a chat gets a `focus` field.
  `RunRequest.focus` and `AgentDispatchScope.focus` default to `None` for old
  rows, so recovery keeps it. The Coach pane lists the member's
  `focus = "paper"` chats.
- **Always Work.** A Paper chat has no Discuss mode. Its turns run with
  capability `work_auto`.
- **Write scope.** `ProjectWriteScope.paper_root` (new) is set only for
  `focus = "paper"`. It is handled like `memory_roots`: added to writable
  roots, in the fingerprint when set, and carved out of the RCP-owned deny list
  per provider the same way.
- **One Paper turn per project.** `admit_fresh_chat_turn`
  (`runs/chat_admission.py`) refuses a second Paper turn in the same project
  while one runs, and the turn stays in the thread queue
  (`runs/agent_thread_queue.py`). It shows as queued with "Waiting for another
  Paper turn", and the member can cancel it. Any settle (completed,
  interrupted, failed, recovered) releases the gate.
- **Turn end.** A turn ends only after the provider process tree has exited or
  recovery has settled, so no late write escapes the list.
- **Prompt.** The writing guidance moves into the Work contract when `focus` is
  `paper`, rendered from the resolved scope. It names the folder, says to build
  in the artifact folder, and says: "Files change under you. Re-read before
  each edit. If a person reverted your change, keep their text and mention
  it." Other launch kinds name the folder as the Paper, read-only;
  Auto-research gains the line.
- **Deleted.** `runs/tasks/coach.py`, `CoachRequest`, the `paper_suggest`
  capability and provider profiles, `WritingSession`, `paper_coach_steers`,
  refresh rounds, `PaperChanges` (keep `line_changes`), the change-card routes
  and UI, publication (`publish_paper_tree`, `_select_paper_tree`, the `paper`
  generation, `.research/paper`), and the publication badge.

## A turn's change list

- **Start.** Flush the editor and save a history version.
- **During.** Each foreign write appends a `paper_turn_files` row: path,
  `base_sha`, `coach_sha`, `clash`, and `kind` (`write`, `delete`, `rename`,
  `outside_turn`, `unsupported`). Bytes go into `paper_blobs`, so a restart
  mid-turn keeps them.
- **End.** Flush, scan, save the after version. Add any path whose hash
  changed and differs from the editor's `last_known` (files nobody had open).
  One `graph_run_receipts` row with category `paper_turn` (new) stores:

  ```json
  {"before_version_id": "…", "after_version_id": "…",
   "files": [{"path": "…", "kind": "write", "clash": false,
              "base_sha": "…", "coach_sha": "…"}]}
  ```
- **Rows.** "N files changed", with +/− totals. Each row diffs before against
  the Coach's version with `line_changes`. A red row shows the Coach's version
  beside the current file. A member reverts through History or asks the Coach.
- **Comments.** A diff line takes a shared comment with target kind `reply`, so
  readability follows the chat. It uses the `element` anchor: `path` is the
  file, `label` is the line ("new line 12"), `text` is the line text.
- **Routes.** `GET …/paper/turns/{op}` and `GET …/paper/turns/{op}/files/{path}`.
- **Prune** (`paper/persistence.py:prune`) keeps only versions referenced by
  `paper_turn` receipts of chats that still exist.

## Paper Git

Paper Git keeps its bare repository and link row. `ours` becomes a folder
snapshot, and apply is a multi-file write where GitHub wins overlaps
(decision 7). Its cards are deleted with the rest.

## Migration

Runs at startup, per project with Paper data and no `paper_migrations` row:
1. Materialize the current saved snapshot with `materialize` into a sibling
   temp directory, then rename it into place. A leftover temp directory is
   deleted on retry.
2. Fold the `paper_updates` tail into `paper_doc_states`, store
   `text_sha256`, and keep the old `file_id`s.
3. A legacy draft hold already has its canonical saved as a version; clear it.
4. Settle queued or running `paper_coach` tasks as interrupted, and reject
   pending card groups, each with a notice.
5. Render old `paper_coach` rows into static transcript text.
6. Remove `.research/paper`, then write the `paper_migrations` row. A crash
   before it resumes at the first unfinished step. Drop the deleted tables in a
   later release.

The legacy `paper_coach` parsing module is deleted; old rows read only as the
rendered text.

## Save to Paper

The artifact viewer gains **Save to Paper**: pick a folder, and the artifact's
current version becomes a Paper file through the ordinary upload path.

## Alternatives considered

- **SQLite truth plus a two-way mirror.** Two copies drift; every bug becomes a
  sync bug.
- **Staging plus cards.** Today's design: slow turns apply late and clash.
- **A file watcher instead of polling.** No reliable watcher over SSH; polling
  is one path.
- **A time-window merge base.** A slow agent falls outside any window and
  overwrites human text.
- **History as a Git repo in the folder.** Agents would see and edit it;
  history stays in SQLite.

## Risks

- **Poll cost** on large folders over SSH. Stat-only scans and the per-poll
  hash caps bound it.
- **Agent tools that write in place** (truncate, then write). The poll may read
  a partial file; the stable-stat wait and the merge base limit the damage.
- **History growth** from turn versions. Prune keeps only versions referenced
  by receipts of chats that still exist.

## Invariants and specs that change

- AGENTS.md invariant 4: "The paper coach writes only its own changes folder.
  RCP turns those changes into paper change cards." becomes "A Paper-focused
  chat also writes the Paper folder."
- providers-and-containment.md: the Paper coach row and profiles.
- paper-artifacts-and-result-views.md: storage, publishing, Writing coach,
  cards.
- Backup and transfer specs: the `paper` app-data root.

## Checks

- Folder module: a write that lands after the swap is merged; a new path is
  no-clobber; links and special files are skipped and reported; ignore list;
  `scan` returns stat data only.
- Revision: saves and changed scans bump the counter; routes and compile
  address `version_id` or current.
- Editor loop: load from cache and from changed disk; save acks only after the
  write; a foreign write merges into an open buffer with the human winning; a
  slow agent write against its turn-start base keeps human text; a binary
  clash with a human upload; a foreign delete of an edited open file is
  recreated; rename keeps `file_id`; restart between disk write and cache;
  poll caps.
- Multi-file writes: path order; keystrokes outside the overlap survive; an
  I/O failure reports written files and a rerun is idempotent.
- Turn list: other members' saves never appear; files with no open buffer
  appear; a clash is a red row; a write outside a turn lists on the next one;
  restart mid-turn keeps `paper_turn_files`; the turn ends only after the
  process tree exits.
- Gate: a second Paper turn queues, can be cancelled, and starts after the
  first settles in each way (completed, interrupted, failed, recovered).
- Launch: `focus = "paper"` adds exactly the Paper root and runs `work_auto`;
  other launches cannot write it; old rows load with `focus = None`; prompts
  per launch kind.
- Migration on a copy of real Paper data, including a crash after each step;
  old `paper_coach` rows show as text; backup and restore round-trip a local
  and an SSH Paper.
- Claude Code's Edit tool against a file a member is typing in: the member's
  text survives and the turn reports it.
- Served drive: a real Coach turn edits several files while a member types in
  one; the chat shows the list with one red row; a diff comment reaches the
  agent; 390px.
