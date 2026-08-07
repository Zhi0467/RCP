# Handoff — the orchestrator

**Date:** 2026-08-07
**State:** scope and the rulings marked *decided 2026-08-07* are confirmed by the
human. Everything not marked decided is proposed. **No acceptance scenario has
been written or confirmed**, and no code exists.

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

### Seating scope is not authority scope

**Emphasized by the human, 2026-08-07 — do not conflate these.**

- **Authority scope** is the whole action layer, exercised freely and directly.
  The orchestrator edits Decisions, Experiments, and Blockers itself, including
  their status and `selected_option`, with no gate.
- **Seating scope** is the much narrower question of which nodes it may start a
  *worker agent* on: **Experiments and Blockers only** (see section 4).

The orchestrator does Decision work itself. It does not delegate a Decision to a
worker, because a Decision has no mechanically checkable exit condition — an
agent seated there would run until the budget died. Narrow seating is a
statement about where delegation terminates cleanly, not a reduction of the
orchestrator's own reach.

## 3. Budget and termination

Decisions taken 2026-08-07; do not relitigate.

| | Ruling | Why |
|---|---|---|
| Who sets the budget, and at what granularity? | **One number for the whole campaign**, set when the human presses the button, defaulting from Settings. Not per-worker. | Per-worker ceilings force the human into capacity planning up front — exactly the work being delegated. |
| Does the orchestrator's own turn spend a unit? | **Yes.** | One rule, no exceptions, no accounting bugs. Same reasoning as graph wakes always spending. |
| What happens at exhaustion? | **Exactly what `invocation_ceiling` already does.** Current turns finish, nothing new starts, the campaign sits in **Needs action**, the human may reauthorize. | A careful exhaustion semantics already exists and is durable; a second one would be a worse copy. |
| How many campaigns per project at once? | **One.** | Two orchestrators dispatching against one graph is the write-contention problem, and there is no scope-partition story. Easy to relax later, painful to retract. |

Everything spends from that one pot:

- the orchestrator's own turns;
- every worker turn it spawns;
- every wake — watcher, graph condition, or message.

No exceptions is what makes orchestrator↔worker ping-pong terminate by
exhaustion rather than by good behavior.

**Stop** generalizes **Stop loop** exactly: persist intent first, current turns
finish normally, valid patches still apply, existing and newly emitted watchers
retain as `stopped`, no new claim wins. Do not invent a second stop semantics —
reuse the one in the blueprint, which is already durable, idempotent, and
restart-safe.

## 4. Where it may seat a worker

**Decided 2026-08-07: Experiments and Blockers only.**

Both have a mechanically checkable exit. An Experiment already has its whole
bounded loop lifecycle; a Blocker is finished when it is `resolved` or
`superseded`. Decisions and ResearchQuestions have no such exit, so a worker
seated there would run until the budget died — which is not a design.

Re-read section 2's seating-versus-authority note before implementing this: the
orchestrator still edits Decisions directly. It simply does that work itself.

## 5. The mail channel — star topology only

`messages.json` is a third handoff file beside `patch.json` and `watch.json`,
with the same fail-closed clearing (invariant 10c) and the same atomic
all-or-none validation as `watch.json`.

**Only the orchestrator may address a worker.** Workers may reply to the
orchestrator. Worker-to-worker is out of scope and is documented as an open
question (Q9 in [`open-questions.md`](../open-questions.md)), because it only
becomes compelling in a multiplayer project.

**The human messages the orchestrator, never a worker directly** (decided
2026-08-07). Talking to a worker behind its manager's back desynchronizes the
orchestrator's model of what its workers are doing, and it has no way to notice.

Delivery reuses the wake machinery: durable, coalesced, atomically claimed,
budget-admitted, restart-safe. It needs a new continuation cause alongside
`watcher_wake` ([experiment_loop.py:29](../../src/rcp/runs/experiment_loop.py:29)).
It does **not** reuse the shell poller — RCP routes the message directly.

Two rules that keep the record honest:

- **Messages carry no graph authority.** They are Markdown prose.
  `patch.json` remains the only graph channel (invariant 4b).
- **Messages are hearsay.** A message may report intent and observation, but
  graph facts get read from the graph. Otherwise a worker acts on state that was
  never committed — or that Apply later rejected. Prompt contract, not
  machinery.

### No agent pauses

There is no blocking primitive, and there should not be — a blocked agent is a
held process burning context to do nothing. Every agent is either running a turn
or asleep with durable state. Waiting is declarative: a worker says what should
wake it (`watch.json`, a graph condition) and then **terminates**. The
orchestrator works the same way. Coordination is continuation-passing between
sleeping agents.

## 6. How the orchestrator acts: a staged agent client

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

`watch-graph` is **orchestrator-only**, decided by the human on 2026-08-07.
Experiment loops arm graph conditions through `watch.json` instead; the
reasoning is recorded in
[the wake handoff](handoff-2026-08-07-graph-condition-wake.md).

### The line: effects are commands, deliverables stay files

| | Channel | Why |
|---|---|---|
| `patch.json` | file | The turn's deliverable. Must survive interruption, be retained as task evidence, and be re-read by the recovery ladder (invariant 9). A crash mid-call would lose a submitted patch. |
| loop `watch.json` | file | Validated all-or-none — *one invalid item arms none* — and mandatory, where `[]` is meaningful. A sequence of commands cannot express that: the third call failing leaves the first two armed. |
| spawn / stop / message / watch-graph | command | Immediate, individually meaningful, and needs a reason *now* that the agent can act on. |

The real win is not ergonomics, it is **referential composition**: the
orchestrator runs `spawn`, reads the returned worker id, and uses it in the next
call. A file handoff forces it to predeclare every effect blind and defer all of
them to turn end — which for a dispatcher means one turn is one blind batch.

### Three requirements that make it safe

1. **A per-turn credential.** The client must know who is calling. Stage a token
   bound to (actor, task, turn), scoped to exactly that actor's profile from
   piece 1, expiring with the turn. Every invocation checks it. Without this the
   CLI is an authority hole, not an authority surface.
2. **A caller-supplied idempotency key on every mutating command.**

   The hazard is **RCP replaying the orchestrator's own turn**, not the
   orchestrator deciding to try again. A turn runs `spawn worker A`, the worker
   starts, and then the orchestrator's process dies. Resume or Retry re-runs
   that turn from the start and executes `spawn worker A` a second time. Without a
   key: two live workers, two native sessions, double budget spend, and a
   duplicate that may rerun operational work — the exact failure the blueprint
   works hardest to prevent.

   Note the dangerous case is the one where the **first spawn succeeded**. A
   spawn that genuinely failed is fine to repeat.

   Required semantics for `spawn --key K`:

   - no record for `K` → create the worker, record `K` → worker id, report
     **created**;
   - a record for `K` exists → return that worker id and its current state, report
     **existing**. Never create a second, and **never restart it**.

   This is deduplication, not recovery. The existing worker was never interrupted —
   it has been running the whole time. If the returned worker is genuinely paused
   or failed, the orchestrator acts on that with an explicit `resume`, `retry`,
   or `stop`. Folding "restart it if it looks dead" into `spawn` would put a
   side effect behind a call the agent believes is a no-op, which is how
   duplicated experiments happen.

   Same discipline as the Patch's source operation serving as an idempotent
   commit identity and the deterministic watcher identities — RCP already
   *reconciles* an interrupted handoff rather than re-applying it. Do not invent
   a different mechanism.

   Why the key comes from the caller: RCP cannot generate a stable one across a
   retry, because the retry is a fresh process with no memory of what the last
   attempt generated. The agent can, because it derives the key from its own
   intent, which is stable across attempts.
3. **Every invocation lands in the task event stream, recording start and exit
   separately.** File handoffs are auditable for free because the scratch folder
   is retained. Commands are not; History must show what the agent did and when,
   or the audit trail regresses.

### The record is the ledger

