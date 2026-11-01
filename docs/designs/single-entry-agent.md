# Single-entry agent

Working design for PR #368. Its rules move into the owning specs, and this file
is deleted before merge. Settled decisions are not reopened here. Where code
forced a change, the section says so.

## Why

RCP is hard to use because a member must learn many surfaces: the space's
project cards, each project's views, Work chats, the Agents board, the Ask
buttons and the voice panel. The single-entry agent reduces that to one place
to talk to RCP. Everything else stays available but becomes optional.

It grows from the voice agent. Voice already acts as the member across
projects, but it is voice only, has no memory, and stops when the tab closes.

## Topology

- Each member has one agent per space. It runs in that space's RCP backend.
- Its threads are one **Space thread** plus one **project thread** per project
  the member belongs to. Each thread is private to the member and continues
  indefinitely.
- The space home has two views: project **Cards** and **Agent**. The Agent view
  has a left rail with the Space thread and every project thread, each with an
  unread dot. The rail folds the same way the Agents sidebar folds today.
- Inside a project, the Agents view opens the member's project thread full
  width. The team's board conversations stay reachable there.
- Removed: the voice launcher (`VoiceButton` in `web/src/App.tsx`), the desktop
  Ask button (`askAboutProject` in the project header) and `PhoneAskButton`
  (`web/src/projects/PhoneProjectChrome.tsx`). Voice moves inside the agent.
- Threads are created lazily, the first time the member lists them. Existing
  chats stay. There is no feature flag.

### Agent names

- `DEFAULT_AGENT_NAMES` in `src/rcp/runs/agent_names.py` is a short tuple of
  one-word names. It mixes cute names (Pip, Moss, Biscuit, Juniper) with
  literary ones (Puck, Ariel, Scout, Bartleby, Sancho, Alice, Orlando, Marvin).
  Pip is both. Every name fits `AGENT_NAME_MAX_CHARS`.
- Thread creation seeds `chat_display.title` with the first name, in list
  order, that the member's other threads in this space do not use. When all
  are used, it appends ` 2`, ` 3` and so on.
- The seed follows the consolidation precedent in the conversations spec
  "Agent names": the admission owner seeds it, so the agent cannot self-name.
  The member renames it under the existing rules (35 characters, no reset).

## Data model

Chat transcripts stay file-backed. A project thread is an ordinary
`project_chat` conversation plus one ownership row. The Space thread has no
project, so it gets its own task kind and transcript location.

**Reconciled with the decisions:** the decisions put messages in SQLite.
Transcripts are already append-only JSONL files with backup, transfer and
replay owners, so messages stay there. SQLite holds the thread index,
schedules, cards and handoffs.

### Space id in project-keyed columns

`graph_runs`, `chat_display`, `chat_reads`, `chat_browser_preferences`,
`browser_owners` and `questions` require a `project_id`. Space-thread rows put
the space's own `space_id` (from `space_identity`) there. It is a UUID4 and
never names a project.

**Why:** dropping `NOT NULL` from `graph_runs` needs a table rebuild of the
busiest table. Per-project listing queries already filter by a real project
id, so they never see these rows. Every owner that resolves a `ProjectService`
from a task row (startup recovery, Stop, Retry, sign-in-failure settlement,
watcher delivery) checks `kind == "space_chat"` first and uses the Space
thread owner instead.

### Tables

New storage migrations follow `per_member_provider_logins_v1` (65).

`agent_threads`: one row per member thread. A chat is private exactly when it
has a row here.

| Column | Type | Notes |
| --- | --- | --- |
| `thread_id` | TEXT PK | Equals the `chat_id`. |
| `user_id` | TEXT NOT NULL | Owning member. |
| `kind` | TEXT NOT NULL | `space` or `project`. |
| `project_id` | TEXT | NULL for `space`. |
| `created_at` | TEXT NOT NULL | |
| `closed_at`, `closed_reason` | TEXT | Set when the member loses the project or leaves the space. |

Unique indexes: `(user_id) WHERE kind='space'` and
`(user_id, project_id) WHERE kind='project'`.

`agent_schedules`:

