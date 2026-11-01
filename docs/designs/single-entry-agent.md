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
  Every name fits `AGENT_NAME_MAX_CHARS`.
- Thread creation seeds `chat_display.title` with the first name, in list
  order, that the member's other threads in this space do not use. When all
  are used, it appends ` 2`, ` 3` and so on.
- The seed follows the consolidation precedent in the conversations spec
  "Agent names": the admission owner seeds it, so the agent cannot self-name.
  The member renames it under the existing rules (35 characters, no reset).

## Two thread kinds

**The Space thread owns no project lifecycle.** It reads across the space. It
writes files and graph Patches directly on projects whose base is the backend's
machine. Everything with a project lifecycle is created in the member's own
project thread for that project, through `handoff`: Experiments, compute
`launch`, watch files and approval cards. Code enforces this through the
verbs each client gets.

So questions, watchers, episodes and notifications always carry a real project
id. Only task and chat bookkeeping rows carry the space id; see Data model.

| | Project thread | Space thread |
| --- | --- | --- |
| Task kind | `project_chat` | new `space_chat` |
| Runs on | The project's base | The backend's machine |
| Graph target | Main | Main of each admitted project |
| Writes | Repository roots, Patch stage, memory | Same, per same-machine project |
| Lifecycle (Experiments, `launch`, watchers, cards) | Yes | No; hands off |
| `ask` | Plain and with an action | With an action only; becomes a handoff |
| Push | `agent_thread` and Needs you | None in v1 |

Every thread turn is a captured Work turn, including wakes and schedule fires.
The thread's member is the authorizer. Threads have no Discuss mode.

## Data model

Transcripts stay file-backed. A project thread is an ordinary `project_chat`
conversation plus one ownership row.

**Reconciled with the decisions:** the decisions put messages in SQLite.
Transcripts are already append-only JSONL files with backup, transfer and
replay owners, so messages stay there. SQLite holds the thread index,
schedules, cards and handoffs.

### Space id in bookkeeping rows

Space-thread rows put the space's own `space_id` (from `space_identity`, a
UUID4 that never names a project) in these project-keyed columns:

- `graph_runs` and `agent_usage`
- `chat_display`, `chat_reads`, `chat_pins`
- `chat_browser_preferences`, `browser_owners`
- `chat_session_contexts`
- `artifacts`, `artifact_operations` (turn artifacts)
- `client_requests` (voice idempotency keys)

The resource-version triggers on these tables bump under the space id. The
Agent rail polls that scope.

`questions`, `watchers`, `episodes` and every `notification_*` table never get
a space-id row. A Space thread has no plain `ask`, no watch files and no
episodes, so nothing creates one.

One resolver, `task_owner(store, task)` in `src/rcp/runs/agent_threads.py`,
returns the project service or the Space-thread owner for a task row. Every
owner that resolves a `ProjectService` from a task row uses it: startup
recovery, Stop, Pause, Resume, Retry, steering, sign-in-failure settlement and
usage projection.

**Why:** dropping `NOT NULL` from `graph_runs.project_id` needs a table rebuild
of the busiest table. Per-project listing queries filter by a real id, so they
never see these rows.

### Tables

| Version | Migration name | Table |
| --- | --- | --- |
| 66 | `agent_threads_v1` | `agent_threads` |
| 67 | `agent_schedules_v1` | `agent_schedules` |
| 68 | `approval_cards_v1` | `approval_cards` |
| 69 | `agent_handoffs_v1` | `agent_handoffs` |
| 70 | `space_turn_project_bindings_v1` | `space_turn_project_bindings` |
| 71 | `memory_migrations_v1` | `memory_migrations` |

`agent_threads` is the authority on privacy: a chat is private exactly when it
has a row here.

| Column | Type | Notes |
| --- | --- | --- |
| `thread_id` | TEXT PK | Equals the `chat_id`. |
| `user_id` | TEXT NOT NULL | Owning member. |
| `kind` | TEXT NOT NULL | `space` or `project`. |
| `project_id` | TEXT | NULL for `space`. |
| `created_at` | TEXT NOT NULL | |
| `closed_at`, `closed_reason` | TEXT | Set when the member loses the project. |

Unique indexes: `(user_id) WHERE kind='space'` and
`(user_id, project_id) WHERE kind='project'`.

`agent_schedules`:

| Column | Type | Notes |
| --- | --- | --- |
| `schedule_id` | TEXT PK | |
| `thread_id` | TEXT NOT NULL | Owning thread. |
| `created_operation_id`, `idempotency_key` | TEXT NOT NULL | Unique together. |
| `cron` | TEXT | Exactly one of `cron`, `at_local`. |
| `at_local` | TEXT | `YYYY-MM-DDTHH:MM`. |
| `timezone` | TEXT NOT NULL | IANA name. |
| `note` | TEXT NOT NULL | |
| `state` | TEXT NOT NULL | `active`, `cancelled`, `disabled`, `done`. |
| `disabled_reason` | TEXT | `restore` or `access_lost`. |
| `next_fire_at`, `pending_since`, `last_fired_at`, `created_at` | TEXT | UTC. |

