# Space relay: what runs in my projects, and what needs me

The Space thread is the member's one entry point. This design extends two
existing owners to it.

- **Runs index → Running list.** The Space Runs index already decides each
  episode's health label and section. The new list adds the member's own
  project threads to it and shows it from a new Running button in the Space
  thread's header, beside Wakes.
- **Approval dock → every question for me.** The dock above the Space composer
  shows approval cards today. It also shows the plain questions the member may
  answer.

Mockups were approved in the session that proposed this. Nothing else is added
to the Space thread's UI.

## Decisions

- **Scope.** Covers:
  - Experiment and Auto-research episodes in the member's projects, whoever
    started them.
  - The member's own project agent threads.
  - Questions from shared project chats (owner `None`), which the dock relays.

  Another member's private thread never shows
  ([owner-only rule](specs/conversations-episodes-and-watchers.md)).
- **Failures and pauses are list states, not dock cards.** The member acts on
  them through the Space agent's verbs, or by opening the row.
- **One row per run or agent.** "Watching" is a state of the row that owns the
  watcher. A watcher is never a row of its own.
- **The list has no controls.** A row only opens its run or thread.
- **Every question card shows when it was asked.** `QuestionCard` and
  `ApprovalCardShell` render `created_at`. The time is local clock time, with
  the date when the day is not today, as the thread's `localClock` formats it.

## Running list

`GET /api/agent-threads/running` reads across `member_project_ids` for the
acting member. Each row:

| Field | Value |
| --- | --- |
| `kind` | `experiment`, `auto_research` or `thread` |
| `id` | episode id or thread id |
| `project_id`, `project_name` | |
| `title` | episode title as the Runs index gives it, or the thread's agent name |
| `needs_you` | bool |
| `label` | short state text |
| `watching` | count of active or degraded watchers owned by the row |
| `at` | `last_activity_at` for episodes; the latest turn's `updated_at` for threads |
| `link` | `#/projects/{pid}/targets/{target}/episode/{eid}`, or `#/agent/{tid}` |

**Episode rows** come from the Space Runs builder. The body of
`space_runs` (`api/index.py`) is extracted into one function. The `/api/space/runs`
route and the new route both call it.
- A row is listed while `run_section` is not `completed` and the episode is not
  archived.
- `needs_you` is `run_section == "actionable"`. `label` is `health_label`.
- An Auto-research child Experiment (`auto_research_parent_episode_id` set) has no
  row of its own. Its parent's health already covers it.
- No new state mapping exists. The Runs projection stays the only owner of
  episode health text.

**Thread rows** come from `thread_projections` (`runs/agent_threads.py`), the
owner of thread `live_state`.
- `live_state` gains `failed`: the latest turn failed, and no retry or resume of
  it has started.
- A row is listed when `live_state` is one of `needs_you`, `running`, `queued`,
  `paused` or `failed`, or when the thread has an active watcher.
- `needs_you` is true for `needs_you`, `paused` and `failed`.
- `label` comes from the projection:

  | `live_state` | `label` |
  | --- | --- |
  | `needs_you` | "Asked you a question" |
  | `running`, `queued` | "Working" |
  | `paused` | "Paused" |
  | `failed`, `failure_kind` `provider_auth` | "Sign-in needed" |
  | `failed`, `classify_terminal_error(error)` is `session_limit` | "Session limit" |
  | `failed`, any other error | "Failed" |
  | idle with watchers | "Watching" |

  `provider_auth` is checked first.

**Order and size.** Rows with `needs_you` come first, then the rest. Each group
is ordered newest `at` first. The response carries `needs` and `total`, counted
before the list is cut to `SPACE_RUNNING_MAX_ROWS = 50` in `limits.py`.

**Button.** `RunningPopover` sits in the Space thread header, left of
`WakesPopover`. It reuses the Wakes popover's row styles. Its label is
`needs / total`, and it is hidden when `total` is 0.

## Dock