| Column | Type | Notes |
| --- | --- | --- |
| `schedule_id` | TEXT PK | |
| `thread_id` | TEXT NOT NULL | Owning thread. |
| `user_id` | TEXT NOT NULL | |
| `created_operation_id` | TEXT NOT NULL | The member-message turn that created it. |
| `idempotency_key` | TEXT NOT NULL | Unique with `created_operation_id`. |
| `cron` | TEXT | Five fields. Exactly one of `cron`, `at_local`. |
| `at_local` | TEXT | One-shot local time `YYYY-MM-DDTHH:MM`. |
| `timezone` | TEXT NOT NULL | IANA name. |
| `note` | TEXT NOT NULL | What to do; shown in the list. |
| `state` | TEXT NOT NULL | `active`, `cancelled`, `disabled`, `done`. |
| `disabled_reason` | TEXT | `restore` or `access_lost`. |
| `next_fire_at` | TEXT | UTC. |
| `pending_since` | TEXT | Set when a fire is owed; cleared when a wake claims it. |
| `last_fired_at`, `created_at`, `cancelled_at`, `cancelled_by` | TEXT | `cancelled_by` is `member` or `agent`. |

`approval_cards`:

| Column | Type | Notes |
| --- | --- | --- |
| `card_id` | TEXT PK | |
| `question_id` | TEXT NOT NULL UNIQUE | The `questions` row that carries the card. |
| `user_id` | TEXT NOT NULL | Owner; the only resolver. |
| `thread_id`, `origin_operation_id` | TEXT NOT NULL | |
| `project_id` | TEXT NOT NULL | The project the action changes. |
| `action_json` | TEXT NOT NULL | The validated action. |
| `pinned_json` | TEXT NOT NULL | Live facts read at creation. |
| `pinned_sha256` | TEXT NOT NULL | Over action and pins, canonical JSON. |
| `state` | TEXT NOT NULL | `pending`, `running`, `approved`, `declined`, `stale`, `failed`, `expired`. |
| `expires_at`, `created_at`, `resolved_at` | TEXT | |
| `resolved_channel` | TEXT | `web`, `desktop` or `phone`; `slack` later. |
| `result_json` | TEXT | The human route's result or refusal. |

`agent_handoffs`:

| Column | Type | Notes |
| --- | --- | --- |
| `handoff_id` | TEXT PK | |
| `source_thread_id`, `source_operation_id` | TEXT NOT NULL | The Space thread turn. |
| `idempotency_key` | TEXT NOT NULL | Unique with `source_operation_id`. |
| `target_thread_id` | TEXT NOT NULL | The same member's project thread. |
| `target_operation_id` | TEXT | The turn it admitted. |
| `settled_at`, `notified_operation_id` | TEXT | The Space wake that reported it. |

`memory_migrations`: `project_id` PK, `migrated_at`, `lesson_count`.

### Task kinds and transcript

- Project-thread turns keep kind `project_chat` on main.
  `work_ask_authorized` and the Work handler already admit it.
- Space-thread turns use the new `AgentTaskKind` `space_chat` in
  `src/rcp/storage/models.py`. `work_ask_authorized` admits it too.
- Private project-thread transcripts use the existing chat writer with target
  `private-<user_id>`. The file is `.research/chat/private-<user_id>-<chat_id>.jsonl`.
  The prefix lets hidden-read hide one member's threads with one glob.
- The Space-thread transcript is
  `<data_dir>/agent-threads/<user_id>/<chat_id>.jsonl`, in the same line format.
  It stays outside backup, like voice records.
- `ChatMessage` in `src/rcp/service.py` gains optional `source`: `voice`,
  `handoff`, and later `slack`. Absent means the composer. It is excluded when
  absent, so old transcripts keep their bytes.
- `TaskTrigger` gains `handoff`. Schedule fires reuse `schedule`. The server
  binding, not the trigger, still marks consolidation.

## Capability

Every thread turn is a captured Work turn. Wakes and schedule fires are too.
The thread's member is the authorizer. Threads have no Discuss mode. Handing
work on is a workflow instruction, never a limit.

### Project thread

- Ordinary Work on the project's base and main target: files, the project's
  Patch stage, and its memory folder.
- Its stage is the chat's existing persistent stage.

### Space thread

- Runs on the backend's machine, in a persistent local stage
  `<data_dir>/run-stage/<stage>`.
- At admission RCP lists the member's projects whose base is the backend's
  machine and whose write admission is open. Each gets its own
  `ProjectWriteScope` with stage `projects/<project_id>/` inside the Space
  stage. Each scope keeps the per-scope rules in providers-and-containment
  ("Repository roots cannot be another project's checkout"). The provider gets
  the union of these scopes' roots.
