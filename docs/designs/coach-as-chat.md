# The Paper is a folder; the Coach is an ordinary chat

The Paper becomes a folder of files on the base machine. Those files are the
Paper. The Paper editor is an editor over them, and the Coach is an ordinary
project chat that edits them like any coding agent edits a repository. No
staging, no mirror, no change cards.

## Problem

- **Two copies.** Paper content lives in SQLite and is republished into
  `.research/paper` after idle editing. Agents read a copy that can trail.
- **Two launch paths.** The Coach has its own task kind, request, provider
  profiles, sessions, steering store, recovery, refresh rounds, and modes
  (`runs/tasks/coach.py`). Most of it duplicates ordinary chat Work.
- **Staging and cards.** A Coach round writes whole files to a changes folder
  that become cards. A slow turn applies late and clashes with typing.

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

No Undo button on a turn, no site builds in the preview, no live file watcher
dependency, and no Git repository inside the folder.

## The folder

The folder follows the project memory folder (`agents/memory_file_io.py`,
`agents/memory_files.py`), which is already a code-defined write root inside
RCP-owned storage.

- **Local base:** `<data_dir>/paper/<project_id>/`.
- **SSH base:** `~/.rcp/paper/<project_id>/`, created by a shipped script like
  `RemoteRunStage.project_memory`.

Both sit under `rcp_owned_paths`, outside every repository and `.research`.
Agents run on the base machine, so the Coach edits local files. Rename and
delete follow `change_project_memory`; backup and transfer list `paper` as an
app-data root.

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
- `paper_versions.revision`, `paper_git_links.synced_revision`, and
  `paper_doc_states.through_revision`: the counter when the version was taken,
  at the last sync, and that the cache equals. `through_seq` goes with
  `paper_updates`.
- The `revision` query param on the raw, preview, and HTML-preview routes in
  `api/paper.py`, and `PaperCompileRequest.revision`, become `version_id`;
  absent means current.
- `Receipt(revision=…)`: the counter after the write.
- `PaperLive.apply` drops `expect`. `PaperLive.snapshot` and
  `PaperLive.text_at` take a `version_id` or nothing for current.

## The editor loop

One per-project loop in `PaperLive` replaces the publisher. Rooms, `receive`,
framing, acks, and broadcast stay.

- **Load.** If the cache's `text_sha256` matches disk, use the cached Yjs
  state; else apply `text_replacement(cached, disk)` so the disk wins.
- **Save** (about 1 s after typing stops, on room close, before a snapshot):
  `write`, then the cache, then the ack, which now means "on disk".
- **Reload.** While there are viewers or a Paper turn, a poll scans, hashes
  only stat-changed paths, and reads one once its stat is stable for one
  interval. New `rcp/limits.py` constants: `PAPER_POLL_SECONDS` (1 s),
  `PAPER_REMOTE_POLL_SECONDS` (2 s), and per-poll caps `PAPER_POLL_MAX_FILES`
  and `PAPER_POLL_MAX_HASH_MB`. A hash that differs from `last_known` is a
  foreign write; an open file merges with `text_merge(base, ours=buffer,
  theirs=disk)`, buffer winning overlaps, and goes to viewers as a Yjs update.
  Any change bumps the `paper` watermark.
- **Merge base.** Per path, the agent's previous write in this turn, else the
  turn-start version. Human edits the agent never saw always count as ours.
  For a binary file the agent's bytes win, unless a human uploaded that path
  during the turn: a clash, the human wins.
- **Foreign delete or rename** of an open file whose buffer changed since its
  base is a clash: the file is recreated with a red row. Else it is accepted.
- **Renames.** A vanished path and a new path with the same hash in one scan
  keep the `file_id`.
- **Outside a turn,** a foreign write still merges (human wins) and is listed
  on the next Paper turn as "outside a turn".

**Multi-file writes** (tree routes, upload, rename, restore, Paper Git apply)
go through the save in path order, so only agent writes are foreign. Each file
merges three ways: base is what the operation read, theirs its result, ours
disk or buffer now. Overlaps go to the operation for human actions and to
GitHub for Paper Git; other keystrokes survive. An I/O failure stops and
reports the files written; a rerun is idempotent. The client drops `expect`.

## The Coach chat

- **Focus.** A chat has `focus`, `null` or `"paper"`, fixed when it is created.
  It is stored as a new column `chat_display.focus TEXT CHECK (focus IS NULL OR
  focus = 'paper')`, one row per chat. A chat starts with its first turn, so
  `AgentTaskRequest.focus` sets it on that turn; later turns that name a
  different focus are refused with 409.
  `RunRequest.focus` and `AgentDispatchScope.focus` default to `None` for old
  rows, so recovery keeps it. The Coach pane lists the member's
  `focus = "paper"` chats.
