# Agent turn messages

Extend the [live agent status](../specs/providers-and-containment.md#live-agent-status)
snapshot to carry the agent's finished in-between messages, so the Agent view shows
a turn's progress at message granularity. Checked on `origin/main` 68ae8de80 against
Codex 0.167.0 (app-server `MessagePhase` probed live) and Claude Code stream-json.

## Behavior

- While a turn runs, the Agent thread's live row shows the turn's steps, open:
  each in-between assistant message in markdown, and the turn's RCP commands as
  today, in order, above the spinner. There is no token or partial streaming;
  the existing 2 s task read carries them.
- When the turn settles, the reply shows as today and the same steps collapse
  into the turn's progress line. Its summary counts the shown messages
  (`2 messages · Ran 1 command · 3s`). Settling only collapses; it never reorders.
- Provider tool calls stay hidden. A turn whose agent writes no text between
  tools shows only the spinner, as today.
- The transcript keeps the reader's place across the swap, as it does for any
  reload: pinned to the bottom stays pinned, otherwise `readerAnchor` holds.

## Which text is an in-between message

Text becomes an in-between message only once the agent acts after it. The
provider runtime holds the latest top-level assistant text. The next top-level
item releases it as a message. Text still held when the agent answers is the
answer. This rule is structural, so no text comparison is needed. It holds for
steered turns, because each answer consumes only its own held text. It also
holds for Work corrections and retained answers, which never see the hold.

- **Claude** (`_ClaudeStreamTurn.receive_line`): an `assistant` line with
  `parent_tool_use_id` null and `type: "text"` blocks replaces the held text.
  Text comes independently of `usage`, and `thinking` blocks are never taken.
  Any later top-level assistant line releases the held text. An answering
  `result` clears it. A task-notice result leaves it held.
- **Codex app-server** (`CodexAppServer`): an `agentMessage` with
  `phase: "commentary"` is held, not answered. The next `item/started` releases
  it. `final_answer`, `partial_answer` and a missing phase stay `answer`, so
  older models keep today's reply. If `turn/completed` arrives with no answer
  sent this turn, the held commentary is sent as the `answer`.
- Codex exec, OpenCode and agy report none. Codex exec items carry no phase.

The release travels as `AgentStatusDelta.message: str | None`. It is display
only (invariant 4b), and every run path already forwards status deltas
(`runs/shared.py` labelled-event forwarding, SSH decode in `agents/launcher.py`,
`recorded_settlement.py` replay).

**Codex reply change:** Codex app-server commentary leaves the joined reply on
every surface that joins `outcome.answers`: Agent and project threads, Work,
Discuss, Space, auto-research, Experiment loop, graph and artifact-edit runs.
This matches Claude, whose in-between text was never part of its reply, and
`ProviderStreamEvent`'s rule that only the final assistant message is the answer.

## Storage

- `AgentTurnMessage` (`text`, `at`) lives in `providers/live_status.py` beside
  `AgentStatus`. `AgentStatus.messages` defaults to empty.
- `fold_agent_status` appends one message and keeps the newest
  `AGENT_STATUS_MESSAGE_LIMIT` (20). Text over `AGENT_STATUS_MESSAGE_MAX_CHARS`
  (4000) is cut and ends with `…`. Both limits live in `limits.py`.
- `_LiveStatusWriter` stamps `at` with `store.now()` and treats a message as
  urgent, adding it to `urgent` in `observe`. `settle` leaves `messages` as they
  are. A recovered turn's replayed deltas arrive together, so its messages
  order by arrival, after its commands.
- The snapshot stays the owner because it is display only and never exported
  or replayed. The turn event timeline is in the transfer schema
  (`transfer/records.py`), and its message rows collapse whitespace.
- Only the task detail (`GET .../tasks/{operation_id}`) carries `messages`. The
  task list rows and the chat transcript join (`api/chats.py`) copy
  `agent_status` with `messages=[]`. Both the live read and the lazy turn
  detail already use the detail route.
- A requeued operation starts its live status afresh (`mark_agent_task_running`,
  unchanged). After Stop and Continue, the fold shows only the resumed run's
  messages. The spec states this.

## Web

- `web/src/core/types.ts`: `AgentTurnMessage`, plus a required
  `AgentStatus.messages`.
- `agentThreadModel.ts`: a pure `turnSteps(task)` merges `agent_status.messages`
  with paired command events by time. It replaces `turnCommands` at its call
  sites. The summary names the parts that exist: commands only reads as today,
  messages only reads `N messages`.
- `TurnProgress` renders `turnSteps`, collapsed when settled and open while
  live. It returns null only when there are no steps. Messages render with
  `MarkdownAnswer`, using the props the transcript passes.

## Checks

- `tests/test_launcher.py::test_only_the_final_assistant_message_is_an_answer`
  is extended for Claude hold/release/clear (a text-only line without usage,
  subagent text excluded, a steered pair of results) and for the Codex phases.
- Codex app-server fixtures use recorded 0.167.0 `item/completed` phases:
  commentary then final, and a commentary-only turn whose commentary becomes
  the answer.
- `tests/test_agent_live_status.py`: fold cap and cut, the urgent write,
  `settle` keeps messages, and list and transcript rows omit messages while the
  detail keeps them.
- `web/tests/agentThreads.test.mjs`: `turnSteps` ordering, including equal
  timestamps, and the parts of the summary.
- Live drive on throwaway data and a spare port: one real Claude and one real
  Codex app-server turn. Screenshots of the live row, the settle transition,
  and the folded line, at desktop and 390px.

## Specs to update before merge

- `providers-and-containment.md` Live agent status: the hold rule, which
  runtimes report messages, the Codex commentary change, and requeue.
- `api-web-and-desktop-projections.md`: only the task detail carries messages.
- `interface-and-visual-design.md`: the live steps and the folded line.