- Other projects are read through `projects` and changed by `handoff` to the
  member's own project thread.
- Graph target: main, for every project. Branch work belongs to board
  conversations.

Stage layout:

| Path | Meaning |
| --- | --- |
| `outcome.json` | One turn outcome, under the ordinary Work contract. |
| `turns/<turn-id>/artifacts/` | As in chats. |
| `rcp` | Top-level command client: `projects`, `schedule`, `handoff`, `ask`. |
| `projects/<id>/rcp` | That project's Work command client, on its own mailbox. |
| `projects/<id>/patch.json` | That project's Patch. |
| `projects/<id>/watch.json` | That project's watchers; they wake the Space thread. |
| `history/` | Seeded context; see Context. |

- `validate` names its target by which client runs it. There is no project
  argument.
- Collection reads only `projects/<id>/patch.json` for admitted projects. A
  top-level `patch.json`, or one for another project, is a stray file: kept as
  a receipt and discarded.
- Apply runs per project in project-id order. Each is its own transition under
  that project's lock and live membership check. A rejection does not undo
  another project's Apply. The turn result carries
  `project_results: {project_id: applied | rejected | no_patch}`.
- Seen head is `graph_seen_heads(project_id, owner_id = thread_id, session, "main")`.
  That table is already keyed by project.
- Watcher rows use the real `project_id`, `origin_task_kind = "space_chat"` and
  the Space `chat_id`.

### Invariant wording

- **4b** becomes: "The task stage is the only graph-change channel. RCP
  collects the agent's Patch from its exact RCP-owned stage and nowhere else.
  A stage holds one Patch file per graph target; a Space-thread turn has one
  exact project folder per project it may write."
- **10b** becomes: "Only a captured Work turn has conversation graph authority.
  Message wording or a stray file cannot upgrade Discuss. Every agent-thread
  turn, wake and schedule fire is a captured Work turn authorized by the
  thread's member."
- **10g** becomes: "One episode, one graph target, one validated session and
  stage, one graceful Stop fence. Never silently fall back to a fresh session
  or main graph. A thread's fresh start is recorded and shown."
- **4**: replace the last sentence with: "The per-chat browser grant is human
  consent to a code-defined tool; a member's agent threads start with it on.
  Each project's memory folder is a code-defined write root, never a grant."

### Browser, MCP and sub-agents

- Browser is on by default for threads only. Thread creation writes
  `chat_browser_preferences.browser_requested = 1`. The member can turn it
  off. The conversations spec rule "off by default" gains "except agent
  threads". A lost or unavailable browser never blocks a turn; that is already
  true.
- No MCP servers, as in Work.
- Provider sub-agents are allowed, as in Work.

### Verbs

The Work verbs are `validate`, `launch` (compute), `episode`, `bulletin`,
`ask`, `name`, `graph_log` and `graph_diff` (`src/rcp/runs/questions.py`).
Thread turns get all of them except `name`. A Space thread gets them per
project through each project's client. `ask` stays on the top-level client.

**Changed from the decisions:**

- `apply` and `status` are orchestrator-only
  (`src/rcp/agents/auto_research_commands.py`). Threads get neither. A Work
  Patch applies at turn end.
- `spawn` and `message` create and message Auto-research workers bound to an
  orchestrator episode (`src/rcp/agents/command_protocol.py`). They cannot be
  reused for board conversations. The new `handoff` verb replaces them, and it
  only reaches the member's own project thread. Threads do not start or
  message board conversations.
- `lesson` is removed for every agent. Memory files replace it; see Memory.

New verbs. All arguments use `extra="forbid", strict=True`. Mutating forms take
the existing `idempotency_key`.

`projects` (both thread kinds; read-only):

| Field | Type | Rule |
| --- | --- | --- |
| `action` | `list` or `show` | |
| `project_id` | str | Required only for `show`. |

`list` returns, per member project: id, name, thread id and name, base
machine, `same_machine`, main head, pending Proposals, open questions and
active runs. `show` adds open Decisions and Blockers, recent transitions and
running episodes. Sizes are bounded in `limits.py`.

`schedule` (both thread kinds):

| Field | Type | Rule |
| --- | --- | --- |
| `action` | `add`, `list` or `cancel` | |
| `cron` | str | `add` only; exactly one of `cron`, `at`. |
| `at` | str | `add` only; `YYYY-MM-DDTHH:MM`. |
| `timezone` | str | `add` only; IANA name; defaults to the turn's timezone. |
| `note` | str | `add` only; 1 to `AGENT_SCHEDULE_NOTE_MAX_CHARS`. |
| `schedule_id` | str | `cancel` only. |