The member-wide read `GET /api/agent-threads/cards` (no `thread_id`) keeps
`proposal_cards` and `asked_cards` as they are. It gains `questions`: pending
questions with no card that `question_recipients` gives this member.

Each question row is the existing question serialization plus:
- `project_id`, `project_name`;
- `source_label`: the episode title, or the chat or thread title;
- `answer_continues`, computed per episode as `episode_questions` does it, so an
  ended episode's answer offers Add turns.

**`question_recipients(connection, question) -> set[str]`** is new in
`storage/notifications.py`.
- It is extracted from the inline SQL in `observe_notification_question`: a
  private thread's question goes to its owner, any other question to every live
  project member.
- Push keeps its own device and preference filters on top of it.
- The dock applies only this function.

**Rendering and answers.**
- `StackCard` gains `{kind: "question"}`. It renders `QuestionCard` with
  `apiBase` set to the question's project.
- The answer goes through the existing
  `POST /api/projects/{pid}/questions/{qid}/answer`. Membership, the private
  owner, write admission and continuation are all checked there.
- Approval cards keep their route.
- Stack order, folding and "Later" stay as they are.

## Freshness

`_agent_cards_version` (`api/resource_watermarks.py`) is the version for both
reads. It hashes:

- the member's own agent-thread rail;
- each member project's `episodes`, `watchers` and `questions` generations;
- the Space id's `questions` generation.

Before using the `questions` counter, the build checks whether another member's
private-thread question moves it. If it does, the counter is scoped so that it
does not. `tasks.status` is not hashed, because it moves on other members'
private turns.

The client refetches the cards and the Running list when the version moves.
The extracted Runs builder therefore runs only on those moves.

## Paper coach steering

The coach is the one human conversation that cannot be steered. This extends
chat steering to it.

- `chat_steering_visible` admits `paper_coach` turns with a human trigger.
  - The provider runtime, the live control, attempt and turn matching, and
    receipts stay as they are.
  - The coach therefore gets `can_steer`, `steer_turn_id` and
    `steer_action_label` on its task rows.
- Steered text needs a durable home, and the coach has no chat transcript.
  - Migration 76 adds `paper_coach_steers`, keyed by the steer `message_id`.
    Each row holds the operation, text, attempt, turn id, receipt and time.
  - `begin_chat_steer` picks its record owner from the task: the chat
    transcript for chats, this table for the coach. The idempotency rule is the
    same: a reused id with different content is refused.
- The coach composer behaves as chat does.
  - While a steerable turn runs, Send steers.
  - The Stop button stays beside it.
  - When the turn cannot be steered, Send stays disabled, with the reason on
    hover.
  - The coach transcript shows each steer after its turn's human message, with
    the delivery label chats show.
- **Checks:** steer route tests for a coach turn (delivered, refused when not
  running, id reuse). A browser test sends a steer from the coach composer and
  shows it in the transcript. Old-record replay of the new table, as
  migrations require.

## Out of scope

- New verbs and list actions.
- Relaying other members' private threads.
- Failure dock cards.
- Bringing a card to the front from the list.

## Checks

**Routes.**
- Episode rows match `/api/space/runs` for the same episodes.
- An Auto-research child has no row.
- Each thread row state appears in the order the table gives.
- A watcher sets `watching` on its owner and never adds a row.
- `needs` and `total` are counted before the cut.

**Privacy.**
- Another member's private-thread question and turn never appear in either read.
- Another member's turn does not move the version.

**Recipient rule.**
- `question_recipients` equals the old `observe_notification_question` targets
  for a thread-owned question, a shared chat question, and an episode question.

**Dock answers.**
- From the Space dock, an answer reaches a waiting episode, an ended episode with
  Add turns, and a thread.

**Freshness.**
- A question created with no graph commit moves the version.

**Browser.**
- Running popover rows and links.
- A dock question shows its time, answers, and leaves the stack.
- 390 px with no horizontal scroll.

**Served journey.**
- A fake agent in a project Experiment asks a question, and the member answers
  it from the Space dock.
