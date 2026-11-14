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

**File module.** `paper/folder_io.py` is stdlib-only, runs in-process locally,
and ships over SSH as `memory_file_io.py` does. Operations:
- `scan`: path, size, mtime, inode, sha256. It uses `lstat`, skips a
  code-defined ignore list (swap files, `.DS_Store`, `__pycache__`, LaTeX and
  Typst outputs), and refuses symlinks, hard links, and special files.
- `read(paths)`.
- `write(path, bytes, expect_sha)`: temp file, then an atomic swap
  (`renameat2(RENAME_EXCHANGE)` / `renamex_np(RENAME_SWAP)`, as
  `_select_paper_tree` does today). It returns the swapped-out bytes so a write
  that landed in between is never lost.
- `delete` and `rename`.

## What SQLite keeps

SQLite keeps caches, history, and identity. Losing them loses no content.

| Table | New role |
|---|---|
| `paper_files` | Identity index only: `file_id`, `path`, `kind`, `deleted_at`. Comments and editor document keys target `file_id`. Rebuilt from scans; the disk wins. |
| `paper_doc_states` | The editor's buffer cache, plus `text_sha256` of the disk text it equals, so browser IndexedDB stores still match after a restart. |
| `paper_versions`, `paper_version_texts`, `paper_blobs` | History, unchanged, now snapshotted from the folder. Restore writes files. |
| `paper_git_links` | Unchanged. `ours` is a folder snapshot; applies write files. |

Deleted after migration: `paper_saved_snapshots`, `paper_revisions`,
`paper_publication`, `paper_updates`, `paper_change_groups`, `paper_changes`,
`paper_change_receipts`, `paper_drafts`, `paper_coach_steers`.

## The editor loop

One per-project loop in `PaperLive` replaces the publisher. Rooms, `receive`,
framing, acks, and broadcast stay.

- **Load.** Read the disk text. If the cache's `text_sha256` matches, use the
  cached Yjs state. Otherwise apply `text_replacement(cached, disk)` so the
  disk wins.
- **Save** (about 1 s after typing stops, on room close, and before any
  snapshot): `write(path, text, expect_sha=last_known)`, then the cache, then
  the ack. An ack now means "on disk". Unacked text stays in the browser's
  IndexedDB as today.
- **Reload.** A poll scans the folder every second locally and every 2 s over
  SSH while the project has viewers or a Coach turn runs. A changed file is read
  once its stat is stable for one interval. A disk hash that differs from
  `last_known` is a foreign write. For an open file it merges in memory with
  `text_merge(base, ours=buffer, theirs=disk)`; on overlap the buffer wins.
  The merged text goes to viewers as a Yjs update and is saved if it differs
  from disk. Any change bumps the `paper` watermark so the file tree refreshes.
- **Stale agent writes.** The loop keeps each path's last few saved versions.
  A foreign write that arrives within a few seconds of a save merges against
  the older base, so a human edit the agent never read still counts as ours.
- **Renames.** In one scan, a vanished path and a new path with the same hash
  keep the `file_id`, so comments survive.
- **Every other writer** (tree routes, upload, rename, History restore, Paper
  Git apply) goes through the same save with `expect_sha`, so only agent writes
  count as foreign.

One poll path serves local and SSH; there is no watcher dependency.

## The Coach chat

- **Focus.** A chat has `focus`, `null` or `"paper"`, fixed when it is created
  and stored on the chat record. It travels in `RunRequest` and
  `AgentDispatchScope`, so recovery keeps it. The Coach pane lists the member's
  `focus = "paper"` chats.
- **Write scope.** `ProjectWriteScope.paper_root` is set only for
  `focus = "paper"`. It is handled like `memory_roots`: added to writable roots,
  in the fingerprint when set, and carved out of the RCP-owned deny list per
  provider the same way.
- **One writing turn per project.** A second Paper turn in the same project
  waits until the first ends, because a turn's changes are every foreign write
  during it.
