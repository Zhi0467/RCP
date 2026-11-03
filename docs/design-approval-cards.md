# Approval cards in reach, and message times (temporary design)

This file is deleted before merge. Its rules move to:
- the conversations spec: approval cards and private agent threads;
- the authority spec: Proposals;
- the interface spec: the thread timeline.

**Problem.**
- A Proposal that an agent raises in a member's project thread sits in the
  project Inbox. Members rarely open the Inbox while they talk to the agent.
- Agents told members to "approve it in the Inbox" or "ask me in a new turn",
  so the member could not act where the conversation happens.
- Thread messages show no time, so it is unclear when a message was sent or a
  reply arrived.

**Goal.**
- Every protected action a member's project thread needs reaches the member as
  a card they can tap where they are.
- A card costs no extra agent turn and never forces an answer.
- Every message says when it happened.

## Decisions

The human settled these on 2026-11-02, from rendered mockups in the app's own
styling (snowbound dark, 1440 and 390 px).

**Layout shown in the mockups**
- A card is the existing approval card: an amber edge, an icon, and a bold
  title ("Approve Proposal: H2 standing → supported?").
- "See details" sits on the left. Later, Decline and Approve sit on the right.
- A stack shows the front card with two offset edges behind it. "‹ 1 of 3 ›"
  sits above its right edge.
- Pip's dock is that same stack, directly above Pip's composer.
- A divider row shows "Today · 19:21". A message's time appears in its footer
  row on hover.

**Behaviour**
- When a member's own project-thread turn lands Proposals, the member gets one
  card per Proposal in that thread. Several open cards show as a stack.
- A card has Approve, Decline, and Later.
  - Later never settles anything and never blocks the agent.
  - Decline on any Proposal card rejects the Proposal. That includes an asked
    card whose action approves a Proposal.