`approval_cards`. The card's outcome is its question's answer.

| Column | Type | Notes |
| --- | --- | --- |
| `question_id` | TEXT PK | The `questions` row; its chat names the owner. |
| `action_json` | TEXT NOT NULL | The validated action. |
| `pinned_json`, `pinned_sha256` | TEXT NOT NULL | Live facts at creation, and the hash over action and pins. |
| `expires_at` | TEXT NOT NULL | |
| `running_since` | TEXT | The single-use claim. |
| `result_json` | TEXT | The human route's result or refusal. |

`agent_handoffs`:

| Column | Type | Notes |
| --- | --- | --- |
| `handoff_id` | TEXT PK | |
| `source_thread_id`, `source_operation_id` | TEXT NOT NULL | The Space turn. |
| `idempotency_key` | TEXT NOT NULL | Unique with `source_operation_id`. |
| `target_thread_id` | TEXT NOT NULL | The same member's project thread. |
| `message` | TEXT NOT NULL | |
| `created_at` | TEXT NOT NULL | |
| `target_operation_id` | TEXT | NULL while pending. |
| `settled_at`, `notified_operation_id` | TEXT | The Space wake that reported it. |

`space_turn_project_bindings`: `operation_id`, `project_id`,
`dispatch_authority_json`, `write_scope_fingerprint`, `graph_target_json`,
primary key `(operation_id, project_id)`.

`memory_migrations`: `project_id` PK, `migrated_at`, `lesson_count`.

### Transcripts

- `ChatMessage` and the stored record in `src/rcp/service.py` gain optional
  `source`: `voice`, `handoff`, and later `slack`. Absent means the composer.
  It is excluded when absent, so old transcripts keep their bytes.
- Private project threads: the stored record gains `privateOwner` (the user
  id) on every line. `_canonical_chat_path` gains `private_owner`; with it the
  name is `private-<user_id>-<chat_id>.jsonl`. `_validate_stored_chat_record`
  compares the path using the record's `privateOwner`. The prefix serves only
  the hidden-read glob; `agent_threads` decides privacy.
- The Space transcript is `<data_dir>/agent-threads/<user_id>/<chat_id>.jsonl`.
  Its records use the same model with `chatScope: "space"`, no `nodeId` and no
  `graphTarget`. A separate Space reader accepts only that scope. The project
  reader keeps refusing it, because `_canonical_chat_path` knows only `node`
  and `project`.
- No new trigger. A handoff turn is trigger `human` with `source: "handoff"`,
  so steering and admission accept it unchanged. Schedule fires reuse
  `schedule`; the server binding, not the trigger, still marks consolidation.

## Capability

### Project thread

- Ordinary Work on the project's base and main target: files, the Patch stage,
  and the memory folder. Its stage is the chat's existing persistent stage.
- Verbs: the Work verbs from `src/rcp/runs/questions.py` (`validate`, `launch`,
  `episode`, `bulletin`, `ask`, `graph_log`, `graph_diff`), plus `projects` and
  `schedule`. Not `name`.

### Space thread

- Runs in a persistent local stage `<data_dir>/run-stage/<stage>` on the
  backend's machine.
- **Composite scope.** At admission RCP lists the member's projects whose base
  is the backend's machine and whose write admission is open. Each gets its own
  `ProjectWriteScope` with stage `projects/<project_id>/`. Each keeps the
  per-scope rule that a root cannot be another project's checkout. The provider
  gets the union of their roots plus the Space stage's top-level files. The
  stored `write_scope_fingerprint` is the SHA-256 of the sorted per-project
  fingerprints.
- **No same-machine projects.** The turn runs with no write scope. Its
  fingerprint is NULL, and it can still read, `handoff` and `schedule`.
- **Changed project set.** If the admitted set differs from the previous
  turn's, RCP keeps the session and records the change on the turn. This is not
  a 10g change: the thread is the binding, not the set.
- **Hidden-read cap.** If the union of entries exceeds
  `HIDDEN_READ_PATH_MAX_COUNT`, the turn refuses with the existing
  `too_many_hidden_folders` reason.
- Other projects are read through `projects` and changed through `handoff`.

Stage layout:

| Path | Meaning |
| --- | --- |
| `outcome.json` | One turn outcome, under the ordinary Work contract. |
| `turns/<turn-id>/artifacts/` | As in chats. |
| `rcp` | Top-level client: `projects`, `schedule`, `handoff`, `ask` (action only). |
| `projects/<id>/rcp` | Per-project client on its own mailbox: `validate`, `graph_log`, `graph_diff`, `bulletin`. |
| `projects/<id>/patch.json` | That project's Patch. |

