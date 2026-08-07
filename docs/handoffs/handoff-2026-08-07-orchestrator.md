# Handoff — the orchestrator

**Date:** 2026-08-07
**State:** scope confirmed by the human in a design conversation. The detailed
contract below is **proposed, not confirmed**, and **no acceptance scenario
exists yet**. No code has been written.

**Order in the program:** piece 3 of 3. It depends on both
[actor identity](handoff-2026-08-07-actor-identity-and-permissions.md) (for its
elevated profile) and
[graph-condition wake](handoff-2026-08-07-graph-condition-wake.md) (because its
dispatch decisions are graph-driven). Do not start it before those land.

Read [`AGENTS.md`](../../AGENTS.md), then the blueprint's
[Experiment control and watchers](../research-control-panel-blueprint.md#experiment-control-and-watchers)
section, then this file.

---

## 1. What it is

One agent actor, seated at project scope, that pushes research forward under a
framing the human fixed. In the UI this is the **auto-research** action: the
human sets the framing and a budget, presses it, and the orchestrator works the
action layer until the budget runs out or it needs a human decision.

Its powers:

- **spawn / stop / pause** node agents and Experiment loops;
- **message** them — turn-level mail, star topology only;
- **set graph wake conditions**; and
- **act on the action layer** of the graph with elevated authority, despite
  being an agent identity.

## 2. The authority line

The human owns **what we ask and what we believe**. The orchestrator owns
**what we do to find out**.

That is not a new axis. It is the `layer` field already declared per relation in
[`RELATION_SPEC`](../../src/rcp/core/models.py:288):

| Layer | Nodes | Who |
|---|---|---|
| epistemic | ResearchQuestion, Hypothesis, Evidence→Hypothesis edges | human |
| action | Decision, Experiment, Blocker | orchestrator |
| seam | `tests`, `produces` | the handoff between them |

So the orchestrator may set Decision status and `selected_option`, Experiment
status, and Blocker status directly. ResearchQuestion and Hypothesis status
transitions remain human, reached through the two existing Proposal shapes —
unchanged, not widened.

Evidence remains freely creatable by agents, as today. The seam relations are
literally where agent action authority hands off to human epistemic authority,
which is a satisfying place for the line to sit and worth saying out loud in the
blueprint.

### The gate's meaning changes, deliberately

Experiment readiness condition 1 is *"every `governed_by` Decision is decided
with a selected option."* Once the orchestrator can decide Decisions, it
satisfies its own readiness gate.

**Confirmed by the human on 2026-08-07: that is the point.** Judging when a gate
should pass *is* the orchestrator's job, and auto-research is the one mode that
may hold that judgment. This is not a caveat to work around — do not add a
compensating check that re-gates the orchestrator behind a human step.

Two constraints follow, and both are load-bearing:

1. **Exactly one profile carries this.** There is one auto-research mode, not a
   family of elevated agents. Resist a second profile with "almost" the same
   authority; the moment two exist, the line stops being explainable.
2. **The budget is the enforced brake**, together with framing authority. Since
   per-episode human approval no longer gates anything, budget enforcement is
   not bookkeeping — it is the safety mechanism.

## 3. Budget and termination

- One **campaign budget in invocations**, shared by the orchestrator and every
  seat it spawns.
- Orchestrator `dispatch` spends from the same pot. It cannot mint capacity.
- Every wake spends — watcher, graph condition, or message. No exceptions, which
  is what makes orchestrator↔seat ping-pong terminate by exhaustion rather than
  by good behavior.
- **Stop** generalizes **Stop loop** exactly: persist intent first, current turns
  finish normally, valid patches still apply, existing and newly emitted
  watchers retain as `stopped`, no new claim wins. Do not invent a second stop
  semantics — reuse the one in the blueprint, which is already durable,
  idempotent, and restart-safe.

## 4. The mail channel — star topology only

`messages.json` is a third handoff file beside `patch.json` and `watch.json`,
with the same fail-closed clearing (invariant 10c) and the same atomic
all-or-none validation as `watch.json`.

**Only the orchestrator may address a seat.** Seats may reply to the
orchestrator. Seat-to-seat is out of scope and is documented as an open question
(Q9 in [`open-questions.md`](../open-questions.md)), because it only becomes
compelling in a multiplayer project.

Delivery reuses the wake machinery: durable, coalesced, atomically claimed,
budget-admitted, restart-safe. It needs a new continuation cause alongside
`watcher_wake` ([experiment_loop.py:29](../../src/rcp/runs/experiment_loop.py:29)).
It does **not** reuse the shell poller — RCP routes the message directly.

Two rules that keep the record honest:

- **Messages carry no graph authority.** They are Markdown prose.
  `patch.json` remains the only graph channel (invariant 4b).
- **Messages are hearsay.** A message may report intent and observation, but
  graph facts get read from the graph. Otherwise a seat acts on state that was
  never committed — or that Apply later rejected. Prompt contract, not
  machinery.

### No agent pauses

There is no blocking primitive, and there should not be — a blocked agent is a
held process burning context to do nothing. Every agent is either running a turn
or asleep with durable state. Waiting is declarative: a seat says what should
wake it (`watch.json`, a graph condition) and then **terminates**. The
orchestrator works the same way. Coordination is continuation-passing between
sleeping agents.

## 5. How the orchestrator acts: a staged agent client

Confirmed direction, 2026-08-07. The orchestrator's verbs are exposed as
**commands**, not as more handoff files.

This is not a new mechanism. RCP already stages an executable client into the
run workspace and gives the agent its exact command: the live Patch validator,
which exchanges bounded request/response files through the workspace while RCP
polls locally or over the existing SSH run stage, and whose exit values
distinguish valid, invalid, and unavailable. **Generalize that client** rather
than inventing a second channel — it already works remotely without RCP
installed on the execution host, and it is version-matched to the run.

Verbs: `spawn`, `pause`, `resume`, `stop`, `message`, `watch-graph`, plus the
existing `validate` and a `status` query.

### The line: effects are commands, deliverables stay files

| | Channel | Why |
|---|---|---|
| `patch.json` | file | The turn's deliverable. Must survive interruption, be retained as task evidence, and be re-read by the recovery ladder (invariant 9). A crash mid-call would lose a submitted patch. |
| loop `watch.json` | file | Validated all-or-none — *one invalid item arms none* — and mandatory, where `[]` is meaningful. A sequence of commands cannot express that: the third call failing leaves the first two armed. |
| spawn / stop / message / watch-graph | command | Immediate, individually meaningful, and needs a reason *now* that the agent can act on. |

The real win is not ergonomics, it is **referential composition**: the
orchestrator runs `spawn`, reads the returned seat id, and uses it in the next
call. A file handoff forces it to predeclare every effect blind and defer all of
them to turn end — which for a dispatcher means one turn is one blind batch.

### Three requirements that make it safe

1. **A per-turn credential.** The client must know who is calling. Stage a token
   bound to (actor, task, turn), scoped to exactly that actor's profile from
   piece 1, expiring with the turn. Every invocation checks it. Without this the
   CLI is an authority hole, not an authority surface.
2. **A caller-supplied idempotency key on every mutating command.** A turn
   interrupted after `spawn` and then retried must not spawn twice. This is the
   same discipline already used for the Patch's source operation as an idempotent
   commit identity and for deterministic watcher identities — do not invent a
   different one.
3. **Every invocation lands in the task event stream.** File handoffs are
   auditable for free because the scratch folder is retained. Commands are not;
   History must show what the agent did and when, or the audit trail regresses.

### Do not

- Do not expose these verbs on the human `rcp` CLI. The staged client is a
  separate executable so a human cannot accidentally act as an agent, and so the
  remote host needs no RCP installation.
- Do not have the remote client call an HTTP endpoint. RCP is not assumed
  reachable from the execution host; use the existing mailbox-over-run-stage
  pattern.
- Do not hand-transcribe the client into a string literal. Ship one stdlib-only
  module's source, as `record_parsing.py` and the validator client already do —
  the copy nobody can test locally is the copy that rots.

## 6. Blueprint amendments this requires

Both must be made deliberately, in place, with a version bump — not discovered
inside an implementation.

1. **The non-goal.** *"RCP may dispatch work from that structure, but it does not
   become a scheduler"*
   ([blueprint](../research-control-panel-blueprint.md#purpose-and-product-boundary)).
   An orchestrator with `dispatch` crosses this. The honest amended form is
   bounded: RCP schedules **within a human-set framing and a human-set budget**,
   and never sets the framing itself.
2. **Q2's deferral.** *"Graph-level scheduling across the research frontier is
   still separately deferred"* ([open-questions.md](../open-questions.md)). This
   piece un-defers exactly that. Update or delete the sentence when it lands.

## 7. UI sketch

Nothing here needs a new destination, and the
[no-commentary-lines rule](../acceptance/S20-no-ui-commentary-lines.md) applies
throughout.

- **Runs** already nests children under a parent row, so a campaign is a parent
  with its seats as children, one shared budget meter on the parent, and **Stop**
  as its only campaign-level action. Invocation-level Pause/Resume/Retry stay in
  the Agent task inspector, exactly as S72 promises for Experiment loops.
- **The human can message the orchestrator.** This is the steering gesture, and
  it is what makes auto-research feel like delegation rather than a batch job.
- **The mail thread is inspectable** — ordered, attributed by seat and control
  node, read-only by default. Once agents talk, part of the *why* behind a graph
  change lives there; if it is not retained and readable, RCP loses the thing it
  exists to preserve.
- **DAG occupancy** — seats rendered on the nodes they hold. This is the actual
  control panel, and it reuses the existing pin/release grammar.

## 8. Proposed acceptance scenario — needs the human's confirmation first

**"Auto-research runs the action layer and stops at belief."** Promise: pressing
auto-research spawns seats under one budget; the orchestrator decides Decisions
and resolves Blockers directly; any Hypothesis or ResearchQuestion movement
arrives in the Inbox as a Proposal rather than being applied; the budget bounds
total spend across every seat and message; and **Stop** finishes the current
turns without killing external work or discarding a valid patch.

Driver: `browser` for the Runs hierarchy, budget display, and Stop lifecycle;
`pytest` for the authority table, budget accounting, and stop durability.

This is a large scenario and may want splitting into an authority scenario and a
lifecycle scenario. Settle that with the human before building.

## 9. Do not

- Do not build real-time streaming into this. It is deferred as Q8; both
  providers support it, and the reason to wait is RCP's lifecycle model, not
  provider capability.
- Do not build seat-to-seat mail. Deferred as Q9.
- Do not give the orchestrator epistemic authority, even temporarily "to unblock
  testing." The whole design rests on that line.
- Do not let the orchestrator change the framing — it may not edit a
  ResearchQuestion or create one.
- Do not add a second budget, a second stop, or a second wake path. Every one of
  those already exists and is durable; a parallel implementation would be a
  strictly worse copy.
