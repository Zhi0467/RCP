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

## The live Paper folder

`.research/paper` stops being a published output and becomes the Paper's file
view. SQLite stays the truth for history, co-editing, GitHub sync, and backup.
The folder is kept equal to it in both directions.

**Paper → folder.** Every committed Paper revision writes its changed files into
the folder in place, one file at a time: write to a sibling temp file, then
rename. Deletes and renames apply as file operations. The whole-tree swap
(`publish_paper_tree`, `_select_paper_tree`) is removed. Lag is the commit plus
one write, well under a second.

**Folder → Paper.** The owner watches the folder (`watchfiles`, already a
dependency) while any Coach turn runs in the project, and at turn end runs one
full scan.
- For each changed path it reads the bytes and compares them with the Paper
  file. A difference becomes a Paper revision by the actor `coach:<operation>`.
- Text is merged three-way: base = the text RCP last wrote to that file,
  theirs = the file now, ours = the live Paper text. Non-overlapping edits
  merge. An overlap keeps the live text (decision 6) and records a clash for
  the turn.
- Binary files, creates, deletes, and renames apply whole. A delete or rename
  that clashes with a live edit keeps the live file and records a clash.
- Paths Paper refuses (its path rules and limits) are skipped and recorded as
  failed rows.
- Writes that RCP made itself (Paper → folder) are recognized by content and
  ignored.

**Ownership.** Only the owning RCP process writes the folder. The Coach's file
tools get `.research/paper` as an exact write root. Every other launch keeps it
read-only (today's `.research` protection in `agents/write_scope.py`).

**Builds.** Compile and preview read from SQLite snapshots, never the folder, so
a half-written folder never reaches a build. (Today's "never see half a
published tree" guarantee moves to the compile path.)

**SSH state roots.** A personal project whose state lives on an SSH host keeps
its folder there. The owner applies Paper → folder over the existing state
transport and watches by polling the remote folder's mtimes during a Coach turn.
Open question for review: the cost of that poll versus restricting Coach Paper
writes to local state roots in the first version.

## The Coach chat

- **Where.** The Coach pane hosts one ordinary project chat per member, as the
  Coach History and New chat do today. Its rows are `project_chat` tasks with
  `focus: "paper"`.
- **Capability.** `focus: "paper"` adds `.research/paper` to the turn's resolved
  write roots in the one place write scope is resolved. Capability stays code,
  not configuration (invariant 4).
- **Prompt.** The writing guidance from `paper_coach_task_contract` moves into
  the chat prompt when `focus` is `paper`, rendered from the same resolved scope
  object. The policy version bumps.
- **Deleted:** `paper_coach` task kind and its admission branches, `CoachRequest`,
  `runs/tasks/coach.py`, the `paper_suggest` capability and provider profiles,
  `WritingSession`, `paper_coach_steers`, refresh rounds, Review/Apply modes,
  `paper/changes.py` card collection for Coach turns, Coach card routes, and the
  Coach card UI. GitHub sync's `paper-git` cards are a separate design (the
  remote-truth Paper Git PR) and are untouched here.
- **Old rows.** Stored `paper_coach` tasks replay read-only as history.
  Pending Coach cards are rejected by a one-time migration with a notice, since
  their mode no longer exists.

## A turn's change list

- A turn's Paper revisions carry its operation id. The change list is the diff
  of each touched file between the revision before the turn's first write and
  after its last, restricted to the turn's own revisions. Edits other members
  typed during the turn are not shown as the Coach's.
- **Folded row:** "N files changed", with +/− totals. Red rows (clashes, skipped
  paths) sort first. A row opens the file's diff beside the chat.
- **Undo turn** reverts the turn's revisions with the existing guarded inverse
  (live text must still hold the Coach's text in those regions).
- **Comments.** A diff line takes a shared comment thread with a new `diff`
  anchor (`path`, `side`, `line`, `text`, `turn`). Send goes to the same chat
  through the composer path of the shared comments design.

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