- `validate` names its target by which client runs it.
- Collection reads only `projects/<id>/patch.json` for admitted projects. Any
  other `patch.json` or `watch.json` is a stray file: kept as a receipt and
  discarded. `storage/watchers.py` refuses watcher rows whose origin task is
  `space_chat`.
- **Apply authority.** Admission writes one `space_turn_project_bindings` row
  per admitted project. `AppStore.agent_task_authority(project_id, operation_id)`
  (`src/rcp/storage/agent_tasks.py`) selects only `graph_runs` rows with that
  `project_id`. It gains a branch: for a `space_chat` row it returns the
  binding for that exact `(operation_id, project_id)`, with main as the target.
  A missing binding raises `KeyError`. `WriteAuthority.stamp_attribution`
  (`src/rcp/core/authority.py`) then refuses with "unknown agent task" and
  keeps its existing project and target equality checks.
- **Partial Apply.** Apply runs per project in project-id order. Each is its own
  transition under that project's lock and live membership check. A rejection
  does not undo another project's Apply. The turn result carries
  `project_results: {project_id: "applied" | "rejected" | "no_patch"}`.
- Seen head is `graph_seen_heads(project_id, owner_id = thread_id, session, "main")`.
  That table is already keyed by project.

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
  `chat_browser_preferences.browser_requested = 1`. The conversations spec rule
  "off by default" gains "except agent threads". A lost or unavailable browser
  never blocks a turn; that is already true.
- No MCP servers, as in Work. Provider sub-agents are allowed, as in Work.

### Verbs

**Changed from the decisions:**

- `apply` and `status` are orchestrator-only
  (`src/rcp/agents/auto_research_commands.py`). Threads get neither. A Work
  Patch applies at turn end.
- `spawn` and `message` create and message Auto-research workers
  (`src/rcp/agents/command_protocol.py`). They cannot drive board
  conversations. Threads use `handoff` and do not start or message board
  conversations.
- `lesson` is removed for every agent. Memory files replace it.

New verbs. All arguments use `extra="forbid", strict=True`. Mutating forms take
the existing `idempotency_key`.

`projects` (both kinds; read-only):

| Field | Type | Rule |
| --- | --- | --- |
| `action` | `list` or `show` | |
| `project_id` | str | `show` only. |

`list` returns the Cards projection that `GET /api/projects`
(`src/rcp/api/index.py`) returns for this member, plus `thread_id` and
`same_machine` per project. `show` adds pending Proposals, open Decisions and
Blockers, recent transitions and running episodes. Sizes are bounded in
`limits.py`.

`schedule` (both kinds):

| Field | Type | Rule |
| --- | --- | --- |
| `action` | `add`, `list` or `cancel` | |
| `cron` | str | `add` only; exactly one of `cron`, `at`. |
| `at` | str | `add` only; `YYYY-MM-DDTHH:MM`. |
| `timezone` | str | `add` only; IANA name; defaults to the turn's timezone. |
| `note` | str | `add` only; 1 to `AGENT_SCHEDULE_NOTE_MAX_CHARS`. |
| `schedule_id` | str | `cancel` only. |

`add` is refused unless the turn was admitted by a member message: trigger
`human`, persisted `source` absent (composer), `voice` or `handoff`, and
`store.question_for_followup(operation_id)` is None. Wakes and question
follow-ups are refused. The prompt adds: create one only when the member asked.

`handoff` (Space thread only):

| Field | Type | Rule |
| --- | --- | --- |
| `project_id` | str | A project of this member. |
| `message_file` | str | Safe workspace filename; at most `AGENT_HANDOFF_MAX_CHARS`. |

- It writes a pending `agent_handoffs` row and returns `{handoff_id, state}`.
- **Delivery.** Existing admission refuses a human turn while the target
  thread has an active or paused-resumable turn. So delivery happens when the
  target is eligible: at once if idle, otherwise when its turn settles or at
  startup. One transaction appends the member message with
  `source: "handoff"`, admits the turn and sets `target_operation_id`. Pending
  handoffs deliver oldest first, one turn each. The idempotency key prevents
  duplicates.
- **Not peer mail.** It reaches only the same member's own thread and carries
  that member's authority. design.md "no peer or cross-episode mail" gains:
  "A member's Space thread may hand that member's request to the member's own
  project thread; that is not mail." The Bulletin rule becomes: agents of
  different members, and board agents, share only through the Bulletin.

`ask` extension: `AskArguments` gains optional `action`, a discriminated union
on `kind`. With an action, `choices` must be empty and `multiple` false; RCP
supplies Approve and Decline. `question` is the agent's one-paragraph reason.

- In a project thread it creates a card. The action must name that thread's
  project.