`add` is refused unless the turn's trigger is `human`, meaning a member
message. Code therefore enforces that only the member asks for a schedule. The
prompt adds: create one only when the member asked.

`handoff` (Space thread only):

| Field | Type | Rule |
| --- | --- | --- |
| `project_id` | str | A project of this member. |
| `message_file` | str | Safe workspace filename; at most `AGENT_HANDOFF_MAX_CHARS`. |

It appends a member message with `source: "handoff"` to the member's own thread
for that project. It is admitted as an ordinary member turn with trigger
`handoff`. If the thread is busy, the message waits as a queued human turn, as
in chats. The result names the target thread and operation.

**Not peer mail.** It addresses only the same member's own thread and carries
that member's authority. design.md "no peer or cross-episode mail" gains:
"A member's Space thread may hand that member's request to the member's own
project thread; that is not mail." The Bulletin rule becomes: agents of
different members, and board agents, share only through the Bulletin.

`ask` extension: `AskArguments` gains optional `action`, a discriminated union
on `kind`. With an action, `choices` must be empty and `multiple` false. RCP
supplies Approve and Decline. `question` is the agent's one-paragraph reason.
`action` is accepted only in thread turns.

| `kind` | Fields |
| --- | --- |
| `proposal` | `project_id`, `proposal_id`, `decision` (`approved` or `rejected`), `reason?` |
| `truth_scope` | `project_id`, `alias`, `writable` (bool) |
| `auto_research` | `project_id`, `invocation_ceiling` (≥ 1), `starting_instruction?`, `code_worktree?`, `worktree_repository?`, `branch_name?`, `provider?`, `model?`, `reasoning?` |
| `merge` | `project_id`, exactly one of `branch_id`, `episode_id` |

A project thread may name only its own project. The Space thread may name any
of the member's projects, on any base, because the human route runs in the
backend.

## Authority

### Approval cards

Protected acts wait for the member. Each card runs the existing human owner.

| `kind` | Human route | Pinned at creation, rechecked on tap |
| --- | --- | --- |
| `proposal` | `POST /api/projects/{id}/sync` with `proposals: [{proposal_id, decision, reason}]` and the current `base_revision` | Proposal pending, not stale, content digest |
| `truth_scope` | `PUT /api/projects/{id}/repositories/{alias}/writable` | Alias exists; current value differs |
| `auto_research` | `POST /api/projects/{id}/episodes` with `mode: "auto_research"` | Readiness, the resolved profile, the start refusal owner's verdict |
| `merge` | `POST /api/projects/{id}/graph-refs/{branch_id}/merge` or `/episodes/{episode_id}/merge` | Merge-preview source head and main head |

