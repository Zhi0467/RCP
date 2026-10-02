# Agents ask the human through an `ask` command verb

Date: 2026-10-01
Status: design settled with the human on 2026-10-01 and reviewed once by an
xhigh design pass the same day. Implementation started in this PR on
2026-10-01. Done: the protocol, polling client, durable question store, and
nonblocking helper (slice 1); human Work and Experiment handlers, receipt and
follow-up admission, chat projection, owner prompts, and fresh question
snapshots (slice 2); orchestrator dispatch, answer mail, wake snapshots,
lifecycle withdrawal and reopening, the health overlay, and per-question
notifications (slice 3). Slice 4's prompts landed inside slices 2 and 3.
Slice 5 adds authenticated question list/answer/dismiss routes, shared Web cards
in node/project chats and both episode views, and existing-cadence refresh.
API tests cover concurrent retry, owner delivery, withdrawal, membership,
strict input, and immutable follow-up binding; Web helper tests and the build
pass. Disposable served HTTP checks pass for chat/episode question lists,
dismissal persistence, and ended-episode refusal. On 2026-10-01 the served app
on disposable seeded data rendered the chat cards and the Auto-research and
ended-Experiment episode cards, answered a single-choice question (the card moved
into the transcript as answered), and fit a 375 px phone width; the question
browser test passes outside the Codex sandbox. Still open: the close criteria
below with a real provider and broker (live answer within one call, parked
answer starting the follow-up turn, restart, orchestrator wake and phone push). After a human resolves a question, the answer
API calls `app.state.reconcile_question_answers(project_id)` for chat and
Experiment owners and `record_auto_research_question_answer(store, question_id)`
then ordinary mail delivery for orchestrator questions; dismissal never
dispatches mail. Live broker socket checks need an unrestricted test host.
Fix round 1 implements scalar Web refresh signals, exact-answer retry delivery,
and client-token acknowledgement before successful settlement can confirm receipt.
Missing acknowledgement retains follow-up eligibility on both command transports;
legacy offered-only receipts are insufficient. Item 4 restores append-only steering
receipt snapshots, records the four question routes in the frozen inventory, and
retains the resolved `ask` offer in Work/Experiment mailbox checkpoints so resume
cannot add question authority to a validation-only turn. The close criteria below
remain open.

Fix round 2 claims reopened, undelivered Experiment answers in the continuation's
creation transaction for its first invocation, using the answer-wake origin
binding checks. Matching answers reach invocation 1's snapshot even at ceiling 1;
unprovable or changed bindings leave the answered card read-only. The real-provider
close criteria below remain open.

Close this handoff when all of these hold:

- a Work chat turn asks, the human answers on the card within one client call,
  and the agent continues in the same turn;
- a Work chat turn parks a question, the human answers later, and the answer
  starts the next turn on the same native session with the asking turn's
  capability, write scope, and graph target;
- a parked question survives an RCP restart and is still answerable;
- an Auto-research orchestrator asks, the episode keeps running, the phone gets
  one "Needs you" push, and the answer wakes the orchestrator as human mail;
- a regression test proves an answer cannot change the capability, scope, or
  graph target of the turn it resumes.

Why one command verb, and not MCP or the providers' own ask tools:
[decision](../decisions/2026-10-01-agents-ask-through-the-command-channel.md).

## Who can ask

- **Work-shaped turns:** node chat, project chat, and Experiment episodes a
  human started. They wait live.
- **The Auto-research orchestrator.** It parks at once.
- **Not:** Discuss turns (they ask in prose), Auto-research workers, and child
  Work or child Experiments an orchestrator started. Those mail the
  orchestrator, which asks the human. Humans talk only to the orchestrator.

Availability comes from each handler's resolved allowed verbs. The same list
drives dispatch and the prompt text. Work capability or an episode id alone
never grants `ask`.

## The request

`ask --key <key>` with:

- `question`: text;
- `choices`: optional list;
- `multiple`: optional flag, only meaningful with choices.

A free-text answer is always accepted. Size caps live in `limits.py`. There is
no urgency field and no default-if-silent.

The response envelope stays `status: ok`. The question's state is in
`result.state`: `pending`, `answered`, `dismissed`, or `parked`. Transport
`delivery` keeps its current meaning and is separate from question state.

## Work: live wait

1. The server handler never blocks; the mailbox serves requests one at a time.
   It records the question (or finds it by owner and key) and answers at once
   with its current state.
2. The client re-sends the same key and arguments with a **fresh transport
   request id** on an interval, until one outer deadline of
   `COMMAND_CLIENT_WAIT_SECONDS`. The same file name would replay a cached
   `pending` forever.
3. `answered` returns the answer text and chosen choices. `pending` at the
   deadline tells the agent to repeat the exact call to keep waiting, or to end
   its turn so the question parks.
4. The human answers on the question card. The composer keeps steering; it does
   not answer.

## Work: parked answers

- An answer to a parked question is stored as the human's chat message and
  starts the next turn on the **asking turn's** native session, with the asking
  turn's capability, write scope, and graph target. These come from the
  question's origin binding, not from the composer's current settings.
- An answer recorded during the turn that no `ask` call received also starts
  that follow-up turn, once the asking turn has fully settled.
- Follow-up admission runs from the settlement callback and on startup, never
  from inside the handler or from provider output. It claims delivery in the
  same transaction that inserts the task. An occupied or paused session defers
  it. A binding that can no longer launch stays visible on the card.
