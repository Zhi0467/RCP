# Live provider steering — discussion draft

Status: draft planning PR. The human confirmed on 2026-09-05 that live steering
is desirable if it is feasible as a modest change to the current provider-call
model. No transport or lifecycle implementation has been selected or authorized.

## Confirmed direction

- Let a human send a message to steer the provider turn they are watching while
  it is still running: for example, "stop that approach; use the other dataset."
- Keep this work in its own investigation and implementation PR.
- Preserve the provider abstraction and both local and SSH execution.
- Do not assume this requires keeping provider processes alive between turns.
  An inbound channel during an existing turn and a persistent session daemon
  are different designs. Do not claim restart safety must be sacrificed.
- This is not worker-to-worker or cross-episode mail. Those remain out of scope.

## Current boundaries

Read [durable task lifecycle](../specs/providers-and-containment.md#durable-task-lifecycle),
[episode continuity](../specs/conversations-episodes-and-watchers.md), and
[Codex runtime selection](../decisions/2026-08-25-codex-app-server-runtime.md).
RCP currently uses a fresh Codex app-server process per provider turn, with
persisted native thread identity. Its Stop fence does not mean force-cancel an
in-flight turn. Ordinary messages do not currently inject input into that turn.

## Investigation required

1. Re-probe actual supported provider versions and official native protocols;
   do not rely on the old August CLI observations. Distinguish steering, queueing
   the next turn, interruption and cancellation for Codex exec, Codex app-server,
   and Claude. Demonstrate the actual timing and acknowledgment for each.
2. Trace the concrete local and SSH invocation paths, input/output ownership,
   process lifetime and disconnect recovery before selecting a transport.
3. Define the exact addressed task/turn, authenticated sender, delivery receipt,
   and what happens when the turn finishes while the message is in flight.
4. Decide whether and how a steering message may alter the scientific goal while
   retaining the original episode budget, graph target and write scope. A
   message must never upgrade Discuss to Work or widen provider permissions.
5. Make unsupported delivery and uncertain acknowledgment explicit. Do not
   silently call next-turn queueing live steering or resend a possibly delivered
   message after a disconnect. Decide the modest version first; a persistent
   session daemon requires a separate human decision.

## Closure target

First return an evidence-backed feasibility report and bounded proposed design
for human discussion. If approved, confirm acceptance covering real local and
SSH steering, task identity races, failure/unsupported cases and restart safety.
The draft does not authorize production probes that send messages to real work,
provider login, credentials changes or broad runtime redesign.

## Suggested skills

Use `grilling` to settle the delivery contract and `openai-docs` to verify current
Codex protocol support. Archive this draft once completed or rejected; confirmed
behavior then belongs in the existing provider and episode specifications.
