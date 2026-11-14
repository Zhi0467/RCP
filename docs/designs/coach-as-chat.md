# The Coach as an ordinary chat on a live Paper folder

Retire the Paper Coach's own launch path. The Coach becomes an ordinary project
chat with a writing prompt and one extra capability: it writes the Paper. The
Paper folder becomes a live, two-way mirror of the Paper, so the Coach edits the
same files every turn, with no staging or change cards.

## Problem

- **Two launch paths.** The Coach has its own task kind (`paper_coach`), request
  (`CoachRequest`), permission profile per provider (`paper_suggest`,
  `rcp_paper`), sessions (`WritingSession`), steering store
  (`paper_coach_steers`), remote recovery (`_CoachStage`/`_CoachRound`), refresh
  rounds, and Review/Apply modes. Most of it duplicates ordinary chat Work
  (`runs/tasks/coach.py`).
- **Staged copies.** Each Coach round reads a frozen snapshot (`paper-base-N`)
  and writes whole files to a changes folder. RCP diffs them into cards. A
  slow turn applies late, and its edits clash with anything typed meanwhile.
- **Cards.** Cards existed for the Review mode, which is gone. The human now
  reviews a turn's diff in chat and comments on it.
- **A stale read copy.** Other agents read `.research/paper`, which RCP
  republishes as a whole tree after idle editing, so it can trail the Paper.
  Auto-research prompts never mention it.

## Settled decisions

Decided with the human in chat:

1. **Read.** Every agent reads the project repositories and the Paper.
2. **Write.** Only the Coach writes the Paper. Every other agent keeps today's
   write scope (exact repository roots).
3. **One path.** The Coach is an ordinary project chat. It gets the same launch
   path, session, steering, recovery, and permissions as other Work chats, plus
   Paper write. The Coach-specific path, modes, refresh rounds, and cards are
   deleted.
4. **No staging.** One Paper folder; every Coach turn edits it directly.
5. **Review in chat.** A turn's Paper edits show in the chat as a folded list of
   changed files. Clicking one opens its diff beside the chat. Any line takes a
   shared comment, and Send returns it to the agent. Failed or clashing files
   are red rows at the top.
6. **Clash rule.** When the human and the Coach edit the same lines within the
   mirror's lag, the human's text wins and the Coach's version waits in the red
   row.
7. **Self-contained output.** Agent prompts say the Paper preview cannot build
   sites (Jekyll and similar); when something does not render, make a
   self-contained HTML version or an artifact. A **Save to Paper** action on
   artifacts copies one into the Paper.

## Where things run

Agents run on the base machine, and the Paper folder lives in the state root.
For a team project both are the team server, together with the backend and its
SQLite, so the mirror below is all local. The one split case is a personal
project whose state root is an SSH host: the agent and the folder are on that
host while SQLite is in the desktop backend. There the folder is read back at
the end of each Coach turn instead of watched (decided with the human). The
local mirror that `_pull_remote_tree` keeps of the remote root is a read-only
cache; scans and writes go to the remote folder through a shipped remote script
that does the same per-file exchange.

A Paper-focused chat always runs on the machine that holds the state root.
Asking it to run elsewhere is refused with the reason. Other chats that run on
another machine read the Paper only when that machine is the state host, as
today.

## The live Paper folder

The Paper's file view moves out of `.research` to `<state root>/paper/`, so a
write grant on it never touches graph or chat state (invariants 1 and 4b). The
old `.research/paper` is removed on upgrade after the first full write.

SQLite stays the truth for history, co-editing, GitHub sync, and backup. The
folder is kept equal to it in both directions. This extends two existing pieces
instead of adding new ones: the Paper publisher (`LivePaper._publish` and the
`paper_publication` table) and Paper Git's merge (`paper/git_merge.py`
`merge_trees` with the apply loop in `PaperGit._merge`).

**One owner.** The watcher, merge, bases, and folder writer live in one module
behind `StateWorkspace`, with a local and a remote implementation, as publish is
today. Nothing else reads or writes the folder's bookkeeping.

**Safety.** Every entry is checked with `lstat`. Symlinks, hard links
(`st_nlink > 1`), and special files are refused and reported, never followed or
imported. RCP writes through directory file descriptors with `O_NOFOLLOW` at
every level and refuses to start when the folder root is not a plain directory.
The write root is the resolved canonical path, compared by filesystem identity.