- In the Space thread it creates a handoff to the member's thread for that
  project. RCP renders the message from the action: a request to ask for it
  with `ask` and this exact action. A plain `ask` in the Space thread is
  refused; the agent asks in its reply.

| `kind` | Fields |
| --- | --- |
| `proposal` | `project_id`, `proposal_id`, `decision` (`approved` or `rejected`), `reason?` |
| `truth_scope` | `project_id`, `alias`, `writable` (bool) |
| `auto_research` | `project_id`, `invocation_ceiling` (≥ 1), `starting_instruction?`, `code_worktree?`, `worktree_repository?`, `branch_name?`, `provider?`, `model?`, `reasoning?` |
| `merge_branch` | `project_id`, `branch_id` |
| `merge_episode` | `project_id`, `episode_id`, and the `MergeEpisodeBody` options: `target_branch?`, `history_mode` (`merge` or `squash`), `remove_worktree`, `delete_code_branch`, `archive_graph_branch`, `keep_branch_open`, `confirm_unfinished_jobs`, with that model's defaults and validator |

## Authority

### Approval cards

Protected acts wait for the member. Approval runs the existing human route
handler, with its admission, as the owner.

| `kind` | Handler | Route-level admission the card adds | Pins rechecked on tap |
| --- | --- | --- | --- |
| `proposal` | `sync_graph` (`api/sync.py`), body `{base_revision: current, proposals: [{proposal_id, decision, reason}]}` | None; the handler takes `project_write_admission` and `require_graph_edit_admission` inside | Proposal pending and not stale; content digest |
| `truth_scope` | `set_repository_writable` (`api/project_state.py`) | `project_write_admission` | Alias exists; value differs |
| `auto_research` | `start_episode` (`api/episode_routes.py`), `mode: "auto_research"` | `project_write_admission` | Readiness and the resolved profile |
| `merge_branch` | `merge_graph_branch`, `request_id` from the card | `project_write_admission` | Merge-preview source head and main head |
| `merge_episode` | `merge_episode_branch`, body with the card's options and `request_id` | None; inside | Merge-preview heads |

- **How the card calls a route.** Admission is inline in each route handler,
  and there is no shared service function. So the card route calls the handler
  as a Python function. It passes its own `Request`, which carries the owner's
  session, and its own injected dependencies. Route-level dependencies do not
  run on a direct call, so the card route enters them itself, as listed above.
  The handler then runs `require_patch_capable_identity`, membership, the
  transfer fence and the Experiment lock exactly as for a tap on the human
  control.
- **Idempotency.** Every action carries `request_id = uuid5(card namespace, question_id)`.
  `MergeEpisodeBody` gains optional `request_id` for this. `MergeBranchBody`
  already requires one.
- **Creation.** In a project thread, `ask` with an action validates it, reads
  the pins, and writes the `questions` row and the `approval_cards` row in one
  transaction. The question supplies the single **Needs you** push and the ask
  polling.
- **Single use.** `UPDATE approval_cards SET running_since = now WHERE question_id = ? AND running_since IS NULL`
  claims the card. A second tap finds it taken.
- **Recheck.** The client sends the `pinned_sha256` it showed. If that or any
  live pin differs, nothing runs and the answer is `stale`. This mirrors
  voice's confirm card, which rereads page state and runs nothing if a pin
  changed.
- **Expiry.** `APPROVAL_CARD_TTL_HOURS` (72) in `limits.py`. Expiry is checked
  when the card is tapped or listed; there is no sweeper. An expired card's
  question is dismissed.
- **Answer.** Settlement answers the question with `approved`, `declined`,
  `stale`, `failed: <reason>` or `expired`. Existing answer delivery steers the
  running turn or admits one follow-up, so a resolved card wakes its thread.
  The ordinary answer and dismiss routes refuse card questions (409
  `approval_card`).
- **Crash.** At startup, a card with `running_since` set and no `result_json`
  is answered `failed: interrupted`. It never re-runs on its own.
- **Not a page tool.** Neither the voice catalog nor WebMCP offers the card
  route. A test asserts both catalogs lack it. Invariant 3 already treats a page
  agent as the member; protected judgment stays outside the catalog.
- **Slack later.** A Slack-signed interaction from a Slack user the member
  linked inside RCP.

### Private records

- **Questions** in a private thread, plain or card, are owner-only.
  `notifications.py` enqueues them with `recipient_user_id = owner` only. The
  question list, answer and dismiss routes (`api/questions.py`) return 404 to
  other members.
- **Tasks.** `GET /api/projects/{id}/tasks`, `GET …/tasks/{operation_id}`, the
  task controls, `chat_inventory`, the chat list and the chat routes return 404
  for private-thread tasks and chats to anyone but the owner.
- **Transfer.** It keeps each private task's operation id, kind and authorizer,
  so Patch provenance still resolves. It redacts the request message and the
  answer text. Private transcripts (`private-*.jsonl`) are excluded.

