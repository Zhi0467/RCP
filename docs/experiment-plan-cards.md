# Experiment plan cards

Design for agreeing on an Experiment's design before it runs. This file is
deleted before merge. Its rules move into the owning specs listed at the end.

## Problem

A Work chat starts an Experiment the moment the member asks. The human never
sees the plan as one reviewable thing, and agents mix the per-run handoff into
the node. Agents also run long scientific work as a launch, watch, debug loop
inside a chat, with no agreed design, budget, Stop or report.

## Goal

- No agent chat starts a fresh Experiment before a human presses **Start** on
  a plan card they could review, comment on and edit.
- Continuations of an episode a card started (resume, add turns, stop) stay
  with the chat, as today. The human already agreed to that episode.
- Agents keep running small work themselves, without a card.
- Reuse the asked approval card, the reply comments and the Work start. Add
  no parallel path.

## Extend X to Y

Extend `episode --kick-off-experiment` from "start now" to "raise a plan card
when the turn lands". Extend the asked approval card with one action kind,
`experiment_start`, that works in every chat that can start an Experiment.

## Routing rule (agent guidance)

The Work prompt states it once, with no project examples:

> Start an Experiment for work that answers a research question and needs
> more than one shot: runs that iterate, need debugging, take hours, or could
> change what the project believes. The Experiment agent carries it from start
> to finish. Do everything else yourself in this chat: infrastructure and
> tooling of any size, and short one-shot checks, such as a smoke test, a pilot
> that decides whether a study is worth planning, or a quick measurement that
> answers the human's question. Launch it, arm a watch, and report.

Guidance only. Nothing caps a direct launch. Any route may record Evidence.

## Kickoff

`episode --kick-off-experiment --node <id> --goal-file <file>` keeps its
arguments and key. It validates only the arguments: a non-empty goal, and
`--invocation-limit` at least 1. It records the request on the turn's
`work_experiment_kickoff` receipt with `"phase": "card_pending"` and returns
disposition `card_pending`. It starts nothing.

The node may be written in the same turn. Graph edits land when the turn
ends, so the card is created then, from the settled graph, through the same
post-turn hook that schedules Work resumes
(`_schedule_work_experiment_resume` / `start_scheduled_experiment_resumes`).
When the turn succeeds and its Patch is applied:

- If the node exists on the chat's graph target and is an Experiment, RCP
  creates the card (below). Kickoff requires both `member_work_chat_turn` and
  `work_ask_authorized`, so `question_origin` accepts the turn; a turn that
  fails either is refused at kickoff with `experiment_card_unavailable`.
- Otherwise RCP creates no card and records a task event with the reason.
  The agent sees it in its next turn's loop status.

A failed turn creates no card.

Replay: a receipt from before this change that names an `episode_id` still
returns that episode as `existing`. A `card_pending` receipt replays as
`card_pending` and never creates a second card.

## The card

The card is a chat question with a pinned `ExperimentStartAction`, created
with `question_origin` and `create_or_get_question` on the raising turn. The
question key derives from the kickoff key.

`ExperimentStartAction` joins the `ApprovalAction` union:

| Field | Value |
| --- | --- |
| `kind` | `"experiment_start"` |
| `project_id`, `graph_target` | the chat's |
| `node_id` | the Experiment |
| `goal` | goal file text |
| `invocation_ceiling` | the kickoff limit, else the node's limit |
| `provider`, `model`, `reasoning` | the agent's pick, else null |
| `run_truth_scope` | the raising turn's |

`experiment_start` is excluded from `THREAD_CARD_KINDS`. `ask --action` with
it is refused with `experiment_start_uses_episode`, like
`AUTO_RESEARCH_USES_EPISODE`. Kickoff is the only creator.

Pinned facts: the node id, that it is an Experiment, and the graph target.
They never include the node's revision, so a later Patch or Sync shows on the
card live without making it stale. The tap's `node_rev` check (below) guards
against starting a plan the human did not see.

A newer card for the same node in the same chat settles the older pending one
as `stale`.

### Outcomes

The existing `ApprovalOutcome` set, with new button labels:

| Event | Outcome |
| --- | --- |
| Start | `approved`, result carries `episode_id` |
| Don't run this now | `declined` |
| Newer card for the node in this chat | `stale` |
| 72 h without a tap | `expired` |
| Start refused (not ready, live episode, profile not ready) | `failed`, reason shown; the agent may raise a new card |

Settlement uses the existing answer delivery, so each outcome reaches the
agent as today: steered into a running turn, else a follow-up turn under the
raising turn's authorizer. On `approved` the agent arms its node-status
watcher, as the current usage text says to after a start.

## Where cards appear and who acts

The card renders inline in the raising turn's line, inside that line's
`.chat-annotatable-answer` region, in private threads
(`AgentWindowTranscript`) and in shared and node chats (`AgentChatView`). It
is not docked in `ApprovalCardStack`.