**Ignored names.** A code-defined list (editor swap files, `.DS_Store`,
`__pycache__`, LaTeX and Typst build outputs) is never imported. The Coach's
prompt says to build in its artifact folder. A refused or ignored file is
reported once per turn and left alone.

**What RCP last wrote.** A new table `paper_folder_files(project_id, path,
sha256, revision)` records, per file, the bytes RCP last wrote to the folder.
It is the merge base for folder edits and survives restarts. A folder file equal
to the live Paper file is a no-op; there is no separate echo rule.

**Paper → folder.** The publisher stops swapping whole trees. It writes only
changed paths, per file:
1. Write the new bytes to a sibling temp file.
2. Exchange it with the target atomically (`renameat2(RENAME_EXCHANGE)` on
   Linux, `renamex_np(RENAME_SWAP)` on macOS, as `_select_paper_tree` does).
3. Compare the swapped-out bytes with `paper_folder_files`. If they differ, a
   folder edit landed after the last read: merge it in as a folder edit before
   recording the new base. RCP never overwrites an unread edit.

Writes are coalesced per file and flushed every 2 seconds, immediately before a
Coach turn starts, and before its final scan. The `paper_publication` row keeps
its role: `published_revision` becomes the folder revision (every file written
up to it). `hold`, failure backoff, `pending_since`, the editor badge, timing
pruning, backup's republish marker, and import publication keep working on it.
A full reconcile from SQLite runs at startup, after restore and import, on
unpause, and on SSH reconnect, so a crash between commit and write is repaired.
`publish_paper_tree` and `_select_paper_tree` are replaced by the per-file
writer; the `.publish` exclusions in backup and transfer go with them.

**One writing turn.** At most one Paper-writing turn runs per project. Another
Coach turn queues behind it, as a busy chat does. Every folder edit during the
turn is credited to it.

**Folder → Paper.** On local state roots the owner watches the folder with
`watchfiles` (declared as a direct dependency) while the project is open, so
any edit reaches Paper. On SSH state roots RCP scans by content hash at the end
of each Coach turn and at the reconcile points above.
- A changed path becomes one call to `merge_trees(base, theirs, ours)`: base =
  the bytes in `paper_folder_files`, theirs = the folder, ours = live Paper.
- Clean results apply through Paper Git's apply loop as a Paper revision whose
  actor is `coach:<operation id>` during a Coach turn, else `folder`.
- **Renames.** A delete plus a create with the same content in one scan is a
  rename and keeps the Paper file id. A rename with edits is a delete plus a
  create. A write to a path a member renamed during the turn is a clash.
- **Mass deletes.** A scan that would delete more than 20 files, or more than a
  quarter of the Paper, applies none of those deletes and shows one red row
  asking the member to confirm.
- **Clashes.** An overlap keeps the live text (decision 6). The folder's side
  is held with Paper Git's held-file mechanism under the turn's task id. That
  held side is the red row.
- Refused paths and limits are skipped with Paper Git's reasons and show as red
  rows.

**Builds.** Compile and preview already read SQLite snapshots
(`paper/compile.py` `materialize`), never the folder.

**GitHub.** A Paper Git pull is an ordinary Paper revision, so it reaches the
folder like any other edit. The folder never talks to GitHub.

## The Coach chat

- **Mode.** A Coach chat is an ordinary Work chat; there is one chat mode.
- **Focus.** A chat has `focus`, `null` or `"paper"`, fixed when the chat is
  created and stored on the chat record. Requests carry it into `RunRequest`
  and `AgentDispatchScope`, so recovery and frozen authority keep it. The Coach
  pane lists the member's chats with `focus = "paper"`. The policy version of
  the Work task contract bumps.
- **Write scope.** `ProjectWriteScope` gains `paper_root`, set only for
  `focus = "paper"`. It is part of the scope fingerprint. Old scopes have none
  and stay valid. `protected_repository_paths` keeps denying all of `.research`. The Paper folder sits outside `.research`, so `.research` stays fully
  denied and the grant is an ordinary write root for every provider.
- **Prompt.** The writing guidance from `paper_coach_task_contract` moves into
  the Work contract when `focus` is `paper`, rendered from the same resolved
  scope object.
