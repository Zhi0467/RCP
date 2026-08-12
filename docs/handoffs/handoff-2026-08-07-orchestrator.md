# Handoff — the orchestrator

**Date:** 2026-08-07
**State:** scope and the rulings marked *decided 2026-08-07* are confirmed by the
human. Everything not marked decided is proposed. Two acceptance scenarios are
written but not yet human-confirmed, and no code exists.

**Updated 2026-08-09:** the team-space profile model replaces the original
agent-actor ownership. The Decision-action split distinguishes ordinary agents
from the human-authorized project orchestrator; it does not remove the
orchestrator's direct Decision choice. The authority line is operation-specific,
not a division of whole node layers: the orchestrator may create new
ResearchQuestions and Hypotheses and has full control of every other graph node
type, while changes to existing ResearchQuestions and Hypotheses require
producer-separated Proposals.

**Order in the program:** piece 3 of 3. It depends on both the
[permission design](../design/identity-permissions-and-agent-profiles.md) (for
its elevated profile) and
[graph-condition wake](handoff-2026-08-07-graph-condition-wake.md) (because its
dispatch decisions are graph-driven). Do not start it before those land.

Read [`AGENTS.md`](../../AGENTS.md), then the blueprint's
[Experiment control and watchers](../research-control-panel-blueprint.md#experiment-control-and-watchers)
section, then this file.

---

## 1. What it is

One project-owned orchestrator profile, active through a bounded campaign task,
pushes research forward within a campaign scope the human authorized. In the UI
this is the **auto-research** action: the human sets the scope and a budget,
presses it, and the orchestrator conducts the campaign until the budget runs
out, it is stopped, or it needs new authorization. It may leave protected
epistemic changes in the Inbox without pausing otherwise independent work.

Its powers:

- **spawn / stop / pause** node agents and Experiment loops;
- **message** them — turn-level mail, star topology only;
- **set graph wake conditions**; and
- **conduct and structure the research**, including direct Decision choice,
  full control of Experiments, Blockers, and Evidence, direct creation of new
  ResearchQuestions and Hypotheses, and Proposals for changing existing ones.

## 2. The authority line

The orchestrator may expand the research framing by creating new questions and
hypotheses. Once a ResearchQuestion or Hypothesis exists, the orchestrator may
change it only through a Proposal. Every agent-produced Proposal waits for a
human; neither the orchestrator nor an ordinary child approves one.

The earlier whole-layer split was wrong. Authority follows the semantic
operation, not the node's layer:

| Operation | Orchestrator authority |
|---|---|
| Create a new ResearchQuestion or Hypothesis in its normal unresolved initial state | direct |
| Change ordinary content, status, standing, or meaning-bearing relations of an existing ResearchQuestion or Hypothesis | Proposal |
| Remove an existing ResearchQuestion or Hypothesis | Proposal |
| Approve any agent-produced Proposal | forbidden; human only |
| Create, edit, judge, or remove Evidence | direct |
| Create, edit, decide, judge, or remove a Decision | direct |
| Create, edit, advance, judge, or remove an Experiment or Blocker | direct |

The permission boundary is therefore **new versus existing** for these two node
types. Ordinary editing of an existing question or hypothesis is protected just
as status, standing, relation, and removal changes are. The orchestrator can
raise those changes as Proposals and continue independent campaign work. A
human judges them. An ordinary child may independently produce another Proposal,
but that Proposal has the same human-only decision boundary; task or campaign
lineage never gives an agent approval authority.

These operations widen the future Proposal vocabulary beyond the current
ordinary-agent Hypothesis-status-only contract. They never reintroduce Decision
Proposals: the orchestrator decides Decisions directly.

Its direct operations take effect during the campaign. When the campaign wraps
up, the orchestrator must produce a detailed HTML report showing what Decisions
and Blockers were resolved, which Experiments ran, what Evidence was recorded,
which existing ResearchQuestions or Hypotheses changed through approved
Proposals, and which Proposals still await judgment. Correcting an already
effective operational action is a new action, not retrospective permission
retraction.

### The gate's meaning changes, deliberately

Experiment readiness condition 1 is *"every `governed_by` Decision is decided
with a selected option."* Because the orchestrator may decide Decisions, it can
satisfy that readiness gate itself inside the human-authorized campaign.

Two constraints follow, and both are load-bearing:

1. **Exactly one profile carries this.** There is one auto-research mode, not a
   family of elevated agents. Resist a second profile with "almost" the same
   authority; the moment two exist, the line stops being explainable.
2. **The budget is the enforced brake**, together with campaign scope. Since
   per-episode human approval no longer gates anything, budget enforcement is
   not bookkeeping — it is the safety mechanism.

### Seating scope is not authority scope

**Emphasized by the human, 2026-08-07 — do not conflate these.**

- **Authority scope** includes full direct control of Decisions, Experiments,
  Blockers, and Evidence, direct creation of new ResearchQuestions and
  Hypotheses, and Proposal creation for existing ones. Existing ResearchQuestion
  and Hypothesis records are never directly edited or removed by the
  orchestrator, and every Proposal it creates waits for a human.
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
| Who sets the budget, and at what granularity? | **One number for the whole campaign**, set when the human presses the button, defaulting to **10 invocations** from Settings. Not per-worker. | Per-worker ceilings force the human into capacity planning up front — exactly the work being delegated. |
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

**Decided 2026-08-12:** normal completion is one idempotent staged `finish`
command from the orchestrator. Quiescence is not completion: a campaign may be
quiet because its actors are asleep on mail or watchers.

Provider, network, rate-limit, and resumable session failures of the orchestrator
use the existing bounded backoff and exact-session Resume/Retry paths. Only an
unrecoverable orchestrator failure ends the campaign. Worker failures remain
visible work for the orchestrator rather than becoming campaign verdicts. A
terminal orchestrator failure fences admission, retires campaign watchers with
Stop-loop semantics, retains pending mail, and proceeds to a partial report.

Human controls are campaign-level only. Runs derives a structured recommended
action table from durable campaign and orchestrator state in the same style as
Experiment control; it never exposes an individual worker control or infers an
action from diagnostic prose.

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

1. **A per-turn credential.** The client must know which authorized campaign
   task is calling. Stage a token bound to the campaign, task, and turn, scoped
   to the project orchestrator profile, and expiring with the turn. Every
   invocation checks it. Without this the CLI is an authority hole, not an
   authority surface.
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
   bounded: RCP schedules **within a human-authorized campaign scope and a
   human-set budget**. The orchestrator may create and revise graph framing
   inside that scope; it may not expand its own campaign authorization.
2. **Q2's deferral.** *"Graph-level scheduling across the research frontier is
   still separately deferred"* ([open-questions.md](../open-questions.md)). This
   piece un-defers exactly that. Update or delete the sentence when it lands.

## 8. UI sketch

Nothing here needs a new destination, and the
[no-commentary-lines rule](../acceptance/S20-no-ui-commentary-lines.md) applies
throughout.

### Decided 2026-08-12

**Auto-research starts from the project header, beside Ask.** The action's scope
and its location agree: the campaign is project-wide, and it lives where the
project-wide actions live. A button on one ResearchQuestion that authorizes work
across the whole project would misstate its own reach at the moment of pressing,
which is the confusion the project-scope ruling exists to prevent.

The accepted cost is that the orchestrator starts with **no anchor** — no
particular question in view — so its first turn could be spent choosing what to
work on rather than working. The authorization dialog therefore accepts an
optional starting instruction, which gives the campaign an anchor without
misstating its project-wide scope or granting any authority.

**The budget is typed in invocations, with observed cost shown beside it.** The
enforced ceiling stays exactly `invocation_ceiling`, and the existing usage
ledger supplies what has actually been spent. The enforced number stays exact and
the legible number stays honest, at the price of two numbers on screen. Money as
the input unit was rejected: it needs drifting per-provider pricing hardcoded
somewhere, and a campaign halting because a price moved is a weaker guarantee
than a counted ceiling.

- **Runs** already nests children under a parent row, so a campaign is a parent
  with its workers as children and one shared budget meter on the parent. Its
  controls remain on that parent: a durable-state action table recommends Stop,
  exact-session recovery, reauthorization, or report review when each is safe.
  Workers remain inspection-only in the human UI.
- **The human can message the orchestrator.** This is the steering gesture, and
  it is what makes auto-research feel like delegation rather than a batch job.
- **The mail thread is inspectable** — ordered, attributed by worker and control
  node, read-only by default. Once agents talk, part of the *why* behind a graph
  change lives there; if it is not retained and readable, RCP loses the thing it
  exists to preserve.
- **DAG occupancy** — workers rendered on the nodes they hold. This is the actual
  control panel, and it reuses the existing pin/release grammar.
- **The completed campaign exposes a detailed HTML report** from its Runs record.
  This is the retrospective review surface; completed Experiments, Evidence,
  decided Decisions, and resolved Blockers do not enter the graph Inbox merely
  because auto-research touched them.

### Campaign-report skill — confirmed contract

RCP supplies a versioned campaign-report skill and requires the orchestrator to
use it for the final wrap-up. The skill tells the orchestrator how to turn the
campaign ledger, graph deltas, worker results, and pending Proposals into one
detailed HTML report. Because the report is a campaign contract, the skill is an
RCP-owned orchestration dependency rather than an optional project Settings
selection. It must cover at least:

- the campaign scope, initiator, budget use, and termination reason;
- Decisions made and their rationale;
- Blockers opened, resolved, or left unresolved;
- Experiments attempted and their outcomes;
- Evidence created or changed;
- new ResearchQuestions and Hypotheses;
- approved changes to existing ResearchQuestions and Hypotheses, with Proposal
  provenance; and
- still-pending Proposals and other concrete follow-up work.

The report is not a Patch, carries no graph authority, and does not determine
whether a campaign succeeded. It summarizes authoritative graph and task state;
the graph and task ledger remain the source of truth.

**Decided 2026-08-12: the report is a durable artifact, produced on every
ending.** Normal completion, budget exhaustion, human Stop, and failure all
produce one. It is captured at wrap-up and kept.

Both halves were chosen against a cheaper option, and the reasons should not be
relitigated. Durable rather than regenerable, because a campaign is a period of
time: a report rebuilt later against a moved graph describes something else, and
a record that changes is not a record. Every ending rather than clean completion
only, because *ran out of budget* and *I stopped it* are precisely the endings a
person needs explained — reporting only on success would stay silent exactly when
it matters.

The accepted cost is partial reports. An ending that was not clean produces a
report about an incomplete campaign, and it must read as one rather than as a
tidy summary of work that did not happen.

**Decided 2026-08-12:** the report is the campaign's concluding turn. After the
ending fence blocks new admission, RCP waits for every already-admitted child
turn to settle. It then spends the reserved unit by resuming the sole
orchestrator's exact native session and actor-owned stage, stages the required
versioned `campaign-report` official skill, and requires the exact output file
`campaign-report.html`. Only after those children have settled and that HTML
validates does the report become visible. Missing or invalid HTML enters the
bounded report-only correction ladder in the same allocation, session, stage,
skill, and output path, without repeating operational work. Rendering reuses the
existing sandboxed HTML boundary rather than inventing an unrestricted campaign
document surface.

## 9. Acceptance scenarios — confirmed

**Two scenarios, decided 2026-08-07.** Bundling them would make the cheap half
expensive.

- [S77 — Auto-research creates freely and proposes changes to existing epistemic nodes](../acceptance/S77-auto-research-stops-at-belief.md).
  Driver `pytest`. Owns the authority line, including the
  seating-versus-authority distinction.
- [S78 — One budget, one stop](../acceptance/S78-one-budget-one-stop.md).
  Driver `browser`. Owns budget accounting, exhaustion, Stop, and the client's
  idempotency and event-stream behavior.

Both scenarios were human-confirmed on 2026-08-12. S78 now records the project
header entry point, campaign parent row, one budget meter, campaign-level
recovery controls, and concluding report turn that the implementation must
drive in a browser.

## 10. Do not

- Do not build real-time streaming into this. It is deferred as Q8; both
  providers support it, and the reason to wait is RCP's lifecycle model, not
  provider capability.
- Do not build worker-to-worker mail. Deferred as Q9.
- Do not turn epistemic review into a blanket ban on epistemic work. The
  orchestrator may create new ResearchQuestions and Hypotheses, propose changes
  to existing ones, and exercise full authority over Evidence.
- Do not let the orchestrator directly modify or remove an existing
  ResearchQuestion or Hypothesis.
- Do not let any agent approve a Proposal. **Superseded 2026-08-09:** the
  earlier rule here let the orchestrator judge an eligible child-produced
  Proposal while barring it from approving its own. That did not bind, because
  the orchestrator writes the instructions for the child whose Proposal it would
  then approve. Every agent-produced Proposal now waits for a human; see
  [Identity, permissions, and agent profiles](../design/identity-permissions-and-agent-profiles.md#a-proposal-is-an-escalation-to-a-human).
- Do not widen graph Inbox membership to make campaign review possible; the
  detailed HTML campaign report owns that retrospective review.
- Do not add a second budget, a second stop, or a second wake path. Every one of
  those already exists and is durable; a parallel implementation would be a
  strictly worse copy.
