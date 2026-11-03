# Approval cards in reach, and message times (temporary design)

This file is deleted before merge. Its rules move to:
- the conversations spec: approval cards and private agent threads;
- the authority spec: Proposals;
- the interface spec: the thread timeline.

## Problem

A Proposal that an agent raises in a member's project thread sits in the
project Inbox. Members rarely open the Inbox while they are talking to the
agent. Agents told members to "approve it in the Inbox" or to "ask me in a new
turn", so the member could not act where the conversation happens.

Thread messages also show no time. It is unclear when a message was sent or
when a reply arrived.

## Goal

Every protected action that a member's project thread needs reaches the member
as a card they can tap where they are. A card costs no extra agent turn and
never forces an answer.

Every message says when it happened.

## Decisions

The human settled these on 2026-11-02, from rendered mockups in the app's own
styling (snowbound dark, at 1440 and 390 px).

Layout shown in the mockups:
- A card is the existing approval card: amber edge, icon, and a bold title
  ("Approve Proposal: H2 standing → supported?").
- See details sits on the left. Later, Decline and Approve sit on the right.
- A stack shows the front card with two offset edges behind it. "‹ 1 of 3 ›"
  sits above its right edge.
- Pip's dock is that same stack, placed directly above Pip's composer.

Behaviour:
- When a member's own project-thread turn lands Proposals, the member gets one
  card per Proposal in that thread. Several open cards show as a stack.
- A card has three buttons: Approve, Decline, and Later.
  - Later never settles anything and never blocks the agent.
  - Decline on any Proposal card rejects the Proposal. That includes an asked
    card whose action approves a Proposal.