- **See:** whoever can read the chat (`readable_comment_target`'s rule).
- **Act:** in a private chat, its owner. In a shared or node chat, any
  Patch-capable member with project write admission. One helper owns this
  rule: `experiment_card_actor(store, question, identity)` in
  `runs/approval_cards.py`.

What changes for this kind only:

- `_owned_card` uses `experiment_card_actor` instead of private ownership.
- `_asking_thread` and `approval_card_member` stay thread-only; kickoff does
  not use them.
- `_DUE_CARDS` and the open-card version trigger also cover chats that are
  not agent threads, so shared and node chats refresh and expire cards.
- `AgentChatView` stops filtering out card questions of this kind and renders
  them in the raising turn's line.
- Needs-you notification: the chat owner in a private chat. A shared or node
  chat sends none; the card shows in the chat and its unread state.

Other card kinds keep their current scope.

## Card contents

`ApprovalCardShell` gains label props and an always-open body for this kind.
Top to bottom:

1. Node title and node chip.
2. **Design**: the node's `design`, rendered with the drawer's markdown.
3. **Done when**: the node's `completion_criteria` as a list.
4. **This run**: the goal, editable in place.
5. Agent chip (`AgentConfigChip`) and turn limit, editable.
6. **Don't run this now** and **Start**.

Design and Done-when have an edit icon that opens the node's existing **Edit
node**. That stages into the member's Sync draft like any node edit.

## Start

Start and Don't run this now go through the existing tap route
`POST /api/approval-cards/{question_id}`
(`resolve_approval_card`), which claims the card once, checks
`pinned_sha256`, executes, settles and reconciles delivery.

`ResolveApprovalBody` gains an optional `experiment_start` object, accepted
only for this kind and outside the fingerprint:

| Field | Rule |
| --- | --- |
| `goal` | non-empty, at most the goal file limit |
| `invocation_ceiling` | at least 1, at most the node's limit |
| `provider`, `model`, `reasoning` | a ready profile for the tapping member |
| `node_rev` | the node `updated_rev` the card showed |

`_resolve` passes `body.experiment_start` to `_execute`. The overrides
replace the action's goal, limit and profile. After the pin check and before
`_run`, `_resolve` compares `node_rev` with the node's current `updated_rev`;
a mismatch raises `_NotNow(409, {"code": "plan_changed"})`, so the card stays
pending and the human re-reads before starting. Their own Sync counts too:
Sync, then Start.

The web refuses Start while the member has staged changes on the card's
graph target, with the same message Runs shows. No Sync runs inside Start.

`_execute` gets an `experiment_start` branch. It calls the Work start
(`start_work_experiment`, generalized to take the authorizer) with:

- the tapping member as `authorized_by`
- `started_from_operation_id` = the raising turn
- the chat id derived from the episode id, as today
- the card's `request_id` as the idempotency key

The episode id derives from the card's `request_id`, so a repeat or resumed
tap names the same episode, and `start_work_experiment` is already idempotent
on it. `_request_receipt` gets an `experiment_start` branch that reads
`store.episode(derived_id)`, so an interrupted tap settles from the episode.

`experiment_start` joins the `_admit` kinds that skip
`project_write_admission`, because the start takes its own operation lock and
that lock is not reentrant.

The branch maps `ValueError`, `ExperimentNotReady` and
`AgentTaskAdmissionConflict` to `HTTPException` with the message, as the run
route does, so a refused Start settles `failed` with the real reason.

The episode records the tapping member as its authorizer. Its root names the
raising turn, so Runs shows **Started from** that chat, and that chat's later
turns may resume, extend or stop it.

## Authority

Invariant 3 wording changes from "the member's ordinary Work chat starting an
Experiment they asked for" to:

> So does the member's ordinary Work chat raising an Experiment plan card, and
> resuming, extending or stopping an episode a human started from that card.

A fresh Experiment start always needs a human tap. Auto-research starts from
chat and the orchestrator exception are unchanged.

## Prompt

- Kickoff usage: the command raises a plan card that appears when the turn
  ends; the run starts only on a human's Start; write and fix the node in the
  same turn as the kickoff; arm the node watcher after learning the card was
  approved. Keep the goal versus node-fields rule.
- The routing rule above.
- Bump `WORK_POLICY_VERSION`.

## Checks

- Kickoff returns `card_pending`, starts nothing, and replays the same.
- A node written in the kickoff turn gets its card after the Patch lands; a
  failed turn gets none; a non-Experiment node gets none and an event.
- A pre-change receipt with an `episode_id` still replays `existing`
  (old-writer regression).
- `ask --action experiment_start` is refused.
- A second kickoff for the node settles the first card `stale`.
- Start in a private chat: owner starts it; another member gets 404.
- Start in a shared chat: write member starts it; read-only member refused.
- Start with a moved node revision answers `plan_changed`, card stays pending.
- Start on a not-ready node settles `failed` with the reason.
- Overrides above the node limit are refused.
- An interrupted tap settles from its receipt; a repeat tap starts no second
  episode.
- Don't run this now settles `declined`; the agent receives it.
- Web: card in a thread and in a shared chat, edit goal/agent/limit, comment
  on card text and send, Start; desktop width and 390 px.

## Spec owners after merge

- `authority-and-proposals.md`: the card kind, its scope and tap rules.
- `conversations-episodes-and-watchers.md`: Work-started episodes, routing.
- `interface-and-visual-design.md`: card layout.
- `AGENTS.md`: invariant 3 wording.