- An SSH disconnect or restart is not proof the provider died. The existing
  detached mailbox and liveness owner decides before another turn is admitted.

## Experiment episodes

- Experiment validation today refuses a turn that ends with no watcher and no
  Blocker or Proposal (`runs/tasks/experiment_loop.py`, the empty-handoff check
  and Patch settlement). An open question or an undelivered answer becomes a
  valid continuation source. Unwatched compute is still refused.
- Live polling spends the invocation already admitted. A parked answer spends
  one Experiment invocation through normal Experiment admission, keeping the
  session, target, scope, Stop, and ceiling. It never becomes node chat. An
  exhausted episode needs the human's reauthorization first.
- A continuation episode carries its predecessor's open questions, keeping the
  origin provenance.

## The orchestrator

- `ask` records the question and returns `parked` at once. The prompt tells the
  orchestrator to end its turn.
- The episode stays `running`. Workers continue and worker mail still wakes the
  orchestrator. Health shows `needs_action` as an overlay while any question is
  open, below terminal states. The persisted status never changes, so admission
  is unaffected.
- The answer is delivered as human mail. That wake spends one invocation like
  any mail wake.

## Lifecycle

- A question stays open until the human answers or dismisses it, across later
  turns.
- Dismiss: a live call returns `dismissed`. Otherwise the next turn or wake
  lists it as dismissed. Dismissing never wakes an agent and never spends
  budget.
- When an episode ends, its cards become read-only and stop counting toward
  `needs_action`. A continuation episode makes them answerable again.

## Storage

One AppStore question table owns, per question:

- the immutable origin binding: project and chat or episode, logical turn,
  provider and session, stage, capability, write-scope fingerprint, and graph
  target, all from server records;
- uniqueness by owner and key across turns and recovery, plus argument identity
  (a repeat with other arguments is `invalid`);
- state, the human's resolution, the answer revision, client receipt, and the
  follow-up delivery claim, each recorded separately. An ambiguous receipt keeps
  the input rather than losing it.

The answer is projected once into the chat history through `StateWorkspace`
with a stable message id. SQLite and canonical history are separate stores, so
the projection is durable and retried.

## Notifications

- One push per question id when the question is created, using the existing
  `episode_needs_action` ("Needs you") kind, for chats and episodes alike.
- The push links to the chat or the episode card. Delivery checks know the item
  may be a chat question, not only an episode.
- A health transition caused by that same question sends no second push.
  Dismissal, later wakes, and reauthorization send nothing new.

## Prompts

Standing prose rides in versioned masters. Question state rides in fresh
per-turn parts.

- **Shared contract:** one renderer beside `_command_client_rule` in
  `agents/prompts.py`. It covers when to ask, the fields, identical-key
  repeats, that an answer is human input and never approval or a change to
  capability, roots, target, or budget, and that a change to an existing
  ResearchQuestion or Hypothesis is still a Proposal. Caps render from
  `limits.py`; syntax matches the client parser.
- **Work addition** in `_work_execution_instructions` (`runs/tasks/work.py`),
  independent of compute guidance and outside the watcher-only conditional:
  the wait window, repeat-to-wait versus park, the card answers and the
  composer steers, and what `dismissed` means. Experiment gets the same text
  through `_experiment_start_contract`.
- **Orchestrator addition** in `_command_invocations`
  (`agents/auto_research_prompt.py`): parks at once, end the turn, answers
  arrive as mail, an answer wake spends one invocation, dismissal never wakes.
  It appears in the root contract and the session-start Retry contract.
- **Version bumps:** chat master 14→15; Work, Experiment, and Auto-research
  masters v2→v3. The shared `MASTER_VERSION` stays.
- **Question snapshots:** each fresh, recovery, or wake launch carries a bounded
  list of open questions, dismissals not yet delivered, and newly delivered
  answers, read from question records and never from chat transcripts
  (invariant 10d). Larger snapshots use the existing input staging. Delivery
  attribution is persisted, so a failed launch does not swallow a dismissal.

## Slices

1. Protocol, client, store: the `ask` verb and its request model, client
   parsing and the polling loop under one outer deadline, the question table
   and its migration, limits.
2. Work and Experiment: the Work command handler that composes questions beside
   compute, the Experiment handler and continuation-source rule, follow-up
   admission from settlement and startup, the chat-history projection, and the
   retained handler on recovery.
3. Orchestrator and attention: dispatcher support, mail delivery of answers,
   the health overlay, and per-question notifications.
4. Prompts: the shared renderer, the per-surface additions, version bumps, and
   question snapshots.
5. API and Web: question list, answer and dismiss routes, the question card in
   chat and episode views, read-only cards for ended episodes.

Specs to update with the code: `docs/specs/providers-and-containment.md`
(command client), `docs/specs/conversations-episodes-and-watchers.md` (human
input, Experiment continuation, notifications), and
`docs/specs/auto-research-and-branch-merge.md` (orchestrator questions).

## Checks

- Both prompt bootstraps, and Experiment and orchestrator continuations,
  carry the rendered verb data; excluded roles do not.
- Live answer, answer racing turn end, fresh-id polling under one deadline,
  duplicate submissions, and a repeat with other arguments.
- SSH disconnect and restart recovery; Stop and exhaustion.
- Immutable authority bindings (the regression test above).
- Notification deduplication.
- Served-app journeys for the card in chat and in an episode.