- When the member asks an agent for a protected action ("reject that
  Proposal", "merge the branch"), the agent sends a card. The card usage
  reaches every project-thread turn again (#374).
- The Space thread (Pip) mirrors every open card from the member's project
  threads. The cards are docked above Pip's composer and can be tapped there.
  Pip cannot create cards.
- Each message shows its time on hover, or on a tap on phones. A quiet divider
  appears at a new day or after a gap of 30 minutes or more.
- Proposal cards are derived, not stored. The server draws each one from the
  live main graph. No question row or card row is written for it.

## Two kinds of card

- **Proposal card**: one per Proposal that a turn of this thread raised. The
  server derives it, and taps on it go through the approval-card module.
- **Asked card**: what an agent requests through `ask` with an approval action.
  Examples: a merge, a truth-scope change, Auto-research, or a pending Proposal
  the member names that this thread did not raise. Asked cards are stored,
  tapped and recovered as they are today, except for §4.

"Raised by this thread" has a single meaning everywhere. The Proposal's
`created_by_operation_id` is one of the thread's turn ids: the `project_chat`
tasks of the thread's chat, authorized by the thread's member. A turn's
receipt can trim its Proposal ids, so receipts never decide which cards a
thread gets.

## Alternatives

Product:
- **Pip raises its own cards.** Rejected. It would touch verbs, origins,
  storage keys, admission, delivery and the web. A mirrored tap already runs in
  the project.
- **Cards only through agent `ask`.** Rejected. A Proposal is pending only after
  the turn's Patch lands, so its card would need a second turn that the member
  has to prompt.
- **Inline mirrored cards in Pip's timeline.** Rejected by the human. Docked
  cards stay reachable; inline ones scroll away.
- **Always-visible times.** Rejected by the human as clutter.

Mechanism:
- **Stored Proposal cards.** Rejected by the human after four review rounds. A
  post-commit reconciler would write one card row per pending Proposal, settle
  the cards decided elsewhere, and keep them out of delivery, snapshots and
  push. Each round found new races, retries and windows.
- **Browser-derived cards.** Rejected after round 5, for three reasons:
  - The browser's graph copy is current only for open project tabs, and may be
    a branch snapshot.
  - A tap that Syncs at the browser's revision fails on any unrelated commit.
  - Pip would load a full snapshot for every project.
- **New decide route and module beside the threads routes.** Rejected. The
  approval-card module already owns member-only taps: admission, the write
  fence, a Sync at a freshly read revision, and error mapping.
- **A separate stack slot that renders after a turn's last line.** Rejected.
  Running retries, watcher wakes and turns with no persisted message have no
  shown line, so their cards would render nowhere. `agentWindowEntries` already
  places cards with ordered fallbacks.
- **Folding graph generations into the thread-list version.** Rejected. That
  version reloads the open thread's transcript, questions and task. One main
  commit by anyone would reload every open thread.
- **Per-line task lookups for times.** Rejected. Turns load lazily, so
  off-screen times and dividers would appear late and then shift. The chat
  routes already read each message's task.

## Non-goals

- Cards for Experiment starts. A member's own Work chat already authorizes the
  Experiment they asked for (invariant 3).
- Cards in team-visible chats, node chats, or closed and archived threads.
- Cards for Proposals raised by Pip, node chats, or Experiments. These stay in
  the Inbox.
- A cross-project Inbox page.
- Push notifications for Proposal cards. The Proposal already raises `proposal`
  attention.
- A `needs_you` badge for Proposal cards. The thread-list badge stays tied to
  open questions.
- Voice and WebMCP tools for card taps, as for asked cards today.

## 1. Server

### Owner

`src/rcp/runs/approval_cards.py`, which already "reads live facts" for approval
preparation, gains the derivation. `src/rcp/api/approval_cards.py`, which
already owns member-only taps, gains the decide route. The two GET routes live
in `src/rcp/api/agent_threads.py` and call the runs function.

### Turn ids

A new store method, `thread_turn_ids(thread_ids) -> dict[operation_id,
thread_id]`, reads the `project_chat` tasks of each thread's chat. It uses the
`graph_runs_chat_turn` index, joined to `agent_threads` so that only tasks
authorized by the thread's member count. It is one query.

### Card view

`ProposalCardView` lives in `runs/approval_cards.py` and is mirrored in
`web/src/core/types.ts`.

| Field | Meaning |
|---|---|
| `project_id`, `project_name`, `thread_id` | Where the card belongs. |
| `turn_id`, `turn_created_at` | The raising turn and when it was created. |
| `proposal_id`, `title` | The Proposal. |
| `status` | `pending`, `stale` (pending and in `stale_proposal_ids`), `approved`, `rejected` or `withdrawn`. |
| `resolved_by` | `human` or `agent`, from the Proposal, once it is settled. |
| `details.situation`, `details.why_now`, `details.consequences`, `details.decision_needed` | The Proposal's `card` fields. |
| `details.changes` | The `proposal_actions` lines for a pending Proposal. Empty once settled. |

### Derivation

`proposal_cards(state, turn_ids, ...) -> list[ProposalCardView]` is a pure
function of a main state and a set of turn ids. It keeps the Proposals whose
`created_by_operation_id` is in the set. The result is cached per project,
keyed by the project's `graph:main` content generation. A project whose
generation is unavailable is read without caching.

### Routes

**`GET /api/agent-threads/{thread_id}/proposal-cards`**
- Checks `owned_thread`, which is read-only.
- Returns every pending or stale Proposal the thread raised.
- Returns settled Proposals only from the thread's newest
  `PROPOSAL_CARD_HISTORY_TURNS` (100, in `limits.py`) turns.
- A Space thread, or a closed or archived thread, gets `[]`.
- An unreadable graph returns 503. The thread view keeps its transcript and
  shows no cards.

**`GET /api/agent-threads/cards`**

This route feeds Pip's dock. It returns
`{proposal_cards, asked_cards, unavailable}` for the member's open project
threads, found the way `GET /api/agent-threads` finds them, without closing
any thread.
- `proposal_cards`: pending, non-stale cards, newest `turn_created_at` first.
- `asked_cards`: open card questions in those threads, newest first. Open
  means the question is pending and not withdrawn, and its card has no result.
  They are read in one SQLite query and serialized as thread questions are.
- `unavailable`: project ids whose state read failed.
- When the same Proposal has both a Proposal card and an asked card, only the
  Proposal card is listed.

**`POST /api/approval-cards/proposals/{proposal_id}`**

Body: `{thread_id, decision: "approved" | "rejected"}`. The route:
1. Checks the thread is owned, open, and not refused by `chat_turn_refusal`,
   as asked-card taps do (`_owned_card`).
2. Runs `_admit`, which includes `require_project_accepts_new_work`, write
   admission and the write fence.
3. Reads live main state.
4. Requires the Proposal to be raised by the thread, pending, and not stale.
5. Runs `_execute` with a `ProposalAction` at that revision.

The reason is "Approved from an approval card" or "Declined from an approval
card".

| Response | Card |
|---|---|
| 200 with the updated `ProposalCardView` | Redraws as settled. |
| 404: not raised by this thread, or no such thread | Removed. |
| 409 `proposal_not_pending`: decided, withdrawn or stale | Redraws from a fresh GET. |
| 409 `graph_changed` | Shows "Try again" and stays open. |
| 409 from admission or the fence, 403, 422 | Shows the server's reason and stays open. |
| 503 | Shows "Try again" and stays open. |

No request id is needed. A second tap after success gets
`proposal_not_pending`, so a double tap can never decide twice.

### Inbox drafts and authority

A staged Inbox draft that decides the same Proposal is dropped on the draft's
next reconcile (`reconcileHumanDraft`). This is the same as for any Proposal
decided outside the draft.

A tap is the member's own human decision from their own page (invariant 3). It
reaches the same Sync path that an Inbox decision does.

## 2. Freshness

`WatermarkResponse` (`src/rcp/api/resource_watermarks.py`) gains
`agent_cards`, beside `agent_threads`. Its version is a digest of two inputs:
- the member's `agent_threads` counter;
- the `graph:main` content generation of each project where the member has an
  open project thread.

The generation is the workspace's in-memory token; reading it opens no
project. Only the two card resources key on `agent_cards`. The thread list and
transcripts keep keying on `agent_threads`.

Local projects refresh on every main commit. A remote project's generation
moves only when this process publishes or captures it, which needs an open
tab, an activation or a requested sync. So a remote card decided on another
device can look open until then. A tap then answers `proposal_not_pending`,
and the card redraws as settled.

## 3. Web: cards and stacks

### Card shell

`ApprovalCardView` splits into two parts:
- `ApprovalCardShell`, which is presentational: icon, title, project chip,
  details, error line, busy and settled states, and the buttons, with Later
  always present.
- Two adapters around it:
  - `AskedApprovalCard` keeps today's question and card logic and
    `resolveApprovalCard`.
  - `ProposalApprovalCard` takes a `ProposalCardView` and calls
    `decideProposalCard` in `web/src/agents/agentThreadsApi.ts`.

Both adapters render a Proposal's details the same way, from situation, why
now, consequences, decision needed, and changes. The asked card takes these
from its pins.

### Placement

`agentWindowEntries` places cards as it does today.
- It also accepts Proposal cards, placed by `turn_id` with the same ordered
  fallbacks: the owner's lines, the card's time position, then the end.
- It groups the open cards of one turn chain (the root attempt, through the
  existing parent links) into one `cards` entry.
- That entry renders through the transcript's card slot as one
  `ApprovalCardStack`.

Only the project-thread `AgentThreadView` passes Proposal cards. Every other
transcript behaves exactly as today. The receipt keeps its "N proposal(s) sent
to Inbox" chip everywhere.

When a thread shows a Proposal card and an asked card for the same Proposal,
only the Proposal card is shown. This covers asked cards created before this
change.

### Stack

`ApprovalCardStack` owns the stack order, the "1 of N" arrows, and Later.
- **Later** moves a card to the back of its stack. With one card left, it
  folds the stack to the single line "1 card waiting", which unfolds on a tap.
- **Order**: the stack order and the fold are kept per stack in memory. They
  survive a thread switch and are lost on reload, which restores server order.

### Settled and out-of-date cards

After the stack, the turn's settled cards render one per line:
- A Proposal card shows "Approved" or "Rejected". It shows "Withdrawn" when
  `resolved_by` is `agent`, and "Withdrawn when out of date" when it is
  `human`.
- An asked card renders as it does today.
- A `stale` Proposal card shows "Out of date" with an Open Inbox link to
  `projectViewHash(project_id, main, "attention")`.

### Loading

Proposal cards load as their own resource keyed on `agent_cards`. A failed
load hides the cards and leaves the transcript, questions and task as they
were.

## 4. Asked cards: Decline on an approve-Proposal card

### Decline rejects

On an asked card whose stored action is a Proposal action with
`decision: approved`, Decline takes the tap path: admission, then the pin
check, then `_execute` with the decision flipped to `rejected`. The rejection
reason is always "Declined from an approval card". It never reuses the
agent's approval reason, because that reason enters append-only history under
the member's name.

| Situation | Card settles | Proposal |
|---|---|---|
| The pins match and Sync commits. | `declined`, result `{proposal_id, decision: "rejected"}` | Rejected. |
| Any pin failure: presented hash, live pins, or a Proposal decided or stale elsewhere. | `declined`, result `{}` | Unchanged. The member's "no" stands. |
| Sync refuses the execution (422). | `failed` with the reason | Unchanged. |
| Admission refuses (409 or 503). | Stays open and shows the reason. | Unchanged. |

### Other asked cards

Decline on any other asked card keeps today's behaviour, including the
presented-hash check. A card that asks to reject a Proposal is never executed
by Decline.

### Results and recovery

Proposal action results always carry `{proposal_id, decision}`, whether they
come from a live tap or from recovery. Merge and Auto-research results still
come from their handlers' responses.

Recovery of an interrupted Proposal card reads the Proposal:
- Status equals the stored decision: `approved`.
- Stored decision `approved`, status `rejected`, and resolution reason
  "Declined from an approval card": `declined`.
- Anything else: failed as interrupted, as today. A rejection made by someone
  else in the Inbox is never credited to this member's tap.

`APPROVAL_OUTCOMES` says that `declined` on an approve-Proposal card means the
Proposal was rejected, unless its result is empty.

### Header

`approvalCardTitle` reads `card.pinned.proposal.title`. Today the header
falls back to "Approve this Proposal?", because Proposal pins are stored as
`{"proposal": ...}`.

## 5. The agent side

- **Usage text.** `approval_card_usage` says the member gets a card for each
  Proposal this thread's turns raise, so the agent never asks for one. The
  agent asks for a Proposal card only when the member names a pending Proposal
  that this thread did not raise. The "offer its card on the next turn"
  sentence goes. `WORK_POLICY_VERSION` becomes `work-v24`. This step also
  carries §4's `APPROVAL_OUTCOMES` change.
- **Duplicate guard.** `approval_card_for_ask` refuses a Proposal ask with
  `proposal_card_shown` when this thread raised the Proposal.
- **No signal to the agent.** The agent reads every Proposal's status from
  graph state each turn. A Proposal card has no answer, so a tap starts no
  turn and sends the agent nothing.
- **Space prompt.** `space_limits_section` (`runs/space_chat.py`) says cards
  from project threads show in Pip's dock, and that Pip points the member
  there. `SPACE_MASTER_POLICY` becomes `space-master-v3`.

## 6. Pip's dock

The Space view in `AgentThreadView` loads `GET /api/agent-threads/cards` as a
resource keyed on `agent_cards`. It renders one `ApprovalCardStack`, docked
above the composer and hidden when empty:
- Proposal cards come first, then asked cards, in server order.
- Each card names its project through the shell's project chip.
- Each card decides through its own adapter. The dock refetches after each tap.

When the response lists `unavailable` projects, the dock shows one muted line
("Some projects could not be read"), with the project names on hover.

## 7. Message times

### Data

`ChatMessage` gains `shown_at`. The project chat route (`api/chats.py`) and
the Space transcript route (`api/agent_threads.py`) both set it. Each reads
its messages' tasks in one query.

| Message | `shown_at` |
|---|---|
| A member message that is not a steer, including question answers and handoffs | The `created_at` of its turn's first attempt. `_turn_first_attempt` moves from `runs/space_chat.py` to `runs/shared.py`, so both routes use it. |
| An assistant message | Its task's `finished_at`. Space records stamp both messages with `created_at` today. |
| A steer, or a message with no task | None. |

### Web

`chatMessageTranscriptLine` (`web/src/agents/agentTasks.ts`) sets the line's
`shownAt` to `shown_at ?? timestamp`. `TaskTranscriptLine` and `ChatMessage`
in `web/src/core/types.ts` gain the fields.
- A reconstructed reply uses its task's `finished_at`, and shows no time while
  the task runs.
- Reconstructed and pending human lines keep their `timestamp`, which is the
  send time.
- Error lines use `finished_at`.
- `line.timestamp` is unchanged, so card placement is unchanged.

### Divider

A divider goes before the first shown line. Another goes before a line whose
`shownAt` falls on a new local day, or comes `THREAD_TIME_GAP_MINUTES` or more
after the previous counted line's `shownAt`. The constant is 30, in
`web/src/core/uiConstants.ts`.
- Card entries and receipt-only lines (an agent line with no text and a graph
  update) neither count nor get a divider.
- A turn longer than 30 minutes therefore gets a divider before its reply.

### Formats

`agentThreadModel.ts` exports two functions. Both take a `Date`.
- `clockOf(at)` gives `HH:MM` on a 24-hour clock, built on `clock`. The
  schedule chip in `AgentThreadView` uses it too.
- `dividerLabel(at, now)` gives "Today · 19:21", "Yesterday · 08:02",
  "Mon 2 Nov · 19:21", or "Mon 2 Nov 2025 · 19:21" when the year is not the
  current one. Weekday and month names come from fixed English arrays.

### Showing it

Every line row carries its time. It shows on hover or keyboard focus on a
pointer that can hover. On touch, a tap on the line toggles it, unless the tap
lands on a link, button or chip, or selects text. Nothing else in the row
moves.

## Ownership, order, and checks

| Step | Owner files | Checks |
|---|---|---|
| 0 | Mockups | Render mockups of the folded stack, the settled lines, the "Out of date" line and the dock's unavailable line, in the app's styling at 1440 and 390 px. The human judges them before step 3. |
| 1 | `runs/approval_cards.py` (`ProposalCardView`, `proposal_cards`, cache), `thread_turn_ids` store method, `api/agent_threads.py` (GETs), `api/approval_cards.py` (decide), `limits.py` | `tests/test_approval_cards.py`. The thread list keeps only this thread's Proposals and bounds settled history. Space, closed and archived threads get `[]`. The member list returns pending cards, open asked cards and `unavailable`, and dedupes by Proposal. Decide approves and rejects. An Inbox decision gives `proposal_not_pending`. A moved graph gives `graph_changed`. Admission refusals pass their reason through. Another member gets 404. A double tap decides once. `tests/lifecycles/proposals.py` adds a derived-card decide action that races an Inbox decision. |
| 2 | `api/resource_watermarks.py`, `web/src/core/refreshScheduler.ts` | `tests/test_resource_watermarks.py`: a local main commit changes `agent_cards` and not `agent_threads`. |
| 3 | `ApprovalCard.tsx` (shell, adapters), `ApprovalCardStack.tsx` (new), `AgentWindowTranscript.tsx` (`agentWindowEntries`), `AgentThreadView.tsx`, `agentThreadsApi.ts`, `core/types.ts`, `styles/14-agent.css` | `web/tests/agentWindow.test.mjs`: Proposal cards place by `turn_id` with every fallback (a running retry, a turn with no lines); one turn chain gives one entry; a Proposal card hides an asked card for the same Proposal. `web/tests/agentThreads.test.mjs`: Later order and fold; settled labels from `resolved_by`; the Out-of-date link. `web/tests/agentThreads.browser.test.mjs`: Approve and Decline call the decide route and redraw; a refusal shows its reason; "Try again" on 503; 390 px. |
| 4 | `api/approval_cards.py`, `runs/approval_cards.py` (recovery, results), `agentThreadModel.ts` (title) | `tests/test_approval_cards.py`: Decline rejects; Decline after an Inbox decision settles `declined {}`; Decline on a reject card runs nothing; a 422 settles `failed`; an interrupted Decline recovers as `declined`; another member's Inbox rejection recovers as failed; Proposal results carry `{proposal_id, decision}`. `tests/lifecycles/proposals.py`: `decline-stop-after-claim` and `decline-stop-after-commit` taps; `restart_never_reruns` expects `declined` as above; a Step rule that Decline on an approve card rejects. `web/tests/agentThreads.test.mjs`: the title comes from the pins. |
| 5 | `runs/approval_cards.py` (usage, guard), `agents/prompts.py`, `runs/space_chat.py` | `tests/test_approval_cards.py`: the guard refuses with `proposal_card_shown`. `tests/test_work_questions.py`: the usage data names derived cards. Both policy versions bump. |
| 6 | `AgentThreadView.tsx` (dock) | `web/tests/agentThreads.browser.test.mjs`: a card decided from Pip settles in its project thread; the dock hides when empty; the unavailable line; 390 px. |
| 7 | `service.py`, `api/chats.py`, `api/agent_threads.py`, `runs/shared.py`, `runs/space_chat.py`, `agentTasks.ts`, `AgentWindowTranscript.tsx`, `AgentThreadView.tsx`, `agentThreadModel.ts`, `uiConstants.ts`, `core/types.ts`, `14-agent.css` | `tests/test_api_chats.py`: a member message carries its first attempt's `created_at` across a Retry chain; a steer carries none; a reply carries `finished_at`. `tests/test_agent_thread_routes.py`: the same for Pip's transcript. `web/tests/agentWindow.test.mjs`: dividers at 29 and 30 minutes, across local midnight, on the first line, and never on receipt-only lines; `dividerLabel` output. |
| 8 | Served-app journey | On disposable data: a thread turn raises two Proposals; approve one and decline one from the thread; decide one from Pip; decide one in the Inbox and watch its card settle. Inspect network, console and server logs. |
| 9 | Specs; delete this file | egm `codebase-details` on the touched specs. |

Order:
- Step 0 comes before step 3.
- Step 1 comes before steps 3 and 6.
- Step 2 comes before steps 3 and 6, which rely on it for freshness.
- Step 3 comes before step 6.
- Steps 4 and 5 ship together, after steps 1 and 3, because the guard and the
  new usage text assume derived cards exist.
- Step 7 is independent.
- Step 8 comes last.

## Risks and limits

- **Card volume.** A turn that raises many Proposals gets many cards. The stack
  keeps them to one row, and Later never blocks. Pip's dock holds every
  project's cards in one stack.
- **Dock cost.** A refetch re-derives only projects whose main generation
  moved. A remote project's read crosses SSH, as opening it does. A failing
  read is reported through `unavailable`.
- **Remote freshness.** A remote card decided on another device can look open
  until this process captures that project. A tap then settles it.
- **No `needs_you` badge for Proposal cards.** The thread's stack and Pip's
  dock show them. The thread-list badge does not.
- **Decline meaning.** A member who used Decline as "not now" now rejects.
  Later is the "not now", and it sits before Decline.
- **Decline needs the writer.** Decline on an approve-Proposal asked card now
  passes admission. In a closed or fenced project, the card stays open and
  shows the reason. Today such a Decline always settles.
- **Missed landings.** Proposals raised by Pip, node chats or Experiments get
  no card, and stay in the Inbox.