- When the member asks an agent for a protected action ("reject that
  Proposal", "merge the branch"), the agent sends a card. The card usage
  reaches every project-thread turn again (#374).
- The Space thread (Pip) mirrors every open card from the member's project
  threads, docked above its composer and tappable in place. Pip cannot create
  cards.
- Each message shows its time on hover, or on tap on phones. A quiet divider
  appears at a new day or after a gap of 30 minutes or more.
- **Proposal cards are derived, not stored.** The server draws them from the
  live main graph, and no question or card row is written for them.
- **Only open cards show.** After a tap, the card shows its result until the
  next refresh, and then it drops away. The turn keeps its existing
  "N proposal(s) sent to Inbox" chip.
- **A card decided elsewhere clears within seconds** on local projects.

## Two kinds of card

- **Proposal card.** One per pending Proposal that a turn of this thread raised.
  The server derives it, and a shared decide helper acts on it.
- **Asked card.** What an agent requests through `ask` with an approval action.
  Examples are a merge, a truth-scope change, Auto-research, or a pending
  Proposal the member names that this thread did not raise. Asked cards are
  stored, tapped and recovered as today, except for the change in §4.

**Raised by this thread** means one thing, and one function decides it. The
Proposal's `created_by_operation_id` names a task, and
`bound_thread(store, task)` (`storage/agent_threads.py`) returns this thread
for that task. The lookup starts from the Proposal and does a handful of point
reads per project. Receipts never decide which cards a thread gets.

## Alternatives

**Product**
- **Pip raises its own cards.** Rejected. It would touch verbs, origins,
  storage, admission, delivery and the web. A mirrored tap already runs in the
  project.
- **Cards only through agent `ask`.** Rejected. A Proposal is pending only after
  the turn's Patch lands, so its card would need a second turn the member has
  to prompt.
- **Inline mirrored cards in Pip's timeline.** Rejected by the human: docked
  cards stay reachable, while inline ones scroll away.
- **Always-visible times.** Rejected by the human as clutter.
- **A settled-history line per Proposal.** Rejected by the human. It needed a
  second route, a turn-history limit and new labels.

**Mechanism**
- **Stored Proposal cards.** Rejected by the human after four review rounds of
  races, retries and windows.
- **Browser-derived cards.** Rejected. The browser's graph copy is current only
  for open tabs and may be a branch. A tap at the browser's revision fails on
  any unrelated commit.
- **A decide route tied to a thread.** Rejected. Authority comes from the
  patch-capable member, project membership and write admission (invariant 3),
  the same as an Inbox decision. A thread check adds no authority.
- **A stack slot after a turn's last line.** Rejected. Running retries, watcher
  wakes and turns with no persisted message have no shown line, so their cards
  would render nowhere. `agentWindowEntries` already places cards with ordered
  fallbacks.
- **Graph generations in the thread-list version.** Rejected. That version
  reloads the open thread's transcript, questions and task, so a commit by
  anyone would reload every open thread.
- **Holding the claim across Decline's rejection.** Rejected. The claim does not
  record which button was tapped. Recovery would have to guess from free-text
  reasons that other members can write too.
- **Per-line task lookups for times.** Rejected. Turns load lazily, so
  off-screen times would appear late and shift.

## Non-goals

- Cards for Experiment starts. A member's own Work chat already authorizes the
  Experiment they asked for (invariant 3).
- Cards in team-visible chats, in node chats, or in closed threads.
- Cards for Proposals raised by Pip, node chats or Experiments. These stay in
  the Inbox.
- A cross-project Inbox page.
- Push notifications for Proposal cards. The Proposal already raises
  `proposal` attention.
- A `needs_you` badge for Proposal cards.
- Voice and WebMCP tools for card taps. Asked cards do not have them today
  either.
- De-duplicating a Proposal card against an asked card for the same Proposal.
  The ask guard stops this inside one thread. Across threads, Pip may show
  both, and a tap on either settles the other on its next refresh or tap.

## 1. Server

**Owners**
- `src/rcp/runs/approval_cards.py`, which already "reads live facts", owns the
  derivation.
- `src/rcp/api/approval_cards.py`, which already owns member-only taps, owns
  the decide route and the shared decide helper.
- The GET route sits in `src/rcp/api/agent_threads.py` and calls the runs
  function.

**Card view: `ProposalCardView`** (in `runs/approval_cards.py`, mirrored in
`web/src/core/types.ts`)

| Field | Meaning |
|---|---|
| `project_id`, `project_name`, `thread_id` | Where the card belongs. |
| `turn_id`, `turn_created_at` | The first attempt of the raising turn, and when it was created. Found with `turn_first_attempt` (§7). |
| `proposal` | The Proposal model dump. The web already types it. |
| `stale` | Whether the Proposal is in attention's `stale_proposal_ids`. |
| `changes` | Attention's `proposal_actions` lines for the Proposal. |

**Derivation**
- `proposal_cards(store, project, state, threads)` takes:
  - `project`: the project record, for its id and name;
  - `state`: the display snapshot's main state;
  - `threads`: the member's open project threads in that project.
- It reads `project_graph_attention(state)` once.
- For each pending Proposal, it reads the creating task, keeps the Proposal if
  `bound_thread` returns one of `threads`, and builds the view.
- It writes nothing and caches nothing.

**`GET /api/agent-threads/cards`**
- Optional query: `thread_id`.
- Threads come from `store.agent_threads(user_id)`, filtered by:
  - `kind == "project"`;
  - `closed_at IS NULL`;
  - not archived;
  - project membership.

  The route never calls `ensure_agent_threads`.
- With `thread_id`, it checks `owned_thread` and reads only that thread's
  project. A closed or archived thread, or a Space thread, gets an empty
  response.
- State comes from `catalog.open_display(project_id).history.state()`. That is
  the local display snapshot that the `graph:main` token describes, so no
  remote read happens here.
- A project whose state raises `StateUnavailable`, `ReplayHalted`, `OSError` or
  `KeyError` is skipped and logged. With `thread_id`, that project's failure
  returns 503 instead.
- **Response:** `{proposal_cards, asked_cards}`.
  - `proposal_cards`: newest `turn_created_at` first.
  - `asked_cards`: the threads' open card questions, newest first. They come
    from the question list path that already expires due cards
    (`store.approval_cards()`), serialized with `_serialize_all`
    (`api/questions.py`), with the thread's `project_name` added.
  - A question is open when it is `pending` and not `withdrawn_readonly`, and
    its card has no result. A claimed card (`running_since` set) counts as open
    and shows its spinner.

**`POST /api/approval-cards/proposals/{project_id}/{proposal_id:path}`**
- Body: `{decision: "approved" | "rejected"}`. Proposal ids contain slashes, so
  the id is a path parameter. The web encodes it with `encodeURIComponent`.
- The route calls `decide_proposal(request, project_id, proposal_id,
  decision)`, a helper in `api/approval_cards.py` that:
  1. runs the same membership and new-work admission as `_admit`;
  2. opens the writer and reads live main state;
  3. requires the Proposal to be pending and not stale;
  4. runs `_execute` with a `ProposalAction` at that revision.
- The reason is "Approved from an approval card" or "Declined from an approval
  card".
- No presented hash is needed, for two reasons: a Proposal's content never
  changes under its id, and staleness is rechecked at the fenced revision.

| Response | Card |
|---|---|
| 200 `{proposal_id, status}` | Shows "Approved" or "Rejected" until the next refresh. |
| 409 `proposal_not_pending` (decided, withdrawn or stale) | Redraws from a refetch. |
| 409 `graph_changed` | Shows "Try again" and stays open. |
| 409 from admission or the fence, 403, 404, 422 | Shows the server's reason and stays open. |
| 503 | Shows "Try again" and stays open. |

**Other effects**
- A second tap after success gets `proposal_not_pending`, so no request id is
  needed.
- A staged Inbox draft that decides the same Proposal is dropped on its next
  reconcile (`reconcileHumanDraft`).
- A tap starts no agent turn of its own. Graph watchers armed on the Proposal
  fire as they do after any human Sync.

## 2. Freshness

**Watermark**
- `WatermarkRequest` gains `agent_cards: bool`.
- `WatermarkResponse` gains `agent_cards: AgentCardsVersion | None`, holding
  `{version}`.
- Both live in `src/rcp/api/resource_watermarks.py`.
- The version is a digest of the epoch, the member, and the sorted
  `(project_id, graph:main generation)` pairs for the member's open project
  threads.
- The generation is the workspace's in-memory token, so reading it opens no
  project.

**Web**
- `RefreshScheduler` (`web/src/core/refreshScheduler.ts`) gains
  `watchAgentCards(listener)`, alongside `watchAgentThreads`.
- A `useAgentCardsVersion()` hook in `web/src/agents/useAgentThreads.ts`
  exposes it.
- The card fetches in the thread view and in Pip's dock key on that version
  and on the `agent_threads` revision. So a finished turn brings its new cards,
  and a main commit clears decided ones.

**Remote projects**
- A remote project's generation moves only when this process publishes it or
  captures it.
- So a card decided on another device can look open until then. A tap answers
  `proposal_not_pending` and redraws.
- A project with no display workspace yet has the token `"unavailable"`, so
  its cards refresh only on thread events until a tab opens it.

## 3. Web: cards and stacks

**Card shell** (`web/src/agents/ApprovalCard.tsx`)

`ApprovalCardView` splits into two parts.
- `ApprovalCardShell` is presentational. It renders the icon, title, project
  chip, details, error line, busy and settled states, and the buttons. Later is
  always shown.
- Two adapters wrap it:
  - `AskedApprovalCard` keeps today's question and card logic and calls
    `resolveApprovalCard`.
  - `ProposalApprovalCard` takes a `ProposalCardView` and calls
    `decideProposalCard` in `web/src/agents/agentThreadsApi.ts`.

The Proposal card's details are the Proposal's `card` fields (situation, why a
human now, consequences, decision needed) and `changes`. The asked card's
details stay as they are today.

**Placement** (`web/src/agentWindow/AgentWindowTranscript.tsx`)
- `agentWindowEntries(lines, questions, proposalCards = [])` places Proposal
  cards by `turn_id` and `turn_created_at`, with today's ordered fallbacks: the
  turn's lines, then the card's time position, then the end.
- Card entries that land at the same position form one `cards` entry,
  `{kind: "cards", key, asked, proposals}`.
- The transcript gains an optional `renderCards(entry)` prop.
  - With it, a `cards` entry renders as one `ApprovalCardStack`.
  - Without it, each card renders through `renderQuestion` as today.
- Only the project-thread `AgentThreadView` passes Proposal cards and
  `renderCards`. NodeChat and team chats have no approval cards and behave as
  they do today.
- The receipt keeps its "N proposal(s) sent to Inbox" chip everywhere.

**Stack** (`web/src/agents/ApprovalCardStack.tsx`, new)

The stack owns the order, the "1 of N" arrows and Later.
- Later moves a card to the back of its stack. With one card left, it folds
  the stack to "1 card waiting", which unfolds on a tap.
- Order and fold are kept in memory per stack key (thread id plus anchor key).
  A refetch keeps the relative order of surviving cards, appends new cards, and
  drops settled ones. A reload restores server order.

**Out of date**
- A `stale` Proposal card shows one line, "Out of date", with an Open Inbox
  link to `projectViewHash(project_id, MAIN_GRAPH, "attention")`.

**Failure**
- A failed card fetch hides the cards and leaves the transcript as it is.

## 4. Asked cards: Decline on an approve-Proposal card

When the stored action is a Proposal action with `decision: approved`, a
Decline goes through four steps:
1. Settle the card `declined` immediately, as every Decline does today. The
   member's answer is recorded before anything else.
2. Run `decide_proposal(..., decision="rejected")` (§1) on the same Proposal.
3. The tap's response carries `{rejected: true}`, or `{rejected: false,
   reason}`. A reason comes from `proposal_not_pending`, admission, the fence,
   or 503.
4. The card's settled line reads "Declined" or "Declined · Proposal still
   pending: <reason>".

**Rules for this Decline**
- The claim is never held across the rejection, so recovery needs no change.
- The agent reads the Proposal's status from the graph.
- The rejection reason is always "Declined from an approval card". The agent's
  approval reason is never reused, because it would enter append-only history
  under the member's name.

**Other asked cards** keep today's Decline unchanged, including the
presented-hash check. A card that asks to reject a Proposal is never executed
by Decline.

**`APPROVAL_OUTCOMES`** says that a declined approve-Proposal card was followed
by a rejection attempt, and that the agent reads the outcome from graph state.

**Header.** `approvalCardTitle` (`agentThreadModel.ts`) reads
`card.pinned.proposal.title`. Proposal pins are stored as
`{"proposal": ...}`, so today the header falls back to "Approve this
Proposal?".

## 5. The agent side

- **Usage text.** `approval_card_usage` says:
  - The member gets a card for each Proposal this thread's turns raise, so the
    agent never asks for one.
  - The agent asks for a Proposal card only when the member names a pending
    Proposal that this thread did not raise.

  The "offer its card on the next turn" sentence is removed.
  `WORK_POLICY_VERSION` becomes `work-v24`. This change also covers §4's
  `APPROVAL_OUTCOMES`.
- **Duplicate guard.** `approval_card_for_ask` refuses a Proposal ask with
  `proposal_card_shown` when `bound_thread` says this thread raised the
  Proposal.
- **No answer to deliver.** The agent reads every Proposal's status from graph
  state each turn. A Proposal card has no answer to deliver.
- **Space prompt.** `space_limits_section` (`runs/space_chat.py`) says that
  cards from project threads show in Pip's dock, and that Pip points the member
  there. `SPACE_MASTER_POLICY` becomes `space-master-v3`.

## 6. Pip's dock

The Space view in `AgentThreadView` fetches `GET /api/agent-threads/cards` with
no `thread_id`. It renders one `ApprovalCardStack`, docked above the composer
and hidden when empty.
- Proposal cards come first, then asked cards, in server order.
- Each card names its project in the shell's project chip.
- Each card decides through its own adapter, and the dock refetches after each
  tap.

## 7. Message times

**Scope.** `AgentWindowTranscript` covers agent threads, project chats and node
chats. All of them get times and dividers, because they share the transcript
and the chat route.

**Server**
- `ChatMessage` (`service.py`) gains `shown_at`.
- The transcript readers set it: the chat route (`api/chats.py`) and the Space
  transcript route (`api/agent_threads.py`).
- A new `store.agent_tasks_by_ids(ids)` (`storage/agent_tasks.py`) reads a
  transcript's tasks in one query.
- `_turn_first_attempt` moves to `runs/shared.py` as `turn_first_attempt`. It
  walks parents through that map, and fetches missing parents in one more batch
  per hop. Retry chains are a few hops deep.

| Message | `shown_at` |
|---|---|
| A member message that is not a steer, including question answers and handoffs | `created_at` of its turn's first attempt |
| An assistant message | its task's `finished_at` (Space records stamp both sides with `created_at` today) |
| A steer, or a message with no task | none; the web falls back to the line's own timestamp, which is a steer's send time |

**Web**
- `chatMessageTranscriptLine` (`web/src/agents/agentTasks.ts`) sets `shownAt`
  to `shown_at ?? timestamp`.
- The types gain these fields: `TaskTranscriptLine` in `agentTasks.ts`, and
  `ChatMessage` in `core/types.ts`.
- A reconstructed reply uses its task's `finished_at`, and shows no time while
  the task runs.
- Reconstructed and pending human lines keep their timestamp, which is the send
  time.
- Error lines use `finished_at`, then `created_at`.
- `line.timestamp` is unchanged, so card placement is unchanged.

**Divider**
- One divider goes before the first counted line.
- Another goes before a line whose `shownAt` falls on a new local day, or is
  `THREAD_TIME_GAP_MINUTES` (30, in `web/src/core/uiConstants.ts`) or more after
  the previous counted line.
- Card entries, and receipt-only lines (an agent line with no text and a graph
  update), are not counted.
- Examples: "Today · 19:21", "Yesterday · 08:02", "Mon 2 Nov · 19:21", and
  "Sun 2 Nov 2025 · 19:21" when the year differs.
- Times use a 24-hour `HH:MM` clock, built on the existing `clock` and
  `WEEKDAYS` in `agentThreadModel.ts`. The schedule chip uses the same clock.

**Showing it**
- Every line row carries its time.
- With a hovering pointer, the time shows on hover or keyboard focus.
- On touch, a tap on the line toggles it. A tap does nothing when it lands on a
  link, button or chip, or when it selects text.
- Nothing else in the row moves.

## Ownership, order, and checks

| Step | Owner files | Checks |
|---|---|---|
| 0 | Mockups | Render the folded stack and the "Out of date" line in the app's styling at 1440 and 390 px. The human judges them before step 3. |
| 1 | `runs/approval_cards.py` (`ProposalCardView`, `proposal_cards`); `api/agent_threads.py` (GET); `api/approval_cards.py` (`decide_proposal`, POST) | `tests/test_approval_cards.py`. **GET:** keeps only this thread's pending Proposals; drops one authorized by another member in the same chat; returns empty for Space and closed threads; returns 503 for an unreadable thread project; the member list skips unreadable projects and lists open asked cards, including claimed ones, but not expired ones. **Decide:** approves and rejects; slashed ids; `proposal_not_pending` for decided, withdrawn and stale; `graph_changed`; admission refusals; another project's member gets 404; a double tap decides once. `tests/lifecycles/proposals.py` gains a decide action that races an Inbox decision. |
| 2 | `api/resource_watermarks.py`, `refreshScheduler.ts`, `useAgentThreads.ts` | `tests/test_resource_watermarks.py`: a local main commit moves `agent_cards` but not `agent_threads`; opening or closing a thread moves `agent_cards`. A web unit test checks that `watchAgentCards` fires on a version change. |
| 3 | `ApprovalCard.tsx`, `ApprovalCardStack.tsx`, `AgentWindowTranscript.tsx`, `AgentThreadView.tsx`, `agentThreadsApi.ts`, `agentThreadModel.ts`, `core/types.ts`, `styles/14-agent.css` | `web/tests/agentWindow.test.mjs`: Proposal cards place by `turn_id` with every fallback (a running retry, a turn with no lines); cards at one position form one entry; with no `renderCards`, output is unchanged. `web/tests/agentThreads.test.mjs`: Later order across a refetch; the fold; the Out-of-date link; a failed fetch hides the cards. `web/tests/agentThreads.browser.test.mjs`: Approve and Decline call the route and show the result; a refusal shows its reason; "Try again" on 503; 390 px. |
| 4 | `api/approval_cards.py` (Decline), `runs/approval_cards.py` (outcomes), `agentThreadModel.ts` (title) | `tests/test_approval_cards.py`: Decline settles `declined` and rejects; Decline after an Inbox decision settles `declined` with `rejected: false`; an admission refusal still settles `declined` and reports its reason; Decline on a reject card runs nothing. `tests/lifecycles/proposals.py`: a Step rule says a Decline on an approve card rejects or reports why. `web/tests/agentThreads.test.mjs`: the title comes from the pins. |
| 5 | `runs/approval_cards.py` (usage, guard), `agents/prompts.py`, `runs/space_chat.py` | `tests/test_approval_cards.py`: the guard refuses with `proposal_card_shown`. `tests/test_work_questions.py`: the usage data names derived cards. Both policy versions bump. |
| 6 | `AgentThreadView.tsx` (dock) | `web/tests/agentThreads.browser.test.mjs`: a card decided from Pip leaves its project thread; the dock hides when empty; 390 px. |
| 7 | `service.py`, `api/chats.py`, `api/agent_threads.py`, `storage/agent_tasks.py`, `runs/shared.py`, `runs/space_chat.py`, `agentTasks.ts`, `AgentWindowTranscript.tsx`, `AgentThreadView.tsx`, `agentThreadModel.ts`, `uiConstants.ts`, `core/types.ts`, `14-agent.css` | `tests/test_api_chats.py`: a member message gets its first attempt's `created_at` across a Retry chain; a steer gets none; a reply gets `finished_at`; tasks load in one batch. `tests/test_agent_thread_routes.py`: the same for Pip's transcript. `web/tests/agentWindow.test.mjs`: dividers at 29 and 30 minutes, across local midnight, on the first line, never on receipt-only lines; the divider labels; tap toggling, ignoring links and selections. |
| 8 | Served-app journey | On disposable data: a thread turn raises three Proposals; approve one and decline one in the thread; decide one from Pip; decide one in the Inbox and watch its card clear. Inspect the network, console and server logs. |
| 9 | Specs; delete this file | egm `codebase-details` on the touched specs |

**Order**
- Step 0 comes before step 3.
- Steps 1 and 2 come before steps 3 and 6, and step 3 comes before step 6.
- Steps 4 and 5 ship together, after steps 1 and 3.
- Step 7 is independent.
- Step 8 comes last.

## Risks and limits

- **Card volume.** A turn that raises many Proposals gets many cards. The stack
  keeps them to one row, and Later never blocks. Pip's dock holds every
  project's cards in one stack.
- **Fetch cost.** Each card fetch reads the display snapshot of each project
  with an open thread, plus one task row per pending Proposal. Display reads
  stay local.
- **Remote freshness.** A remote card decided elsewhere can look open until this
  process captures that project. A tap then settles it.
- **Cross-thread duplicates.** Pip may show a Proposal card and another thread's
  asked card for the same Proposal. Deciding one makes the other settle on its
  next tap or refresh.
- **Legacy asked cards.** Cards asked before this change, for a thread's own
  Proposals, sit next to the new Proposal card until they are tapped or expire
  (72 h).
- **Decline meaning.** Members used to Decline as "not now" now reject. Later is
  the "not now", and it sits before Decline.
- **Decline is two steps.** If the rejection fails, the card still reads
  declined, and the reason it gives says the Proposal is still pending.
- **Missed landings.** Proposals from Pip, node chats or Experiments get no
  card, and stay in the Inbox.
