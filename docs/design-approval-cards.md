# Approval cards in reach, and message times (temporary design)

This file is deleted before merge. Its rules move to:
- the conversations spec (approval cards, private agent threads);
- the authority spec (Proposals);
- the interface spec (thread timeline).

**Problem.** A Proposal an agent raises in a member's thread sits in the project
Inbox. Members rarely open the Inbox while talking to the agent. Agents told
members to "approve it in the Inbox" or "ask me in a new turn", so the member
could not act where the conversation happens. Thread messages also show no
time, so it is unclear when a message was sent or when a reply arrived.

**Goal.** Every protected action an agent thread needs reaches the member as a
card they can tap where they are. A card costs no extra agent turn and never
forces an answer. Every message says when it happened.

## Decisions

The human settled these on 2026-11-02, from rendered mockups in the app's own
styling (snowbound dark, 1440 and 390 px).

**Layout the mockups fix:**
- A card is the existing approval card. It has an amber edge, an icon, and a
  bold title ("Approve Proposal: H2 standing → supported?").
- "See details" sits at the left. Later, Decline and Approve sit at the right.
- A stack shows the front card with two offset edges behind it, and "‹ 1 of 3 ›"
  above its right edge.
- Pip's dock is the same stack, placed directly above Pip's composer.

**Behaviour:**
- When a member's own project-thread turn lands Proposals, the member gets one
  card per Proposal in that thread. Several open cards show as a stack.
- A card has Approve, Decline and Later.
  - Later never settles anything and never blocks the agent.
  - Decline on any Proposal card rejects the Proposal. This includes an asked
    card whose action approves a Proposal.