Requirements 2 and 3 are one mechanism. The event stream that makes commands
auditable is the same record that answers "has `K` already run," so do not build
a separate idempotency store beside it.

Recording **start and exit separately** is what makes the three real outcomes
distinguishable:

| Recorded | Meaning | Retry behavior |
|---|---|---|
| start + exit ok | the effect happened | return the existing result |
| start, no exit | **unknown** — the call may or may not have taken effect | reconcile against live state before answering |
| no start | never ran | execute normally |

The middle row is the one that only exists because start is recorded. RCP
already handles exactly this shape for a remote patch append, where a confirmed
commit succeeds, an absent commit rolls back, and an **unknown** commit is
quarantined until a refresh proves what happened. Follow that: an unknown call
is resolved by looking at whether the worker actually exists, never by guessing
from the log alone.

Feed the same record into the retry framing, so a resumed turn is told what it
already did rather than rediscovering it. That matches how RCP already hands a
retry its external-side-effect diagnostics and its short "only what changed"
follow-up.

**Keep the key as the enforcement anyway.** Telling the agent what it already
did is necessary but not sufficient: prompt compliance is probabilistic, and
this repo's pattern is mechanical enforcement wherever it is available, with
prompt-only boundaries named explicitly as the accepted exception (invariant 4's
canonical `.research` prohibition is the model). A duplicate live worker spends
real budget and may rerun an experiment, which is not a failure worth making
probabilistic. The key also makes intent-matching exact — the agent declares
that *this* is the same spawn — where matching on arguments alone is fuzzy the
moment two calls differ trivially.

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

## 7. Blueprint amendments this requires

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

## 8. UI sketch

Nothing here needs a new destination, and the
[no-commentary-lines rule](../acceptance/S20-no-ui-commentary-lines.md) applies
throughout.

- **Runs** already nests children under a parent row, so a campaign is a parent
  with its workers as children, one shared budget meter on the parent, and **Stop**
  as its only campaign-level action. Invocation-level Pause/Resume/Retry stay in
  the Agent task inspector, exactly as S72 promises for Experiment loops.
- **The human can message the orchestrator.** This is the steering gesture, and
  it is what makes auto-research feel like delegation rather than a batch job.
- **The mail thread is inspectable** — ordered, attributed by worker and control
  node, read-only by default. Once agents talk, part of the *why* behind a graph
  change lives there; if it is not retained and readable, RCP loses the thing it
  exists to preserve.
- **DAG occupancy** — workers rendered on the nodes they hold. This is the actual
  control panel, and it reuses the existing pin/release grammar.

## 9. Acceptance scenarios — written, not yet confirmed

**Two scenarios, decided 2026-08-07.** Bundling them would make the cheap half
expensive.

- [S77 — Auto-research runs the action layer and stops at belief](../acceptance/S77-auto-research-stops-at-belief.md).
  Driver `pytest`. Owns the authority line, including the
  seating-versus-authority distinction.
- [S78 — One budget, one stop](../acceptance/S78-one-budget-one-stop.md).
  Driver `browser`. Owns budget accounting, exhaustion, Stop, and the client's
  idempotency and event-stream behavior.

S78's **UI path is the least settled part of this whole program** — the
auto-research entry point, the campaign row, and the budget display have not
been discussed in enough detail, and the scenario says so. Per
[`AGENTS.md`](../../AGENTS.md) step 0, confirm both before implementation, and
expect S78's drive to change when the surface is actually designed.

## 10. Do not

- Do not build real-time streaming into this. It is deferred as Q8; both
  providers support it, and the reason to wait is RCP's lifecycle model, not
  provider capability.
- Do not build worker-to-worker mail. Deferred as Q9.
- Do not give the orchestrator epistemic authority, even temporarily "to unblock
  testing." The whole design rests on that line.
- Do not let the orchestrator change the framing — it may not edit a
  ResearchQuestion or create one.
- Do not add a second budget, a second stop, or a second wake path. Every one of
  those already exists and is durable; a parallel implementation would be a
  strictly worse copy.