- **Creation.** `ask` with an action validates it, reads the pins, and writes
  one `questions` row (owner kind `chat`, the thread's chat) and one
  `approval_cards` row in one transaction. The question supplies the existing
  single **Needs you** push and the ask polling. A Space-thread question uses
  the space id as its `project_id`.
- **Resolution.** `POST /api/approval-cards/{card_id}` takes
  `{decision: "approve" | "decline", pinned_sha256}`. Only the owner's
  authenticated session may call it, on any signed-in channel.
- **Single use.** One `UPDATE … SET state='running' WHERE state='pending' AND expires_at > now`
  claims the card. A second tap finds it taken.
- **Recheck.** On Approve the server recomputes the pins. If the client's hash
  or any live pin differs, the card becomes `stale` and nothing runs.
  Otherwise it calls the route's service function in-process as the owner. A
  merge passes a UUID derived from `card_id` as `request_id`, so a retry is
  idempotent. This mirrors voice's confirm card, which rereads page state and
  runs nothing if a pin changed.
- **Expiry.** `APPROVAL_CARD_TTL_HOURS` (72) in `limits.py`. On expiry the
  question is dismissed and the card shows Expired.
- **Answer.** Settlement answers the question with `approved`, `declined`,
  `stale`, `failed: <reason>` or `expired`. The existing answer delivery steers
  the running turn or admits one follow-up, so a resolved card wakes the
  thread. The ordinary question-answer route refuses questions that carry a
  card (409 `approval_card`).
- **Not a page tool.** Neither the voice catalog nor WebMCP offers the route.
  A test asserts both catalogs lack it. Invariant 3 already treats a page agent
  as the member; protected judgment stays outside the catalog.
- **Slack later.** A Slack-signed interaction from a Slack user the member
  linked inside RCP.

### Member acts via the agent

Ordinary acts run without a tap: Patches, Experiment starts, compute, Bulletin
posts, schedules and handoffs. They already record the turn's operation and its
authorizer. Projections that show a turn's authorizer gain
`via_agent: {thread_id, name} | null`, derived from the origin operation's
`agent_threads` row. The UI shows "Member, via Pip" with a link to the turn.
It appears in Patch history rows, Runs **Started from**, Bulletin authors and
Inbox rows. Nothing new is stored in a Patch, so replay is unchanged.

Replies link the nodes, runs, chats and Proposals they mention. The backend
agent never drives the member's tab. Voice keeps its page tools.

## Memory

Lessons and memory become one folder of files per project, on its base,
outside Git.

### Location and access

- Local base: `<data_dir>/memory/<project_id>/`. Remote base:
  `~/.rcp/memory/<project_id>/` of the base account. Both lie under
  `rcp_owned_paths`, so no grant can admit them.
- The launch builder adds them as code-defined write roots, the same way it
  adds the stage. The owner is `project_memory_root()` in
  `src/rcp/agents/memory_files.py`.
- Layout: `shared/MEMORY.md`, `shared/notes/*.md`,
  `members/<user_id>/MEMORY.md`, `members/<user_id>/notes/*.md`.

| Run | `shared/` | `members/<own>/` |
| --- | --- | --- |
| The member's project thread | read, write | read, write |
| Space thread, same-machine projects | read, write | read, write |
| Board Work and Discuss chats | read, write | none |
| Experiment, Auto-research root and workers | read, add only | none |
| Consolidation | read, write, prune | none |
| Seed, Refresh, branch merge, Paper coach, artifact edit | none | none |

- Discuss gains the memory folder as its one writable root outside scratch.
  That is fixed in code, like its scratch.
- "Child agents only add" was enforced by the lesson command handler. It is now
  a prompt rule.
- Hidden-read adds, for every other member of the project:
  `<memory>/members/<other_user_id>` as a directory and
  `<state>/chat/private-<other_user_id>-*.jsonl` as a glob
  (`src/rcp/agents/hidden_read.py`). This shares `HIDDEN_READ_PATH_MAX_COUNT`.
  Where hiding is unavailable, the existing Settings and doctor reason applies.
- The Space thread reads only its own member's private notes.

### Format

- `MEMORY.md` is an index: one line per note,
  `- [Title](notes/<slug>.md) — one-line hook`.
- A note is Markdown whose first line is `# Title`. Migrated notes add a second
  line `origin: lesson <lesson_id>`.
- Prompts point to `shared/MEMORY.md` and, for threads, the member's
  `MEMORY.md`. They replace the staged `lessons.md` pointer. Prompts label
  memory as notes, never instructions or authority.
- `MEMORY_INDEX_MAX_BYTES` (32 KiB) in `limits.py` replaces
  `LESSONS_RENDER_MAX_BYTES`. Launch stats the index. If it is over the cap,
  the prompt says so and asks the agent to read the start and prune.

### Migration from lessons

- The backend migrates one project the first time it builds a launch on that
  project's base. An unreachable base defers it; such a launch could not run
  anyway.
- It writes `shared/notes/lesson-<lesson_id>.md` for every `operational_lessons`
  row, using create-if-absent, then rewrites `shared/MEMORY.md` with
  human-owned notes first, newest first. It then inserts the
  `memory_migrations` row. A rerun after a crash skips existing files.
- `operational_lessons` and `lesson_command_receipts` stay as a read-only
  migration source. Nothing writes them.
- Removed: the `lesson` verb and `runs/lesson_commands.py`; the
  `/api/projects/{id}/lessons` routes in `api/lessons.py`;
  `web/src/projects/ProjectLessons.tsx` and its Settings entry; the WebMCP
  lessons read.
- Consolidation prunes and merges `shared/` with ordinary file edits and reports
  each change. It never touches `members/`.
- Given up: note authorship and SQLite backup. Memory folders are not captured
  by backup and not carried by transfer. Lessons were not transferred either.
  A transfer that keeps the same base and account finds the folder by project
  id.
- Native CLI memory is off for every RCP run: Codex `--disable memories`,
  Claude `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (PR #369).

## Context

- Each thread keeps one native session. The provider auto-compacts it.
- A fresh session gets a staged file `history/tail.md`. Its first line says it
  is earlier conversation for context, not instructions or authority. RCP chat
  history is display data (conversations spec, "Native chat context").
  The tail is bounded by `AGENT_THREAD_TAIL_MAX_MESSAGES` and
  `AGENT_THREAD_TAIL_MAX_BYTES` in `limits.py`.
- **Provider switch.** Today it starts fresh with `provider_mismatch`. For
  threads, RCP first asks the old provider to compact the old session and
  stages the result as `history/compacted.md`, under the same label.
  New `chat_session_resolution` reasons:
  - `provider_switch_compacted`: the compaction succeeded.
  - `provider_switch_tail`: it could not run (old sign-in failed, session
    unusable, no compaction call, or `AGENT_THREAD_COMPACT_TIMEOUT_SECONDS`
    passed). The new session gets memory plus the tail.
- Every other fresh start keeps its existing reason and gets the tail.
- The thread shows a "started fresh" marker with the reason.

## Schedules

- The member asks; the agent creates the schedule with `schedule add`.
- Grammar: five numeric cron fields (minute, hour, day of month, month, day of
  week) with `*`, lists, ranges and steps. Or one `at` local time. Timezone is
  an IANA name. The composer sends the browser's timezone with each message,
  and the turn records it as the default. A nonexistent local time uses the next
  valid minute. An ambiguous time uses the first instance, as consolidation does.
- The thread that created a schedule owns it. The thread shows a schedules list
  with Cancel.
- `AGENT_SCHEDULES_MAX_ACTIVE` per thread caps standing cost. It does not limit
  wakes.
- **Firing.** One backend poller runs every `AGENT_SCHEDULE_POLL_SECONDS`. It
  is wired in `api/app.py` beside the consolidation scheduler. A due schedule
  sets `pending_since` and advances `next_fire_at` in one transaction. A
  one-shot schedule becomes `done`.
- **Fire once, combined.** When a thread has pending schedules and no active,
  paused or queued turn, the poller claims all of them and admits one wake turn.
  The turn lists every claimed note and how many fires it missed. Downtime and
  busy periods therefore yield one wake.
- **Restore.** `restore_detachment.py` sets every active schedule to `disabled`
  with reason `restore`, as it does for consolidation. The member re-enables
  each one from the list.

## Wakes

A thread wakes on:

- a member message (composer, voice or handoff);
- its own `watch.json`, through existing conversation watchers. Experiments it
  starts arm the existing node-status watcher;
- a handoff settling: when the target turn settles, RCP admits one Space wake
  claimed through `agent_handoffs.notified_operation_id`;
- a resolved card, through question answer delivery;
- a schedule fire.

There is no wake limit. Deferred wakes follow chat rules.

Unseen thread messages do not push today. A new notification kind,
`agent_thread`, goes only to the thread's owner through
`enqueue_notification(recipient_user_id=…)`. It fires when a turn settles and
the member has not read past it (`chat_reads`). A toggle defaults to on. Space-
thread items use a space-level pass in `notifications.py`.

## Voice in the agent

- The voice button sits in the thread header. Voice binds to the thread that
  is open when it starts, and the panel names that thread.
- `POST /api/voice/sessions` takes `thread_id` instead of `resume_id`. The
  backend seeds `session.input` from that thread's tail, within the existing
  128-message and token bounds.
- Voice entries become thread messages with `source: "voice"`. They go through
  `POST /api/agent-threads/{thread_id}/voice-entries` and are display-only.
- The new tool `rcp_thread_send({text})` hands work to the backend agent as a
  member Work message, under the existing tap confirmation. If a turn is active
  and takes input, the text steers it. Otherwise voice gets `thread_busy` and
  says so.
- `rcp_thread_memory()` returns the bound project's `shared/MEMORY.md` and the
  member's own `MEMORY.md`, bounded. Voice writes memory by asking the backend
  agent.
- Voice runs on OpenAI under the member's own key, so the member's private
  notes and thread text reach that service. Settings states this once on the
  voice card.
- Voice session records end. New sessions write none. Existing records expire
  under the current 30-day rule. The record list, Resume and the record routes
  are removed.

## Access changes

- **Losing a project.** The existing membership fence stops new continuations.
  The thread gets `closed_at` and leaves the rail. Its schedules become
  `disabled` with `access_lost`. Pending cards expire. The transcript and
  `members/<id>/` stay in place and stay hidden from others. Rejoining reopens
  the same thread.
- **Leaving the space.** The Space-thread transcript, schedules and cards are
  deleted, as voice records are today.
- **Transfer.** The transfer record bundle classifies the four new tables as
  space-local. Private transcripts (`private-*`) are excluded from the archive.
  A Patch whose `task_id` names a `space_chat` operation imports as history.

## Whole-app font

- Add `@fontsource/figtree` and `@fontsource/jetbrains-mono` to
  `web/package.json` and import them in `web/src/main.tsx`.
- In `web/src/styles/01-base.css` and `web/src/themes/aqua.css`, `--ui` and
  `--display` become Figtree, and `--mono` becomes JetBrains Mono. Sizes stay.
- Vite emits the `.woff2` files into `web/dist`. KaTeX fonts already ship this
  way. `packaging/rcp_backend.spec` copies all of `web/dist`. The Tauri config
  sets no CSP, and the app page sends none. Artifact previews keep their own
  CSP. No packaging or native change is needed.

## Alternatives considered

- **Several PRs.** Rejected by the human: one PR, #368.
- **Separate agents per project, no Space thread.** The member would still hop
  between projects to find the right one.
- **Router-only Space thread.** Simple changes on the member's own machine
  would need an extra hop.
- **Read-only Space thread.** The same cost as router-only.
- **SQLite memory (keep lessons).** Agents edit files natively, and one
  mechanism serves memory and lessons. This gives up authorship and backup.
- **RCP-managed summaries.** Provider compaction already exists and costs
  nothing extra. RCP compacts only on a provider switch.
- **Messages in SQLite.** Transcripts already have file owners for replay,
  backup and transfer.
- **Reusing `spawn` and `message`.** They create Auto-research workers.
- **Space thread writing remote projects.** A scope cannot hold another
  machine's roots, so these go through `handoff`.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Cost: wakes have no limit, schedules recur, and the Space thread carries many projects. | Schedules only on a member message, a per-thread cap, a visible list with Cancel, every wake shown in the thread, Stop, per-member usage in `agent_usage`. |
| Injection: browser on by default; memory persists across agents. | Protected acts need a card; card resolution is not a tool; secret hiding unchanged; memory labelled as data; consolidation prunes; the member can read notes by asking. |
| Privacy between members. | Private threads and notes are hidden by API filtering and hidden-read. This is cooperative, not a security boundary: members are one lab, and the service account can read everything. Voice sends private text to the member's own OpenAI key. |
| Changes across projects are not atomic. | Per-project Apply with visible `project_results`; `outcome.json` says `partial`; the graph has no cross-project references. |
| `space_id` in `project_id` columns. | One kind check in each task-resolving owner; tests below cover each one. |

## Build phases

One PR, in this order. Each phase ends green.

1. **Storage and contracts.** Tables, `space_chat`, `ChatMessage.source`,
   `handoff` trigger, limits, command argument models, web types.
2. **Memory files.** Root owner, write roots, hidden-read entries, migration,
   prompt pointers, consolidation workflow, lesson removal.
3. **Project threads.** Lazy creation, privacy filtering, default names,
   browser default, session reasons and seeding.
4. **Space thread.** Stage layout, per-project scopes and clients, collection
   and partial Apply, `projects`, `handoff`, wakes, task-owner kind checks.
5. **Approval cards.** `ask` action, resolution route, recheck, expiry,
   question refusal, push.
6. **Schedules.** Grammar, poller, restore detachment.
7. **Web.** Agent view and rail, thread view, removals, voice in the agent,
   fonts.
8. **Docs.** Move rules into specs, amend AGENTS.md and design.md, delete this
   file.

## Files

| Module | Files |
| --- | --- |
| Storage | `src/rcp/storage/base.py` (migrations), `src/rcp/storage/models.py` (`space_chat`), new `src/rcp/storage/agent_threads.py`, `src/rcp/storage/lessons.py` (reader only), `src/rcp/storage/restore_detachment.py`, `src/rcp/storage/notifications.py` |
| Contracts | `src/rcp/service.py` (`ChatMessage.source`, `TaskTrigger`), `src/rcp/limits.py`, `src/rcp/agents/command_protocol.py`, `src/rcp/agents/staged_command_client.py`, `web/src/core/types.ts` |
| Agents | new `src/rcp/agents/memory_files.py`, `src/rcp/agents/write_scope.py`, `src/rcp/agents/hidden_read.py`, `src/rcp/agents/prompts.py`, `src/rcp/agents/context.py`, `src/rcp/agents/continuation_prompt.py` |
| Runs | new `src/rcp/runs/agent_threads.py`, new `src/rcp/runs/approval_cards.py`, new `src/rcp/runs/agent_schedules.py`, `src/rcp/runs/questions.py`, `src/rcp/runs/chat.py`, `src/rcp/runs/chat_admission.py`, `src/rcp/runs/agent_names.py`, `src/rcp/runs/consolidation.py`; delete `src/rcp/runs/lesson_commands.py`, `src/rcp/runs/lessons.py` |
| Background and notices | `src/rcp/background.py`, `src/rcp/notifications.py`, `src/rcp/consolidation.py`, `src/rcp/skills/workflows/graph-consolidation/WORKFLOW.md` |
| API | new `src/rcp/api/agent_threads.py`, `src/rcp/api/app.py`, `src/rcp/api/chats.py`, `src/rcp/api/questions.py`, `src/rcp/api/voice.py`, `src/rcp/voice.py`; delete `src/rcp/api/lessons.py` |
| Transfer | `src/rcp/transfer/records.py` |
| Web | new Agent view under `web/src/agents/`, `web/src/App.tsx`, `web/src/projects/PhoneProjectChrome.tsx`, `web/src/projects/ProjectSettings.tsx`, delete `web/src/projects/ProjectLessons.tsx`, `web/src/voice/*`, `web/src/webmcp/reads.ts`, `web/src/main.tsx`, `web/src/styles/01-base.css`, `web/src/themes/aqua.css`, `web/package.json` |
| Docs | `AGENTS.md` (4, 4b, 10b, 10g), `docs/design.md`, `docs/specs/conversations-episodes-and-watchers.md`, `docs/specs/providers-and-containment.md`, `docs/specs/graph-consolidation-and-lessons.md`, `docs/specs/api-web-and-desktop-projections.md`, `docs/specs/interface-and-visual-design.md`, `docs/specs/authority-and-proposals.md` |

## Test plan

Focused local checks follow AGENTS.md Verification. The full suite runs in PR CI.

- **Replay and migration.** Old transcripts without `source` keep their bytes.
  Storage migrations run on a copy of a real old data directory. Lesson
  migration is idempotent across a crash between files and the marker row. An
  old-writer workflow regression covers a lesson written by the old command
  and then migrated. A Space-thread Patch replays like any Work Patch.
- **Space thread.** Two same-machine projects: one Apply lands and one is
  rejected; `project_results` and `outcome.json` agree. A stray top-level
  `patch.json` is discarded. Seen head per project. A watcher in
  `projects/<id>/` wakes the Space thread. Startup recovery, Stop, Retry and
  sign-in failure on a `space_chat` row.
- **Authority.** Card single use under two concurrent taps. Stale pins run
  nothing. Expiry. Only the owner resolves. The question route refuses card
  questions. Neither the voice catalog nor WebMCP lists the card route.
  `schedule add` is refused in wake and schedule turns. `handoff` refuses
  another member's thread.
- **Privacy.** Another member gets 404 on a private thread. Hidden-read
  entries for other members' notes and transcripts. Transfer excludes
  `private-*` transcripts.
- **Schedules.** Missed fires during downtime yield one combined wake. DST
  edges. Restore disables schedules.
- **Served-app journey** on disposable data, with network, console and server
  logs inspected: open Agent; Space thread edits a same-machine project; hand
  off to a remote-base project; approve a Proposal card from the phone layout;
  add and cancel a schedule; switch provider and see the fresh-start marker.
- **390px.** Agent view, folded rail, card, schedules list and voice panel meet
  the phone, no-commentary and smooth-motion rules.
- **Live Codex turn** on throwaway data: one Space-thread turn and one
  project-thread turn, each writing memory and a Patch. Confirm native memory
  stays off.
- **Desktop.** No native code changes. Still load the packaged frontend in the
  desktop app to confirm the fonts render in WebKit.

## Open before build

- Probe each provider CLI's headless or app-server compaction call, for
  `provider_switch_compacted`.
- Mockups for the approval card, the schedules list and voice inside a thread,
  if not yet shown.