- **Always Work** (`work_auto`). A Paper chat has no Discuss mode.
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
- **Turn end** waits until the provider process tree exits or recovery settles.
- **Prompt.** The writing guidance moves into the Work contract when `focus` is
  `paper`, rendered from the resolved scope. It names the folder, says to build
  in the artifact folder, and says: "Files change under you. Re-read before
  each edit. If a person reverted your change, keep their text and mention
  it." Other launch kinds name the folder as the Paper, read-only;
  Auto-research gains the line.
- **Deleted.** `runs/tasks/coach.py`, `CoachRequest`, `paper_suggest` and its
  profiles, `WritingSession`, `paper_coach_steers`, refresh rounds,
  `PaperChanges` (keep `line_changes`), change cards, publication
  (`publish_paper_tree`, `_select_paper_tree`, `.research/paper`), and its badge.

## A turn's change list

- **Start.** Flush the editor and save a history version.
- **During.** Each foreign write appends a `paper_turn_files` row: path,
  `base_sha`, `coach_sha`, `clash`, and `kind` (`write`, `delete`, `rename`,
  `outside_turn`, `unsupported`). Bytes go into `paper_blobs`, so a restart
  mid-turn keeps them.
- **End.** Flush, scan, save the after version. Add any path whose hash
  changed and differs from the editor's `last_known` (files nobody had open).
  One `graph_run_receipts` row with category `paper_turn` (new) stores:

  `{"before_version_id", "after_version_id", "files": [{"path", "kind",
  "clash", "base_sha", "coach_sha"}]}`.
- **Rows.** "N files changed", with +/− totals. Each row diffs before against
  the Coach's version with `line_changes`; a red row shows it beside the
  current file.
- **Comments.** A diff line takes a shared `reply` comment with the `element`
  anchor: `path` is the file, `label` the line, `text` the line text.
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
3. Clear legacy draft holds (their canonical is already a version).
4. Settle queued or running `paper_coach` tasks as interrupted and reject
   pending card groups, each with a notice.
5. Render old `paper_coach` rows into static transcript text.
6. Remove `.research/paper`, then write the `paper_migrations` row. A crash
   before it resumes at the first unfinished step. Drop the deleted tables in a
   later release.

The legacy `paper_coach` parsing module is deleted.

## Save to Paper

The artifact viewer's **Save to Paper** picks a folder and uploads the
artifact's current version through the ordinary upload path.

## Alternatives considered

- **SQLite truth plus a two-way mirror:** two copies drift.
- **Staging plus cards:** slow turns apply late and clash.
- **A file watcher:** none is reliable over SSH; polling is one path.
- **A time-window merge base:** a slow agent falls outside it.
- **History as a Git repo in the folder:** agents would see and edit it.

## Risks

- **Poll cost** on large folders over SSH; stat-only scans and hash caps bound it.
- **Agent tools that write in place** may be read half-written; the stable-stat
  wait and the merge base limit it.
- **History growth** from turn versions; prune bounds it.

## Invariants and specs that change

- AGENTS.md invariant 4: "The paper coach writes only its own changes folder.
  RCP turns those changes into paper change cards." becomes "A Paper-focused
  chat also writes the Paper folder."
- providers-and-containment.md (Coach row, profiles);
  paper-artifacts-and-result-views.md (storage, publishing, Coach, cards);
  backup and transfer specs (the `paper` app-data root).

## Checks

- Folder module: a write landing after the swap merges; new paths are
  no-clobber; links and special files are skipped and reported; stat-only scan.
- Revision: saves and changed scans bump it; routes take `version_id` or current.
- Editor loop: cache and changed-disk load; ack after write; open-buffer merge
  keeps human text; slow agent write against its turn-start base; binary clash
  with an upload; edited open file recreated after a foreign delete; rename
  keeps `file_id`; restart between disk and cache; poll caps.
- Multi-file writes: path order; keystrokes outside the overlap survive; an
  I/O failure reports written files; a rerun is idempotent.
- Turn list: other members' saves never appear; unopened files appear; a clash
  is a red row; outside-turn writes list next turn; restart mid-turn keeps
  `paper_turn_files`; the turn ends only after the process tree exits.
- Gate: a second Paper turn queues, can be cancelled, and starts after each
  kind of settle.
- Launch: `focus = "paper"` adds only the Paper root and runs `work_auto`;
  other launches cannot write it; old rows load with `focus = None`.
- Migration on a copy of real Paper data with a crash after each step; old
  `paper_coach` rows show as text; backup round-trips a local and SSH Paper.
- Claude Code's Edit tool on a file a member is typing in keeps the member's text.
- Served drive: a Coach turn edits several files while a member types in one;
  one red row; a diff comment reaches the agent; 390px.