### Member acts via the agent

Ordinary acts run without a tap: Patches, Experiment starts, compute, Bulletin
posts, schedules and handoffs. They already record the turn's operation and its
authorizer. Projections that show an authorizer gain `via_agent` (see API
contract), derived from the origin operation's `agent_threads` row. Nothing new
is stored in a Patch, so replay is unchanged.

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

| Run | Write roots | Hidden |
| --- | --- | --- |
| Project thread, and the Space thread per same-machine project | `shared/`, `members/<owner>/` | Every other `members/<id>/`; other members' `private-*` transcripts; `<data_dir>/agent-threads/<other>/` |
| Board Work and Discuss chats, Experiments, Auto-research, consolidation | `shared/` | All of `members/`; all `private-*` transcripts; all of `<data_dir>/agent-threads/` |
| Seed, Refresh, branch merge, Paper coach, artifact edit | None | Same as the row above |

- Discuss gains `shared/` as its one writable root outside scratch. That is
  fixed in code, like its scratch.
- "Child agents only add" was enforced by the lesson command handler. It is now
  a prompt rule.
- Hidden entries go through `resolve_hidden_read_scope`
  (`src/rcp/agents/hidden_read.py`) as directories and last-component globs:
  `<state>/chat/private-<user_id>-*.jsonl` per other member, or
  `<state>/chat/private-*.jsonl` for all. Where hiding is unavailable, the
  existing Settings and doctor reason applies.

### Format

- `MEMORY.md` is an index: one line per note,
  `- [Title](notes/<slug>.md) — one-line hook`.
- A note is Markdown whose first line is `# Title`. Migrated notes add a second
  line `origin: lesson <lesson_id>`.
- Prompts point to `shared/MEMORY.md` and, for threads, the owner's
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
- Removed: the `lesson` verb and its grants, the `/api/projects/{id}/lessons`
  routes, `web/src/projects/ProjectLessons.tsx` and its Settings entry, and the
  WebMCP lessons read.
- Consolidation prunes and merges `shared/` with ordinary file edits and reports
  each change. It cannot reach `members/`.
- Given up: note authorship and SQLite backup. `memory` joins
  `BACKUP_APP_DATA_EXCLUSIONS` and `TRANSFER_APP_DATA_EXCLUDED_ROOTS`. Lessons
  were not transferred either. A transfer that keeps the same base and account
  finds the folder by project id.
