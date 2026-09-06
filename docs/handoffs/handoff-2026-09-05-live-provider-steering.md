# Live provider steering handoff

Date: 2026-09-05
Status: active, human-confirmed on 2026-09-05. Nothing is implemented. This
supersedes the deferral in
[Q8](../open-questions.md#q8--should-rcp-hold-live-provider-sessions-so-a-running-turn-can-be-interrupted)
for the modest version only: the human may send a message into the chat turn
they are watching, through the provider process RCP already owns for that turn.
No persistent session daemon is authorized. Phase 1 probes precede Phase 2
implementation, in one PR of their own.

## Evidence

Probed on 2026-09-05 against the installed binaries; re-probe before Phase 2
because app-server remains experimental.

- **Codex app-server, codex-cli 0.153.2.** The generated protocol schema defines
  `turn/steer` with `threadId`, `expectedTurnId`, `input[]`, and an optional
  `clientUserMessageId`. `expectedTurnId` is a precondition: the request fails
  when it is not the active turn. `turn/interrupt` with `threadId` and `turnId`
  also exists.
- **Claude Code 2.1.260.** `--input-format stream-json` is "realtime streaming
  input" and works only with `--print`.
- **RCP today.** One fresh process per turn. The app-server runtime keeps the
  process's stdin open for the whole turn; the Claude stream-json runtime closes
  stdin after the prompt; Codex exec has no inbound channel. Read
  [durable task lifecycle](../specs/providers-and-containment.md#durable-task-lifecycle),
  [provider runtime selection](../specs/providers-and-containment.md#provider-runtime-selection),
  and [mail and lifecycle notices](../specs/auto-research-and-branch-merge.md#mail-and-lifecycle-notices).

## Settled decisions

1. **Target: the running human-triggered chat turn.** Discuss or Work, sent by
   the human who is watching it. Episode worker turns are out of v1: the spec
   says the human messages the orchestrator, not a child, and steering a worker
   would need the steer recorded in episode lineage and surfaced to the
   orchestrator as a notice. That is the named follow-up. Agent-to-agent
   steering stays ruled out (Q8, Q9).
2. **No persistent session.** Delivery is possible only while RCP's own provider
   process for that exact task attempt is alive. Every agent is still either
   running a turn or asleep with durable state; the recovery ladder, Pause,
   Resume, Retry, and restart safety are unchanged.
3. **Runtime matrix.** Codex app-server: `turn/steer` with the recorded thread
   id and the active turn id. Claude: stdin stays open and the turn is launched
   with `--input-format stream-json`; a steer is one user message on that stream.
   Codex exec, including the pre-prompt fallback from app-server: unsupported;
   the control is disabled and says why. The recorded actual runtime decides.
4. **Delivery is fail-closed.** RCP refuses when the addressed attempt is not
   running or the turn id does not match. It never queues a refused steer as the
   next turn and never resends after a disconnect. The receipt is one of
   **delivered** (provider acknowledged), **refused** (with reason), or
   **unknown** (the write began and no acknowledgment arrived, as after an SSH
   drop or process exit).
5. **Record.** The steer is stored as the human's chat message, tagged with the
   task attempt and its receipt (invariant 11: it is a human message, not an
   answer). It carries no authority: it cannot upgrade Discuss to Work
   (invariant 10b) and changes no scope, graph target, budget, or permission.
6. **No hard interrupt in v1.** Stop keeps its graceful meaning; `turn/interrupt`
   is not wired.
7. **SSH.** The steer travels the same stdin pipe the app-server turn already
   uses through the SSH session. A dropped connection yields an unknown receipt.

## Phase 1: behavior probes

Use a throwaway project and `RCP_DATA_DIR` on a spare port. Never the human's
real data directory or running work. Record results in this section.

- **a.** App-server: send `turn/steer` during a long tool call. Record whether
  the input is applied at the next model step or interrupts the running command,
  the acknowledgment timing, and the response when sent after `turn/completed`.
- **b.** Claude: run `--print --input-format stream-json --output-format
  stream-json`, send a second user message mid-turn. Record when it is applied,
  whether stdin must stay open for the turn to continue, and the behavior after
  the result message. If Claude cannot apply mid-turn input, Claude becomes
  unsupported in v1 and the contract above does not change.
- **c.** Both over SSH through the existing wrapper, dropping the connection
  mid-steer, to confirm the unknown receipt and that nothing is resent.

## Phase 2: owners and file scope

- `src/rcp/providers.py`: a per-runtime steer capability and a `ProviderTurn`
  method that renders a steer or reports unsupported.
- `src/rcp/agents/codex_app_server.py`: the `turn/steer` request, active turn
  id tracking, and response-to-receipt mapping.
- `src/rcp/agents/launcher.py`: an inbound writer for a running attempt, stdin
  kept open when the runtime supports steering, and receipt events.
- Task engine and API: one route that steers an exact task attempt, persistence
  of the message and receipt, and the disabled state in the projection.
- Web: steer input while the turn runs and receipt display. The disabled reason
  comes from the backend; `web/src/types.ts` seals the receipt state.
- Specs in the same PR: durable task lifecycle and runtime selection gain the
  steer channel; conversation human input gains the record; the mail section's
  "nothing is injected into a live provider process" is narrowed to notices and
  mail; Q8 is closed or rewritten to point here.

## Acceptance

This is a new durable cross-module promise, so it needs one acceptance scenario
at the next free number. It must cover: a steer applied on app-server locally and
over SSH; refusal after completion; an unknown receipt after a disconnect with
nothing resent; the disabled control on exec; the stored message and receipt;
and an app restart during a turn leaving the recovery ladder unchanged.

## Closure condition, all of it

1. Phase 1 results are recorded above.
2. The acceptance scenario is confirmed and passing, and the specs and Q8 are
   updated.
3. One real steer was exercised locally and over SSH, with receipts in this file.
4. This handoff is archived in the same PR that completes item 3.
