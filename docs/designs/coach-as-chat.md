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
the end of each Coach turn instead of watched (decided with the human).

A Paper-focused chat always runs on the machine that holds the state root.
Asking it to run elsewhere is refused with the reason. Other chats that run on
another machine read the Paper only when that machine is the state host, as
today.

## The live Paper folder

`.research/paper` becomes the Paper's file view. SQLite stays the truth for
history, co-editing, GitHub sync, and backup. The folder is kept equal to it in
both directions. This extends two existing pieces instead of adding new ones:
the Paper publisher (`LivePaper._publish` and the `paper_publication` table)
and Paper Git's merge (`paper/git_merge.py` `merge_trees` with the apply loop in
`PaperGit._merge`).

**What RCP last wrote.** A new table `paper_folder_files(project_id, path,
sha256, revision)` records, per file, the bytes RCP last wrote to the folder.
It is the merge base for folder edits and survives restarts.

**Paper → folder.** The publisher stops swapping whole trees. After each
committed revision it writes only the changed paths, per file, through a
sibling temp file and a rename, then records the file in `paper_folder_files`.
Writes are coalesced per file for 300 ms so typing does not rewrite a file per
keystroke. `hold`, failure backoff, and `pending_since` keep their meaning;
`published_revision` becomes "every changed file written".
`publish_paper_tree` and `_select_paper_tree` are deleted, and the remote state
transport gains a per-file write with the same temp-and-rename.

**Folder first.** Before RCP overwrites a file, it compares the file's bytes
with `paper_folder_files`. If they differ, someone (the Coach, or a person in
an editor) changed the file since RCP wrote it, so that change is read into
Paper first, under the same per-project lock. RCP never overwrites an unread
edit.

**Folder → Paper.** On local state roots the owner watches the folder with
`watchfiles` (declared as a direct dependency) for as long as the project is
open, not only during Coach turns, so any edit to the folder reaches Paper. On
SSH state roots RCP scans at the end of each Coach turn, at the folder-first
check, and at backend start.
- A changed path becomes one call to `merge_trees(base, theirs, ours)`: base =
  the bytes in `paper_folder_files`, theirs = the folder, ours = live Paper.
- Clean results apply through Paper Git's apply loop as a Paper revision whose
  actor is `coach:<operation id>` while a Coach turn runs, else `folder`.
- An overlap keeps the live text (decision 6). The folder's side is held with
  Paper Git's held-file mechanism under the task id of the turn. That held side
  is the red row.
- Refused paths and limits are skipped with Paper Git's reasons and show as red
  rows.
- **Startup.** Before the first write, a full scan reads any changed files into
  Paper. A restart never loses folder edits.

## The Coach chat

- **Mode.** A Coach chat is an ordinary Work chat; there is one chat mode.
- **Focus.** A chat has `focus`, `null` or `"paper"`, fixed when the chat is
  created and stored on the chat record. Requests carry it into `RunRequest`
  and `AgentDispatchScope`, so recovery and frozen authority keep it. The Coach
  pane lists the member's chats with `focus = "paper"`. The policy version of
  the Work task contract bumps.
- **Write scope.** `ProjectWriteScope` gains `paper_root`, set only for
  `focus = "paper"`. It is part of the scope fingerprint. Old scopes have none
  and stay valid. `protected_repository_paths` keeps denying `.research` and
  lists its children except `paper/`, so the grant never opens graph or chat
  state. Per provider:
  - **Claude:** deny each `.research` child except `paper/`, and allow
    `paper/`; deny still beats allow.
  - **Codex:** `paper/` joins the writable roots and the other `.research`
    children stay `read`. A profile test pins that precedence.
  - **OpenCode:** a granted root inside a deny is already re-allowed.
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
  by `(operation_id, revision)` with the paths, the inverse operations, and
  guards (the text each region must still hold). Receipts pin their before and
  after saved snapshots, so pruning keeps them while the chat exists.
- **List.** The change list is built from the turn's receipts: per path, the
  text before the turn's first receipt and after its last. Other members' edits
  are not receipts, so they never show as the Coach's.
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

- Prompts for Work, Experiment, and Auto-research name `.research/paper` as the
  live Paper, read-only. Auto-research gains the line it lacks today.
- The folder is current within a second, so "not authoritative" is dropped from
  the prompt.

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

- Store and owner tests: Paper → folder in place; folder → Paper merge with
  base, overlap keeps live text, binary, create, delete, rename, refused path;
  RCP's own writes ignored; turn change list excludes other members' edits;
  Undo turn.
- Launch tests: `focus: "paper"` adds exactly the Paper write root; other
  launches stay read-only on `.research/paper`; prompts per launch kind.
- Old `paper_coach` rows replay; pending Coach cards migrate.
- Served drive: a real Coach turn edits several Paper files while a member types
  in one of them; the chat shows the folded list with one red row; a diff
  comment goes back to the agent; Undo turn works; 390px.