- **Deleted:** `runs/tasks/coach.py`, the `paper_suggest` capability and
  provider profiles, `WritingSession`, `paper_coach_steers`, refresh rounds,
  Review/Apply modes, Coach card collection, the Coach card routes, and
  `PaperCoach.tsx`'s card UI. Paper Git's own `paper-git` cards are unchanged.
- **Upgrade.**
  - `CoachRequest` and the `paper_coach` kind move to a frozen legacy module
    that only parses stored rows for read-only replay.
  - A queued or running `paper_coach` task at upgrade is settled as
    interrupted with a notice.
  - Pending Coach card groups are rejected by a migration with a notice. Paper
    Git groups are untouched.
  - Old Coach sessions show read-only in the Coach pane's history.
  - The steers and writing-session tables stay readable for one release, then
    a later migration drops them.

## A turn's change list

- **Receipts.** Each folder → Paper apply during a turn writes a receipt keyed
  by `(operation_id, revision)` with the paths, that revision's own delta, the
  inverse operations, and guards (the text each region must still hold). The
  guarded inverse and its apply stay in `paper/changes.py`; the card collection
  around them is deleted. Receipts pin their before and
  after saved snapshots, so pruning keeps them while the chat exists.
- **List.** The change list composes each path's receipt deltas in order. Other
  members' edits are not receipts, so they never show as the Coach's. This
  composition is new code with its own tests.
- **Rows.** "N files changed", with +/− totals. Red rows sort first: held
  clashes (actions: Take the Coach's / Keep mine, through Paper Git's
  `resolve`) and skipped paths.
- **Undo turn** applies the receipts' inverses newest first. A guard that no
  longer holds stops that file and shows it as a red row.
- **Routes:** `GET …/paper/turns/{op}` (list), `GET …/paper/turns/{op}/files/{path}`
  (diff), `POST …/paper/turns/{op}/undo`, `POST …/paper/turns/{op}/held/{path}`
  with `{take: "coach"|"mine"}`.
- **Comments.** A diff line takes a shared comment thread with target kind
  `reply` and the turn's operation id, so readability follows the chat. A new
  anchor kind `diff` holds `path`, `side`, `line`, and `text`; it joins
  `CommentAnchor`, `render_anchor`, the web mirror, and the parity test. Send
  uses the chat's composer path.

## The Coach pane

The pane embeds the ordinary chat view (`AgentChatView`) for the selected
`focus = "paper"` chat, with History and New chat as today. The turn change list
renders inside the chat's turn, and the diff opens beside the chat.

## Other agents

- Prompts for Work, Experiment, and Auto-research name `<state root>/paper/`
  as the Paper, read-only. Auto-research gains the line it lacks today.

## Save to Paper

The artifact viewer gains **Save to Paper**: pick a folder, and the artifact's
current version becomes a Paper file through the ordinary upload path. It is a
human action; agents already write files directly.

## Invariants and specs that change

- AGENTS.md invariant 4: "The paper coach writes only its own changes folder.
  RCP turns those changes into paper change cards." becomes "A Paper-focused
  chat also writes the Paper folder; RCP mirrors it into Paper."
- providers-and-containment.md: the Paper coach row, `rcp_paper`, the Claude
  and OpenCode paper rules, snapshot staging.
- paper-artifacts-and-result-views.md: publishing, Writing coach, refresh
  rounds, "the coach has no direct paper authority".

## Checks

- Concurrency and safety: a Coach write racing an RCP write (exchange check);
  symlinks, hard links, special files; two Coach turns (the second queues);
  out-of-turn folder edits; ignored names; mass-delete guard; renames.
- Publication: startup, restore, import, and unpause reconcile; the badge;
  backup republish.
- Store and owner tests: Paper → folder in place; folder → Paper merge with
  base, overlap keeps live text, binary, create, delete, rename, refused path;
  RCP's own writes ignored; turn change list excludes other members' edits;
  Undo turn.
- Launch tests: `focus: "paper"` adds exactly the Paper write root; other
  launches stay read-only on the Paper folder; prompts per launch kind.
- Old `paper_coach` rows replay; pending Coach cards migrate.
- Served drive: a real Coach turn edits several Paper files while a member types
  in one of them; the chat shows the folded list with one red row; a diff
  comment goes back to the agent; Undo turn works; 390px.