- Native CLI memory is off for every RCP run: Codex `--disable memories`,
  Claude `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (PR #369).

## Context

- Each thread keeps one native session. The provider auto-compacts it.
- A fresh session's first prompt carries the seed as text, inside a block
  labelled earlier conversation for context, not instructions or authority.
  Nothing is staged, so the seed never sits in a readable stage. RCP chat
  history is display data (conversations spec, "Native chat context").
- The tail is bounded by `AGENT_THREAD_TAIL_MAX_MESSAGES` and
  `AGENT_THREAD_TAIL_MAX_BYTES` in `limits.py`.
- **Provider switch.** Today it starts fresh with `provider_mismatch`. For
  threads, RCP first asks the old provider to compact the old session and
  seeds the new one with the result. New `chat_session_resolution` reasons:
  - `provider_switch_compacted`: the compaction succeeded.
  - `provider_switch_tail`: it could not run (old sign-in failed, session
    unusable, no compaction call, or `AGENT_THREAD_COMPACT_TIMEOUT_SECONDS`
    passed). The new session gets memory plus the tail.
- Every other fresh start keeps its existing reason and gets the tail.
- The thread shows a "started fresh" marker with the reason.

## Schedules

- The member asks; the agent creates the schedule with `schedule add`.
- Grammar: five numeric cron fields (minute, hour, day of month, month, day of
  week) with `*`, lists, ranges and steps. Or one `at` local time. The composer
  sends the browser's IANA timezone with each message, and the turn records it
  as the default.
- The thread that created a schedule owns it. The thread shows a schedules list
  with Cancel. `AGENT_SCHEDULES_MAX_ACTIVE` per thread caps standing cost. It
  does not limit wakes.
- **Firing.** `ConsolidationPoller.poll_once` (`src/rcp/consolidation.py`)
  gains a second step after `dispatch_due`: `dispatch_agent_schedules(now)`.
  There is no new poller. Next fire times reuse `occurrence_at`, so a
  nonexistent local time uses the next valid minute and an ambiguous one the
  first instance. `next_occurrence` is generalized from one daily time to the
  cron's matching local times. A due schedule sets `pending_since` and advances
  `next_fire_at` in one transaction. A one-shot schedule becomes `done`.
- **Fire once, combined.** When a thread has pending schedules and no active,
  paused or queued turn, the step claims all of them and admits one wake turn.
  The turn lists every claimed note and how many fires it missed.
- **Restore.** Consolidation restore deletes its schedules
  (`detach_consolidation_for_restore`). Agent schedules are kept but set to
  `disabled` with reason `restore` in `restore_detachment.py`. The member
  re-enables each one. Restore also sets every pending card's `expires_at` to
  the restore time. Handoffs are untouched.

## Wakes and pushes

A thread wakes on:

- a member message from the composer, voice or a handoff;
- in a project thread, its own `watch.json` through existing conversation
  watchers. Experiments it starts arm the existing node-status watcher;
- in the Space thread, a handoff settling: when the target turn settles, RCP
  admits one Space wake, claimed through `agent_handoffs.notified_operation_id`;
- in a project thread, a resolved card, through question answer delivery;
- a schedule fire.

There is no wake limit. Deferred wakes follow chat rules.

Unseen thread messages do not push today. A new notification kind,
`agent_thread`, covers project threads only. It goes to the owner through
`enqueue_notification(recipient_user_id=…)` when a turn settles and the owner
has not read past it (`chat_reads`). Its toggle defaults to on. The Space
thread gets no push in v1; its results arrive through project threads.

## Voice in the agent

- The voice button sits in the thread header. Voice binds to the thread that
  is open when it starts, and the panel names that thread.
- The backend seeds `session.input` from that thread's tail, within the
  existing 128-message and token bounds.
- Voice entries become display-only thread messages with `source: "voice"`.
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
  `disabled` with `access_lost`. Pending cards expire when next listed. The
  transcript and `members/<id>/` stay in place and stay hidden from others.
  Rejoining reopens the same thread.
- **Leaving the space.** `server_ops/members.py` removal deletes the
  Space-thread transcript folder, the member's schedules and pending handoffs,
  as it deletes voice records today.
- **Transfer and backup.** `agent_schedules`, `approval_cards` and
  `agent_handoffs` have no `project_id`, so they join `TRANSFER_GLOBAL_TABLES`
  (`transfer/archive.py`). `agent_threads`, `space_turn_project_bindings` and
  `memory_migrations` join `TRANSFER_EXCLUDED_PROJECT_TABLES`
  (`transfer/records.py`). `agent-threads` and `memory` join
  `BACKUP_APP_DATA_EXCLUSIONS` (`server_ops/backup_models.py`) and
  `TRANSFER_APP_DATA_EXCLUDED_ROOTS`. Private project transcripts stay in
  `.research/chat`, so backup captures them.

## Whole-app font

Already on this branch (`Figtree and JetBrains Mono across the whole app`).

- Figtree (`@fontsource-variable/figtree`) for `--ui` and `--display`, and
  JetBrains Mono (`@fontsource-variable/jetbrains-mono`) for `--mono`, in
  `web/src/styles/01-base.css` and every theme including
  `web/src/themes/aqua.css`. They are pinned exactly and imported in
  `web/src/main.tsx`. Sizes stay. The base theme loses its Charter serif
  headings.
- Vite emits the `.woff2` files into `web/dist`. KaTeX fonts already ship this
  way. `packaging/rcp_backend.spec` copies all of `web/dist`. The Tauri config
  sets no CSP, and the app page sends none. No packaging or native change is
  needed.

## API contract

All routes require a signed-in member. A thread route answers 404 unless the
caller owns the thread. Shapes are JSON; `?` marks optional fields.

### Thread list

`GET /api/agent-threads` creates any missing thread rows, then returns:

```json
{"threads": [{
  "thread_id": "uuid",
  "kind": "space | project",
  "project_id": "uuid | null",
  "project_name": "string | null",
  "agent_name": "string",
  "unread": true,
  "last_activity_at": "ISO-8601 | null",
  "live_state": "idle | queued | running | paused | needs_you | closed",
  "browser_requested": true
}]}
```

The Space thread comes first, then project threads by `last_activity_at`.
`needs_you` means an open question or card.

### Project threads reuse the chat routes

| Use | Route |
| --- | --- |
| Transcript | `GET /api/projects/{project_id}/chats/{chat_id}` (`ChatTranscript`; messages gain `source?`) |
| Send | `POST /api/projects/{project_id}/tasks/project_chat` with `{chat_scope: "project", chat_id, message, mode: "work", provider?, model?, reasoning?, timezone}`; `RunRequest` gains `timezone` |
| Task status and controls | `GET …/tasks/{operation_id}`, `POST …/tasks/{operation_id}/steer`, `/pause`, `/resume`, `/retry` |
| Questions and cards | `GET /api/projects/{project_id}/chats/{chat_id}/questions`; each question gains `card?` (below) |
| Plain answer and dismiss | `POST /api/projects/{project_id}/questions/{question_id}/answer`, `/dismiss` |

### Space thread routes

Same shapes as the chat routes above.

| Use | Route |
| --- | --- |
| Transcript | `GET /api/agent-threads/{thread_id}/transcript` → `ChatTranscript` |
| Send | `POST /api/agent-threads/{thread_id}/turns` with `{message, provider?, model?, reasoning?, timezone}` → 202 `{operation_id}` |
| Task status and controls | `GET /api/agent-threads/{thread_id}/tasks/{operation_id}`; `POST …/steer`, `/pause`, `/resume`, `/retry` |

### Both kinds

| Use | Route | Request | Response |
| --- | --- | --- | --- |
| Rename | `POST /api/agent-threads/{thread_id}/title` | `{title}` | the thread row |
| Mark read | `POST /api/agent-threads/{thread_id}/read` | `{read_through}` | the thread row |
| Browser | `PUT /api/agent-threads/{thread_id}/browser` | `{browser_requested}` | `{browser_requested}` |
| Schedules | `GET /api/agent-threads/{thread_id}/schedules` | | `{schedules: [Schedule]}` |
| Cancel schedule | `DELETE /api/agent-threads/{thread_id}/schedules/{schedule_id}` | | 204 |
| Re-enable schedule | `POST /api/agent-threads/{thread_id}/schedules/{schedule_id}/enable` | | `Schedule` |
| Voice entries | `POST /api/agent-threads/{thread_id}/voice-entries` | `{entries: [{entry_id, speaker: "member" \| "agent", text, at}]}` | `{appended}`; idempotent by `entry_id` |
| Voice memory | `GET /api/agent-threads/{thread_id}/memory` | | `{shared_index?, private_index?, truncated}` |

For a project thread, rename, browser and read delegate to the existing chat
owners.

`Schedule` is `{schedule_id, cron?, at_local?, timezone, note, state, disabled_reason?, next_fire_at?, last_fired_at?}`.

### Cards

A question's `card` is
`{kind, action, pinned, pinned_sha256, expires_at, state: "pending" | "running" | "expired"}`.

`POST /api/approval-cards/{question_id}` with
`{decision: "approve" | "decline", pinned_sha256}` returns
`{outcome: "approved" | "declined" | "stale" | "failed" | "expired", result?, reason?}`.
A second call returns 409 `card_taken`.

### Voice session

`POST /api/voice/sessions` takes `{sdp_offer, tools, playbook, thread_id}` and
drops `resume_id`. It returns `{sdp_answer, input_truncated, limits}`.

### `via_agent`

Task rows, Patch history rows, `EpisodeResponse` and Bulletin posts gain
`via_agent: {thread_id, name} | null`. The UI shows "Member, via Pip". The link
opens only for the owner.

## Alternatives considered

- **Several PRs.** Rejected by the human: one PR, #368.
- **Separate agents per project, no Space thread.** The member would still hop
  between projects to find the right one.
- **Router-only Space thread.** Simple changes on the member's own machine
  would need an extra hop.
- **Read-only Space thread.** The same cost as router-only.
- **Space thread owning lifecycles.** Questions, watchers and pushes are keyed
  by project; space-id rows broke them.
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
| Privacy between members. | Owner filtering on routes and pushes, and hidden-read. This is cooperative, not a security boundary: members are one lab, and the service account can read everything. Voice sends private text to the member's own OpenAI key. |
| Changes across projects are not atomic. | Per-project Apply with visible `project_results`; `outcome.json` says `partial`; the graph has no cross-project references. |
| Space id in bookkeeping rows. | One `task_owner()` resolver; tests cover each owner that uses it. |

## Build phases

One PR, in this order. Each phase ends green.

1. **Storage and contracts.** Migrations 66–71, `space_chat`, `source` and
   `privateOwner` records, limits, command argument models, web types.
2. **Memory files.** Root owner, write roots, hidden-read entries, migration,
   prompt pointers, consolidation workflow, lesson removal.
3. **Project threads.** Lazy creation, private naming and reader, owner
   filtering, default names, browser default, session reasons and seeding.
4. **Space thread.** Composite scope, per-project clients, collection,
   bindings and partial Apply, `projects`, `handoff` delivery, wakes,
   `task_owner()`.
5. **Approval cards.** `ask` action, card route, recheck, expiry, startup
   failure marking, question refusals, owner-only push.
6. **Schedules.** Grammar, poll step, restore detachment.
7. **Web.** Agent view and rail, thread view, removals, voice in the agent.
8. **Docs.** Move rules into specs, amend AGENTS.md and design.md, delete this
   file.

## Files

| Module | Files |
| --- | --- |
| Storage | `src/rcp/storage/base.py` (migrations), `src/rcp/storage/models.py` (`space_chat`), new `src/rcp/storage/agent_threads.py`, `src/rcp/storage/agent_tasks.py` (authority branch, owner filters), `src/rcp/storage/watchers.py`, `src/rcp/storage/questions.py`, `src/rcp/storage/lessons.py` (reader only), `src/rcp/storage/restore_detachment.py`, `src/rcp/storage/notifications.py` |
| Contracts | `src/rcp/service.py` (records, path, reader, `RunRequest.timezone`), `src/rcp/limits.py`, `src/rcp/agents/command_protocol.py`, `src/rcp/agents/staged_command_client.py`, `src/rcp/runs/episodes/merge.py` (`request_id`), `web/src/core/types.ts` |
| Agents | new `src/rcp/agents/memory_files.py`, `src/rcp/agents/write_scope.py`, `src/rcp/agents/hidden_read.py`, `src/rcp/agents/prompts.py`, `src/rcp/agents/context.py`, `src/rcp/agents/continuation_prompt.py`, `src/rcp/agents/auto_research_prompt.py`, `src/rcp/agents/auto_research_commands.py` |
| Runs | new `src/rcp/runs/agent_threads.py`, new `src/rcp/runs/approval_cards.py`, new `src/rcp/runs/agent_schedules.py`, `src/rcp/runs/questions.py`, `src/rcp/runs/chat.py`, `src/rcp/runs/chat_admission.py`, `src/rcp/runs/agent_names.py`, `src/rcp/runs/consolidation.py`, `src/rcp/runs/auto_research.py`, `src/rcp/runs/tasks/{work,discuss,work_turn_runtime,experiment_loop,auto_research_stream,auto_research_child_work}.py`; delete `src/rcp/runs/lesson_commands.py`, `src/rcp/runs/lessons.py` |
| Background and notices | `src/rcp/background.py`, `src/rcp/notifications.py`, `src/rcp/consolidation.py`, `src/rcp/skills/workflows/graph-consolidation/WORKFLOW.md` |
| API | new `src/rcp/api/agent_threads.py` (threads and cards), `src/rcp/api/app.py`, `src/rcp/api/chats.py`, `src/rcp/api/tasks.py`, `src/rcp/api/questions.py`, `src/rcp/api/voice.py`, `src/rcp/voice.py`; delete `src/rcp/api/lessons.py` |
| Transfer, backup, members | `src/rcp/transfer/archive.py`, `src/rcp/transfer/records.py`, `src/rcp/server_ops/backup_models.py`, `src/rcp/server_ops/members.py` |
| Web | new Agent view under `web/src/agents/`, `web/src/App.tsx`, `web/src/projects/PhoneProjectChrome.tsx`, `web/src/projects/ProjectSettings.tsx`, delete `web/src/projects/ProjectLessons.tsx`, `web/src/voice/*`, `web/src/webmcp/reads.ts` |
| Docs | `AGENTS.md` (4, 4b, 10b, 10g), `docs/design.md`, `docs/specs/conversations-episodes-and-watchers.md`, `docs/specs/providers-and-containment.md`, `docs/specs/graph-consolidation-and-lessons.md`, `docs/specs/api-web-and-desktop-projections.md`, `docs/specs/interface-and-visual-design.md`, `docs/specs/authority-and-proposals.md`, `docs/specs/server-and-machine-operations.md` |

## Test plan

Focused local checks follow AGENTS.md Verification. The full suite runs in PR CI.

- **Replay and migration.** Old transcripts without `source` keep their bytes.
  A private transcript round-trips through the reader; a renamed or moved one
  is refused. Migrations 66–71 run on a copy of a real old data directory.
  Lesson migration is idempotent across a crash between files and the marker
  row. An old-writer regression covers a lesson written by the old command and
  then migrated. A Space-thread Patch replays like any Work Patch.
- **Space thread.** Two same-machine projects: one Apply lands and one is
  rejected; `project_results` and `outcome.json` agree. A missing binding
  refuses Apply. Stray `patch.json` and `watch.json` are discarded. Per-project
  clients lack `episode`, `launch` and `ask`. Zero same-machine projects. A
  changed project set keeps the session. Over the hidden-read cap refuses.
  `task_owner()` covers startup recovery, Stop, Retry, steering and sign-in
  failure on a `space_chat` row.
- **Handoffs.** Delivery to an idle thread, to a busy thread on settle, and at
  startup; duplicate keys; a Space `ask` with an action becomes a handoff.
- **Authority.** A card under two concurrent taps runs once. Stale pins run
  nothing. Expiry on tap and on list. A card left `running` fails at startup.
  Each handler's admission refuses during a transfer fence. Only the owner
  resolves. Neither the voice catalog nor WebMCP lists the card route.
  `schedule add` is refused in wakes and question follow-ups.
- **Privacy.** Another member gets 404 on a private thread, its tasks and its
  questions, and no push. Hidden-read entries for each run kind. Transfer
  redacts private task text, keeps provenance and excludes `private-*`.
- **Schedules.** Missed fires during downtime yield one combined wake. DST
  edges. Restore disables schedules and expires pending cards.
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