- When the member asks an agent for a protected action ("reject that
  Proposal", "merge the branch"), the agent sends a card. The card usage
  reaches every project-thread turn again (#374).
- The Space thread (Pip) mirrors every open card from the member's project
  threads, docked above its composer and tappable in place. Pip cannot create
  cards.
- Each message shows its time on hover, or on a tap on phones. A quiet divider
  appears at a new day or after a gap of 30 minutes or more.
- Proposal cards are derived, not stored. The server draws each Proposal card
  from the live main graph, and creates no question or card row for it.

## Two kinds of card

- **Proposal card:** one per Proposal that a turn of this thread raised. The
  server derives it from main, and the server's decide route acts on it.
- **Asked card:** what an agent requests through `ask` with an approval
  action, for example:
  - a merge;
  - a truth-scope change;
  - Auto-research;
  - a pending Proposal the member names that this thread did not raise.

  Asked cards are stored, tapped and recovered as today, except for the changes
  in §4.

**Raised by this thread** means one thing everywhere: the Proposal's
`created_by_operation_id` names a `project_chat` task bound to the thread. A
turn's receipt lists at most 32 Proposal ids, so receipts never decide
membership.

## Alternatives

**Product**
- **Pip raises its own cards.** Rejected. It would touch verbs, origins,
  storage keys, admission, delivery and the web, while a mirrored tap already
  runs in the project.
- **Cards only through agent `ask`.** Rejected. A Proposal a turn raises is
  pending only after the turn's Patch lands, so its card would need a second
  turn the member has to prompt.
- **Inline mirrored cards in Pip's timeline.** Rejected by the human: docked
  cards stay reachable, inline ones scroll away.
- **Always-visible times.** Rejected by the human as clutter.

**Mechanism**
- **Stored Proposal cards.** Rejected by the human after four review rounds.
  - A post-commit reconciler would write a card row per pending Proposal,
    settle the ones decided elsewhere, and keep them out of delivery,
    snapshots and push.
  - Each review round found new races, retries and windows.
- **Browser-derived Proposal cards.** Rejected after round 5, for four
  reasons:
  - The browser's graph copy stays current only for open project tabs.
  - The thread view may hold a branch or held snapshot.
  - A tap Synced at the browser's revision fails on any unrelated commit.
  - Pip would load a full project snapshot per project.
- **Per-line task lookups for times.** Rejected. Turns load lazily, so
  off-screen times and dividers would appear late and shift. The chat routes
  already join per-message task data.

## Non-goals

- Cards for Experiment starts. A member's own Work chat already authorizes the
  Experiment they asked for (invariant 3).
- Cards in team-visible chats.
- Cards for Proposals raised by Pip or by node chats. These stay in the Inbox.
- A cross-project Inbox page.
- Push notifications for Proposal cards. The Proposal already raises
  `proposal` attention.
- `needs_you` for Proposal cards. The thread list's badge stays tied to asked
  cards and questions.

## 1. Server: deriving and deciding Proposal cards

**Owner.** The logic lives in a new `src/rcp/runs/proposal_cards.py`, and the
routes are added to `src/rcp/api/agent_threads.py`. Both check membership with
`owned_thread`, which only reads.

**Thread mapping.** A new store method,
`project_chat_threads(project_id, operation_ids) -> dict[str, str]`, maps each
operation id to the chat id of a bound `project_chat` task. It is one query on
`graph_runs`.

**Card view.** Each derived card has a `ProposalCardView` (defined in
`runs/proposal_cards.py`, mirrored in `web/src/core/types.ts`):

| Field | Meaning |
|---|---|
| `project_id`, `project_name`, `thread_id` | where the card belongs |
| `turn_id` | the Proposal's `created_by_operation_id` |
| `proposal_id`, `title` | the Proposal |
| `status` | `pending`, `approved`, `rejected`, `withdrawn` or `stale` (pending but in `stale_proposal_ids`) |
| `resolved_by` | `human` or `agent` once settled, from the Proposal |
| `details` | the Proposal's `card` fields (`situation_cold`, `why_human_now`, `consequences`, `decision_needed`) and its `proposal_actions` lines from attention |

**Routes.**
- `GET /api/agent-threads/{thread_id}/proposal-cards` returns every Proposal
  raised by that thread's turns, in any status, read from main state. It
  returns an empty list for a Space thread.
- `GET /api/agent-threads/proposal-cards` returns the pending, non-stale
  Proposal cards for all of the member's open project threads. It feeds Pip's
  dock.
  - A project is read only when the member has an open project thread in it.
  - The project must also have at least one bound `project_chat` task whose
    stored `graph_update.proposal_ids` is non-empty. This is a SQLite check, so
    the graph is never opened for projects with nothing to show.
  - A project whose state read fails is left out of the response, and the
    server logs it.
- `POST /api/agent-threads/{thread_id}/proposal-cards/{proposal_id}` takes
  `{decision: "approved" | "rejected", request_id}`. The server:
  1. reads live main state;
  2. requires the Proposal to be pending, not stale, and raised by this
     thread;
  3. Syncs that one decision through the human Sync path at the revision it
     just read, the way an asked card's `_execute` Syncs at its pinned
     revision;
  4. records the decision with reason "Decided from an approval card" and the
     member as decider.

  The route always targets main, whatever graph the page shows.

**Decide outcomes.**

| Response | Meaning | Card |
|---|---|---|
| 200 + the updated `ProposalCardView` | Decided | Redraws as settled |
| 409 `proposal_not_pending` | Decided or stale elsewhere | Redraws from a fresh read |
| 409 from write admission or the merge fence, 403, 422 | Refused | Shows the server's reason and stays open |
| 503 | Unavailable | Shows "Try again" and stays open |

If the graph moves between the read and the commit, the route reads once
more and retries, but only while the Proposal is still pending and not stale.

**Inbox drafts.** A member's staged Inbox draft that decides the same Proposal
is dropped on the draft's next reconcile (`reconcileHumanDraft`), as with any
Proposal decided outside the draft.

**Authority.** A card tap is the member's own human decision from their own
page (invariant 3). It is the same act as an Inbox decision, and reaches the
same Sync path.

## 2. Freshness

The watermark endpoint (`src/rcp/api/resource_watermarks.py`) computes
`AgentThreadsVersion` for a member.
- Today the version digests only the member's `agent_threads` counter.
- With this change it also digests the `graph:main` content generation of each
  project where the member has an open project thread. Each read is an indexed
  lookup in `catalog.content_generations`, with no project open.

So any commit to main in one of those projects refreshes the member's
agent-thread resources. That holds even when no project tab is open. Both
proposal-card resources key on that version.

## 3. Web: one card stack per turn

**Card shell.** `ApprovalCardView` splits into two layers:
- a presentational `ApprovalCardShell` holding the icon, title, project chip,
  details, error line, and the busy and settled states;
- two adapters:
  - `AskedApprovalCard` wraps today's question-and-card logic and
    `resolveApprovalCard`;
  - `ProposalApprovalCard` takes a `ProposalCardView` and calls a new
    `decideProposalCard` in `web/src/agents/agentThreadsApi.ts`.

The shell always shows Later.

**Turn stack.** `ApprovalCardStack` takes one turn's open cards, both asked and
Proposal, and owns:
- the stack order;
- the "1 of N" arrows;
- Later.

Settled cards of that turn render after the stack as one-line outcomes, for
example "Approved", "Rejected", or "Withdrawn by the agent".

**Which cards belong to a turn:**
- asked cards whose `operation_id` is the turn;
- Proposal cards whose `turn_id` is the turn.

**Where the stack renders.**
- `AgentWindowTranscript` gains an optional `renderTurnCards(turnId)` slot.
  The stack renders after the turn's last line.
- Only the project-thread `AgentThreadView` passes the slot.
- In project threads, card entries leave `agentWindowEntries`, so the stack is
  each card's only home.
- NodeChat, team chats and the Space thread pass no slot. They keep inline
  asked cards and the receipt's existing "N proposal(s) sent to Inbox" chip.

**Later.**
- Later is client-only and moves the card to the back of its stack.
- When one card is left, Later folds the stack to "1 card waiting", which
  unfolds on a tap.
- The order lives in a module-level map keyed by stack id and card ids. It
  survives a thread switch and is lost on reload, which restores server order.

**Out-of-date cards.** A Proposal card whose status is `stale` shows one line,
"Out of date", with Open Inbox. Open Inbox opens the project page on its
Inbox view (`attention`), the same view the receipt chip's `onOpenInbox` opens.

## 4. Asked cards: Decline on an approve-Proposal card

**Decline rejects.** When the stored action is a Proposal action with
`decision: approved`, Decline runs the normal tap path with the decision
flipped:
1. admission;
2. the pin recheck;
3. execution with the decision flipped to `rejected`.

The reason is the action's own reason, or "Declined from an approval card".
Pins and the card hash still come from the stored action.

**Decline outcomes:**

| Situation | Card settles | Proposal |
|---|---|---|
| Pins still match | `declined`, result `{proposal_id, decision: rejected}` | rejected |
| Pins fail because the Proposal was decided or went stale elsewhere | `declined`, result `{}` | unchanged; the member's "no" stands |
| Admission refuses (409 or 503) | stays open, shows the reason | unchanged |

**Other asked cards.** Decline on any other asked card keeps today's
behaviour: it settles `declined` and runs nothing. Declining a card that asks
to reject a Proposal never executes that rejection.

**Settling a tap.** `_run` takes the outcome and builds the result from the
action, not from the Sync response.

**Recovery.** `committed_receipt` returns `(outcome, receipt)` or None. It
settles an interrupted claim as follows:
- the Proposal's status equals the stored decision: `approved`;
- the stored decision is `approved` and the status is `rejected`: `declined`;
- otherwise: failed as interrupted, as today.

**Outcome text.** `APPROVAL_OUTCOMES` says that `declined` on an
approve-Proposal card means the Proposal was rejected, unless it had already
been decided elsewhere.

**Header.** `approvalCardTitle` reads `card.pinned.proposal.title`. Proposal
pins are stored as `{"proposal": ...}`, so today's header falls back to
"Approve this Proposal?".

## 5. The agent side

**Usage text.** `approval_card_usage` says:
- the member gets a card for each Proposal this thread's turns raise, so the
  agent never asks for one;
- the agent asks for a Proposal card only when the member names a pending
  Proposal that this thread did not raise.

The sentence "offer its card on the next turn" is removed.
`WORK_POLICY_VERSION` becomes `work-v24`.

**Duplicate guard.** `approval_card_for_ask` refuses a Proposal ask with
`proposal_card_shown` when this thread raised the Proposal.

**What the agent sees.** The agent reads every Proposal's status from graph
state on each turn. A Proposal card has no answer, so tapping one starts no
turn and sends nothing to the agent.

**Space prompt.** `space_limits_section` (`runs/space_chat.py`) says that
cards from project threads show in Pip's dock, and that Pip points the member
there. `SPACE_MASTER_POLICY` becomes `space-master-v3`.

## 6. Pip's dock

The Space view in `AgentThreadView` shows one `ApprovalCardStack`, docked above
the composer and hidden when it is empty. It holds two kinds of card.

**Proposal cards** come from `GET /api/agent-threads/proposal-cards`.

**Asked cards** come from `loadThreadQuestions`. The dock calls it for each
open project thread whose `live_state` is `needs_you`, or whose row reports an
open card.
- The thread list gains `open_cards`: a count of pending questions with a card,
  `parked` included. It is computed in the same `thread_projections` query.
- A card is open when it has a card, is not `withdrawn_readonly`, and its state
  is `pending` or `parked`.

**Cards in the dock:**
- Each card names its project through the shell's project chip.
- Each card decides through its own adapter.
- Proposal cards come first, newest turn first. Asked cards follow, newest
  first.

**Refresh.** The dock refetches when the member's agent-thread version changes
(§2), and after each of its own taps.

## 7. Message times

**Data.** `ChatMessage` gains `shown_at`. Two routes join it: the project chat
route (`api/chats.py`), as it joins `agent_status`, and the Space transcript
route (`api/agent_threads.py`). The value depends on the message:

| Message | `shown_at` |
|---|---|
| A member message that is not a steer | its turn's first attempt's `created_at` (the same walk `_turn_first_attempt` does) |
| An assistant message | its task's `finished_at`; Space records stamp both messages with `created_at` today |
| Steers, and messages with no task | none; the line keeps its own `timestamp` |

**Web.**
- `chatMessageTranscriptLine` (`web/src/agents/agentTasks.ts`) sets the line's
  `shownAt` to `shown_at ?? timestamp`.
- A reconstructed reply uses its task's `finished_at`, and shows no time while
  the task runs.
- Error lines use `finished_at`.
- `line.timestamp` itself is unchanged.

**Divider.**
- One divider goes before the first line.
- Another goes before any line whose `shownAt` falls on a new local day, or
  comes `THREAD_TIME_GAP_MINUTES` (30, in `web/src/core/uiConstants.ts`) or
  more after the previous line's `shownAt`.
- So a turn longer than 30 minutes gets a divider before its reply.
- Cards and receipts never count toward the gap, and never get a divider.

**Formats.** `agentThreadModel.ts` exports two functions. Both use fixed
English names, so output does not depend on locale.
- `clockOf(at)`: 24-hour `HH:MM`, built on the existing `clock`. The schedule
  chip uses it too.
- `dividerLabel(at, now)`: "Today · 19:21", "Yesterday · 08:02", or
  "Mon 2 Nov · 19:21". The year is added when it is not the current year.

**Showing the time.**
- Every line row carries its time.
- On a pointer that can hover, the time shows on hover or keyboard focus.
- On touch, a tap on the line toggles it. A tap on a link, button or chip, or
  a tap that selects text, does not.
- Nothing else in the row moves when the time shows.

## Ownership, order, and checks

| Step | Owner files | Checks |
|---|---|---|
| 1 | `runs/proposal_cards.py` (new), the `project_chat_threads` store method, `api/agent_threads.py` | `tests/test_proposal_cards.py` (new): the thread list has every status and only this thread's Proposals; a Space thread gets an empty list; the member list skips decided, stale, node-chat and other members' Proposals, and reads no project without candidates; deciding approves and rejects; `proposal_not_pending` after an Inbox decision; a moved graph is retried; admission refusals pass their reason through; another member gets 404. |
| 2 | `api/resource_watermarks.py` | `tests/test_resource_watermarks.py`: a main commit in a thread's project changes the agent-thread version with no project open. |
| 3 | `ApprovalCard.tsx` (shell and adapters), `ApprovalCardStack.tsx` (new), `AgentWindowTranscript.tsx` (`renderTurnCards`), `AgentThreadView.tsx`, `agentThreadsApi.ts`, `core/types.ts`, `web/src/styles/14-agent.css` | `web/tests/agentWindow.test.mjs`: grouping by turn; card entries leave a project thread only. `web/tests/agentThreads.test.mjs`: Later order and fold; settled lines. `web/tests/agentThreads.browser.test.mjs`: Approve and Decline on a Proposal card call the decide route and redraw; a refusal shows its reason; 390 px; a NodeChat receipt keeps its chip. |
| 4 | `api/approval_cards.py`, `runs/approval_cards.py`, `agentThreadModel.ts` (title) | `tests/test_approval_cards.py`: Decline rejects; Decline after an Inbox decision settles `declined` with no change; Decline on a reject card runs nothing; an interrupted Decline recovers as `declined`; the `_run` callers at 793–810 updated. `tests/lifecycles/proposals.py`: new `decline-stop-after-claim` and `decline-stop-after-commit` taps; `restart_never_reruns` expects `declined` when the stored decision is approved and the status is rejected; a Step rule that Decline on an approve card rejects. |
| 5 | `runs/approval_cards.py` (usage, guard), `agents/prompts.py`, `runs/space_chat.py` | `tests/test_approval_cards.py` and `tests/test_work_questions.py`: the usage data reaches project-thread turns; the duplicate guard refuses; policy versions bump. |
| 6 | `runs/agent_threads.py` (`open_cards`), `AgentThreadView.tsx` (dock) | `tests/test_agent_thread_routes.py`: `open_cards` counts parked cards and only the member's own. Browser: a mirrored card decided from Pip settles in its project thread; the dock hides when empty; 390 px. |
| 7 | `service.py` (`ChatMessage`), `api/chats.py`, `api/agent_threads.py`, `agentTasks.ts`, `AgentWindowTranscript.tsx`, `agentThreadModel.ts`, `uiConstants.ts`, `14-agent.css` | `tests/test_api_chats.py`: a member message carries its first attempt's `created_at`; a steer carries none; a reply carries `finished_at`. A Space transcript test checks the same for Pip. `web/tests/agentWindow.test.mjs`: dividers at 29 and 30 minutes, across local midnight, and on the first line; `dividerLabel` output. Screenshots at 1440 and 390. |
| 8 | specs; delete this file | egm `codebase-details` on the touched specs |

**Order.**
- Step 1 comes before steps 3 and 6, and step 3 before step 6.
- Steps 2, 4, 5 and 7 are independent of each other and of the rest.

## Risks and limits

- **Card volume.** A turn that raises many Proposals gets many cards. The stack
  keeps them to one row, and Later never blocks. Pip's dock holds every
  project's cards in one stack.
- **Dock cost.**
  - Each refetch reads main state for every project that has an open thread of
    the member's and a thread turn that raised Proposals. A remote project's
    read crosses SSH, as opening that project does.
  - A failing read leaves that project out of the refetch.
  - The version change in §2 also refreshes the thread list on every main
    commit in those projects.
- **No `needs_you` for Proposal cards.** The receipt stack and Pip's dock show
  them. The thread-list badge does not.
- **Decline meaning.** Members who used Decline to mean "not now" now reject.
  Later is the "not now", and it sits before Decline.
- **Decline needs the writer.** Decline on an approve-Proposal asked card now
  passes admission, so a closed or fenced project keeps the card open with a
  reason. Today such a Decline always settles.
- **Recovery race.** A tap's process may die mid-Sync while the same Proposal is
  decided in the Inbox. Recovery then attributes the outcome by the Proposal's
  status. Both events are rare, so no claim-time column is added.
- **Missed landings.** Proposals raised from Pip or from node chats get no card,
  and stay in the Inbox.