- **Prompt.** The writing guidance moves into the Work contract when `focus` is
  `paper`, rendered from the resolved scope. It names the folder and says to
  build in the artifact folder. Other launch kinds name the folder as the
  Paper, read-only; Auto-research gains the line.
- **Deleted.** `runs/tasks/coach.py`, `CoachRequest`, the `paper_suggest`
  capability and provider profiles, `WritingSession`, `paper_coach_steers`,
  refresh rounds, `PaperChanges` (keep `line_changes`), the change-card routes
  and UI, publication (`publish_paper_tree`, `_select_paper_tree`, the `paper`
  generation, `.research/paper`), and the publication badge.

## A turn's change list

- **Start.** Flush the editor and save a history version. Its id goes in a
  `graph_run_receipts` row with category `paper_turn`.
- **During.** The loop records each foreign write for the running turn: path,
  hash, and whether the merge kept the human's text (a clash). The bytes go
  into the content store.
- **End.** Flush, scan, save the after version. The turn's paths are the
  recorded foreign writes plus any path whose hash changed and differs from the
  editor's `last_known` (files nobody had open, or a restart mid-turn). One
  receipt stores the list.
- **Rows.** "N files changed", with +/− totals. Each row diffs before against
  the Coach's version with `line_changes`. A red row shows the Coach's version
  beside the current file. There is no Undo button; a member reverts through
  History or asks the Coach.
- **Comments.** A diff line takes a shared comment with target kind `reply`, so
  readability follows the chat. It uses the `element` anchor: `path` is the
  file, `label` is the line ("new line 12"), `text` is the line text.
- **Routes.** `GET …/paper/turns/{op}` and `GET …/paper/turns/{op}/files/{path}`.
- **Prune** keeps versions a `paper_turn` receipt references.

## Paper Git

Paper Git keeps its bare repository and link row. `ours` becomes a folder
snapshot, and apply writes files through the editor save. On an overlap GitHub's
text wins (decision 7); its cards are deleted with the rest.

## Migration

Per project with Paper data:
1. Write the current saved snapshot into the empty new folder with
   `materialize`. A non-empty folder is refused, so the step is idempotent.
2. Fold the `paper_updates` tail into `paper_doc_states`, store
   `text_sha256`, and keep the old `file_id`s.
3. A legacy draft hold already has its canonical saved as a version; clear it.
4. Settle queued or running `paper_coach` tasks as interrupted, and reject
   pending card groups, each with a notice.
5. Remove `.research/paper`. Drop the deleted tables in a later release.

Old `paper_coach` rows parse through a frozen legacy module for read-only replay.

## Save to Paper

The artifact viewer gains **Save to Paper**: pick a folder, and the artifact's
current version becomes a Paper file through the ordinary upload path.

## Invariants and specs that change

- AGENTS.md invariant 4: "The paper coach writes only its own changes folder.
  RCP turns those changes into paper change cards." becomes "A Paper-focused
  chat also writes the Paper folder."
- providers-and-containment.md: the Paper coach row and profiles.
- paper-artifacts-and-result-views.md: storage, publishing, Writing coach,
  cards.
- Backup and transfer specs: the `paper` app-data root.

## Checks

- Folder module: swap returns interleaved bytes; links and special files
  refused; ignore list.
- Editor loop: load from cache and from changed disk; save acks only after the
  write; a foreign write merges into an open buffer with the human winning; a
  stale agent write against an older base; rename keeps `file_id`; restart
  between disk write and cache.
- Turn list: other members' saves never appear; files with no open buffer
  appear; a clash is a red row; two Paper turns in one project serialize.
- Launch: `focus = "paper"` adds exactly the Paper root; other launches cannot
  write it; prompts per launch kind.
- Migration on a copy of real Paper data; old `paper_coach` rows replay;
  backup and restore round-trip a local and an SSH Paper.
- Served drive: a real Coach turn edits several files while a member types in
  one; the chat shows the list with one red row; a diff comment reaches the
  agent; 390px.
