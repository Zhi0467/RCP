# Open questions

Design questions that are **raised and evidenced but not decided**. This file is
deliberately not the blueprint: the blueprint records decisions, `docs/acceptance/`
records promises, and this file records what is still genuinely undecided.

An entry stays here until it is either decided — at which point the canonical
blueprint is updated in place and the entry is deleted here — or ruled out. Keep
the evidence with the entry, so the next person does not re-derive it.

---

## Q4 — Who authors glossary terms after the standalone Glossary surface is removed?

**Status:** open. Raised 2026-08-03. No decision.

Existing glossary entries remain useful as inline hover definitions, but the
researcher-facing glossary table and ontology editor are being removed. This
does not decide whether future terms are authored by graph agents, derived from
node prose, staged by a human correction, or omitted entirely.

Until that authority and lifecycle are decided, inline glossary support only
renders entries already present in canonical history. It adds no creation,
editing, or deletion path.

---

## Q2 — What belongs in Control v2 after completion-only watchers?

**Status:** open. Raised 2026-08-01. V1 boundary decided; output-streaming and
other v2 details are not.
**Governing section:** [Experiment control and watchers](research-control-panel-blueprint.md#experiment-control-and-watchers).

### Decided boundary

V1 watchers are restart-durable observations. They do not stream live output,
interpret outcomes, own external work, or carry experiment-attempt semantics.
Generic Work keeps its strict three-field observer list and manual **Stop
watching**. Experiment-loop handoffs may additionally retire a staged compatible
observer through a reasoned file disposition after the agent has settled the
external work with its existing Work tools; that is not a general cleanup
primitive or proof that RCP cancelled a job. Experiment observers use durable
backoff and immutable groups: a group wakes once when no member remains active
and every remaining member is complete or persistently unobservable. A
persistently unobservable member remains degraded with an unknown outcome. V1
adds no stale-record cleanup primitive and no hard repository lease.

### Main Control v2 goal

Add wake-on-new-output while watched work is still running. Follow OpenClaude's
file-backed output plus durable-offset shape rather than holding logs in memory.
The unresolved output-delivery contract is:

- how a watcher requests completion delivery, output delivery, or both;
- what constitutes a deliverable output delta;
- how repeated wakes are batched or debounced;
- when offsets advance relative to queued and delivered turns;
- how restart recovery avoids both dropped and repeated output;

### Direct graph manipulation (v2)

The human wants to drag between nodes to create an edge, choose its relation
type, and approve, refute, or delete an edge in place. This fits how the app
already treats human corrections as literal edits rather than agent requests.

Keep it separate from Experiment belief acceptance. Direct manipulation is UI
authority; belief acceptance is the settled Proposal path described in the
canonical blueprint. Building the first must not silently alter the second.

### Secondary v2 lifecycle questions

- When, if ever, are permanently degraded or abandoned watcher records cleaned
  up? The rows are cheap, survive app close, and need no v1 user-facing cleanup
  action. Experiment agents may retire only scoped staged observers they have
  already settled; this does not decide broader retention policy.
- Does experiment control eventually need an enforceable repository lease, or is
  the v1 advisory active-loop marker enough? Human authority must remain explicit
  either way.

Graph-level scheduling across the research frontier is still separately deferred.
It is not part of this question merely because both features use the word
"control."

---

## Q5 — Should graph-writing agents be required to run an executable scanner?

**Status:** open. Raised 2026-08-03. No decision.
**Governing scenarios:** [S59](acceptance/S59-staged-graph-audit-skills.md) and
[S64](acceptance/S64-project-skill-workflow-selection.md).

Settings-owned package selection, immutable staging, compact context pointers,
and package receipts already ship under S64. What remains undecided is whether
RCP should add a `graph-scanner` package with an executable advisory check and
require a graph-writing agent to invoke it before finishing its initial Patch.

The proposed scanner would report structural quality problems such as likely
misattachments, duplicates, unexplained jargon, and unusually flat graph
regions. It would write only a bounded scratch report, never another graph
channel. Missing or unavailable execution would remain visible but would not
change the semantic validator's verdict or consume a correction round.

The decision is whether this prompt-enforced advisory step is worth its package,
receipt, protection, and execution machinery given that the uniform live Patch
validator already enforces graph correctness. S59 retains the proposed detailed
contract and acceptance drive; it does not authorize implementation until this
question is decided.

---

## Q6 — Should RCP host research-data views as a surface of its own?

**Status:** open. Raised 2026-08-06. No decision. Two candidate designs already
ruled out — see below, and do not re-derive them.
**Related:** [Q7](#q7--which-domains-can-rcp-serve-and-where-must-it-link-instead-of-host)
decides *for whom* this would be worth building; this entry decides *what* it is.

### The question

Every projection RCP currently offers is text-shaped: Research is prose paths,
DAG is nodes and edges, Paper is Markdown, Runs is operational rows, Inbox is
proposals. The one place data can be seen is the preview artifact, and invariant
10e deliberately makes those temporary, non-canonical, and expiring. RCP has
therefore already decided — by omission — that a view of one's own data is not
part of project truth.

The question is whether that decision is right, and if not, what the replacement
is: what a view is, where it lives, and what it is allowed to produce.

### What prompted it

A widely-circulated critique of scientific agents (Claude Science, Open Science,
Biomni), posted 2026-08-06 by someone who is both a developer and a benchtop
researcher. Its argument, compressed:

- These products are built AI-centered; research is data-centered.
- They are shaped like an IDE — chat in the middle, plots and code on the side.
  To see anything you must write code, intermediate states of an analysis are not
  visible without extra work, and saving figures to disk to open one by one is
  worse than the specialized software the researcher already has.
- The operations that actually matter — hand-picking features, deleting a few
  wrong segmentation masks — still require leaving for a domain tool.
- *"If I only use these agents for literature review, why not just use Claude
  Code?"*
- The stated ideal: the agent belongs backstage; visualization and interaction
  belong at the center; static matplotlib is not enough — the agent should write
  the researcher an interactive interface.

### What the complaint actually is

Not "the plots are ugly," and not provenance. It is **loop latency**: see the
data → form a hunch → try a transform → see the result → adjust. The tighter that
loop, the more turns a researcher takes, and the better the research goes. Agents
currently lengthen it, because their output is code that must be run and whose
result must be opened somewhere else.

Domain software is fast inside that loop and useless outside it; a general agent
is the reverse. The two halves live in different places, and that separation is
the cost.

### Two designs considered and ruled out

Recorded because both are attractive on first contact and both fail for reasons
that are not obvious until stated.

**1. Pre-declared decision request plus session capture.** RCP names the decision
it wants (`select_subset`, `edit_labels`, geometry, ranking, confirm), an agent
writes a launcher script that opens the domain tool with the right layers and
captures the result on exit, and the payload comes back as a patch. The payloads
really are tiny — a mask edit reduces to a list of label ids, and the pixels
never move.

It still fails, and not on process lifecycle. It assumes the human knows what
decision they are making before they open the tool, and that they make it once.
Real use is unbounded: open it, poke, revert, leave for two days, come back and
finish. Ten small edits become ten round trips through RCP. There is no "the
decision" available to declare up front, and no session worth keeping alive.

**2. File observation as the main line.** RCP watches artifacts the project
declares, and an agent-written pure differ turns two file states into a decision
record. This is strictly better plumbing than (1) — no process, no session, no
sleep-survival problem, and it collapses the scriptable/proprietary tool
distinction entirely, since only "the tool saves a file" matters.

But it answers RCP's bookkeeping problem, not the researcher's. It supplies
provenance after the fact for a complaint about not being able to see during.
Keep it as a candidate mechanism for the external tail; it is not the thesis.

### The unit, if RCP builds one

A view is **data binding + primitive + encoding + the decisions it can emit**.
The agent authors the encoding; RCP owns the other three.

Primitives, and where the host/link line falls:

| Primitive | Used for | Host? |
|---|---|---|
| Table (rows = entities, cols = attributes) | run tables, dataset stats, per-example predictions, eval results | yes |
| Series (ordered axis × value, overlaid traces) | loss curves, convergence, throughput | yes |
| Distribution (histogram, violin, ECDF) | label balance, length distributions, seed variance | yes |
| Matrix (2D grid of values) | attention, confusion, correlation, ablation pivot, spectra | yes |
| Projection (brushable point cloud) | embeddings, parameter-vs-metric, Pareto fronts | yes |
| Item grid / side-by-side | sample outputs, failure cases, prompt-completion comparison | yes |
| Diff (two structured objects) | config, code, prompt, output | yes |
| Node-link graph | computation graphs, architectures | Netron exists |
| Field / mesh (values over a spatial domain) | PDE solutions, simulation output | no — ParaView, VisIt |
| Timeline / trace (spans over time) | profiling, distributed timing, agent trajectories | no — Perfetto |

**The line runs through primitives, not through subfields.** Even inside CSE
there are entrenched viewers, and they cluster on exactly two shapes: values over
a continuous spatial domain, and long spans over time.

### What RCP would actually be contributing

Not rendering. **The data binding and the research context are already in hand.**
Every generic tool makes the researcher re-explain where the data is and what the
fields mean on each visit; RCP already knows what those forty runs were, which
hypothesis each tested, and which one the human called anomalous last Tuesday.

That saved re-explanation *is* the loop latency, and it is the only honest answer
to "why not just use Claude Code."

### What blocks a decision

1. **Unmeasured core assumption.** The whole design rests on an agent authoring a
   usable view in seconds and then *amending it in place* rather than
   regenerating it. Nothing in the repo does this today and no measurement exists.
   If this is slow, none of the rest matters.
2. **Durable or disposable — the criterion reversed itself.** The first sketch
   wanted a durable, replayable view spec stored in a patch. Loop latency argues
   the opposite: intermediate views should be cheap and thrown away, which is
   precisely the "saving figures one by one" the critique names. A split
   (disposable by default, explicit promotion for the ablation table that goes in
   the paper) is plausible but undecided, and promotion collides with invariant
   10e.
3. **Where it lives.** A new primary destination contradicts the recorded
   preference that the visible projections are Research and Runs. Attaching views
   to nodes and runs instead preserves that, but reproduces the scattering the
   critique is about — data with no stage. Unresolved.
4. **What the action bar emits.** Selecting six runs and calling them evidence is
   a human authority action, so it is legal under invariant 3. Whether it asserts
   directly or creates a Proposal follows from invariant 10b's narrow gating rule
   and has not been checked against it.

### Do not do in the meantime

- Do not build a launcher-and-capture path, and do not add a per-domain
  connector; both are ruled out above.
- Do not promote preview artifacts into canonical state to "make views durable."
  That is a change to invariant 10e and is question (2), not an implementation
  detail.
- Do not add a generic dashboard. Profiling, GPU utilization, scalar browsing and
  sweep panels answer *is my machinery working*, not *what did I learn*; they have
  incumbents and are out of scope whichever way this goes.

### Cheapest probe, when this is taken up

Table, Series, and Diff only, bound to runs, with one action: select rows →
evidence, provenance recording that a human chose them. Three primitives share
one data binding and are enough to measure question (1), which gates everything
else.

---

## Q7 — Which domains can RCP serve, and where must it link instead of host?

**Status:** open. Raised 2026-08-06. No decision.
**Related:** [Q6](#q6--should-rcp-host-research-data-views-as-a-surface-of-its-own).

### The question

RCP says it targets CSE researchers. That has been a statement about who the user
is, not a predicate anything can be tested against — so it cannot settle whether
a given domain, primitive, or feature request is in scope.

### Proposed predicate

> Where the research object is **discrete and configural** — runs, configs,
> samples, items, conditions — RCP hosts the view.
> Where it is a **continuous field or a giant array**, RCP links to the tool that
> already owns it.

The reasoning is not about data size for its own sake. Where an entrenched viewer
exists, it is a decade of specialized rendering and interaction, and an
agent-authored view loses to it on its home ground — permanently, not until the
models improve. Where none exists, an agent-authored view is the best option
available, because the alternative is a hand-written script and a saved PNG.

### The asymmetry that makes CSE the tractable case

|  | Microscopy / spatial omics | CSE / AI |
|---|---|---|
| Entrenched viewer | yes, and strong (napari, Fiji, QuPath) | none — TensorBoard and W&B are generic dashboards |
| Data | gigabytes, needs specialized rendering | numbers, fits in memory |
| A view is | a project | a small program |
| Agent-authored interface | loses to napari | best available option |

The critique in Q6 ends by asking for an AI-written interactive interface. In the
author's own field that is not achievable, which is why it stays a complaint. In
CSE it is achievable. **That is what choosing CSE actually buys** — not a market
segment, but the one domain where the prescription works.

### Candidate domains, ranked by the predicate

Strong — structurally the same as CSE, no incumbent, decisions matter:

- **LLM evaluation and behavior analysis.** The most complete vacuum; current
  practice is ad-hoc notebooks and spreadsheets.
- **Agent and LLM-systems research.** Trajectories, tool calls, failure
  taxonomies. Nothing exists, and it is structurally the same shape as RCP itself.
- **Mechanistic interpretability.** Libraries exist (TransformerLens,
  circuitsvis); no viewer does. Views are bespoke per question, which is exactly
  the agent-authored case.
- **Systems and benchmark research.** Speedups, latency distributions,
  configuration matrices — table and distribution primitives suffice.
- **Numerical optimization and algorithms.** Convergence, iterates, conditioning.
  Small data, hand-written matplotlib today.

Adjacent, but the vocabulary differs:

- **Computational social science and empirical economics.** A specification curve
  — hundreds of model specifications run to see whether a conclusion survives — is
  a run table under another name. Their analysis tools (Stata, R) are entrenched;
  their visualization is not.
- **Downstream bioinformatics.** Differential expression, volcano plots, GSEA are
  largely ad-hoc ggplot. Anything touching genome browsing hits IGV. Half a
  candidate.

Do not enter — each has a decade-old incumbent: wet-lab microscopy, structural
biology, medical imaging, flow cytometry, chemistry, geospatial, CFD and FEA,
robotics. RL is a boundary case: its tables and curves are free, its rollout
videos fall on the array side.

### What blocks a decision

1. **Is this a blueprint fact or a roadmap fact?** The predicate is testable and
   governs feature scope, which argues for the blueprint. But it also reads as
   positioning, and the blueprint is a design specification. Where it belongs is
   undecided.
2. **Does the ontology survive the adjacent domains?** Node types are
   `research_question`, `hypothesis`, `experiment`, `evidence`, `decision`,
   `blocker`. Whether a specification-curve study or a benchmark comparison maps
   onto those without distortion has not been checked against a real project of
   either kind. If it does not, "adjacent" is doing more work than it can bear.
3. **Does the predicate hold at the seams?** A CSE project that produces a PDE
   solution field, or an interpretability project that wants a trace view, sits on
   both sides at once. Q6 answers this per primitive; whether that is sufficient
   in a real mixed project is untested.

### Do not do in the meantime

- Do not add domain-specific node types or per-domain connectors. The predicate
  exists precisely so neither is needed.
- Do not claim support for a domain in user-facing documentation without running
  it through the predicate first.
- Do not treat the do-not-enter list as permanent contempt for those fields. It
  says RCP loses to their viewers, not that their work is out of reach — the
  external-tail mechanism in Q6 is where they would be served, if ever.

---

## Q8 — Should RCP hold live provider sessions so a running turn can be interrupted?

**Status:** open. Raised 2026-08-07. Deliberately deferred, not ruled out.
**Governing section:** [Background tasks, concurrency, and provider readiness](research-control-panel-blueprint.md#background-tasks-concurrency-and-provider-readiness).
**Related work:** [orchestrator handoff](handoffs/handoff-2026-08-07-orchestrator.md).

### The question

RCP's agents terminate and resume. Nothing can reach a turn while it is running.
The human decided not to change that now, but the constraint turned out to be
RCP's, not the providers'.

### Evidence gathered so far

Both installed CLIs expose a real-time inbound channel. Verified by probing the
binaries on 2026-08-07, not from memory:

- **Claude Code** — `--input-format stream-json` is documented as *"realtime
  streaming input"*, paired with `--output-format stream-json`. Also `--bg`
  background agents with `claude agents --json` for scripting, and
  `--forward-subagent-text`, which surfaces subagent text with
  `parent_tool_use_id`.
- **Codex** — `codex app-server` is a daemon whose `--listen` accepts `stdio://`,
  `unix://PATH`, or `ws://IP:PORT`, and which emits its own protocol schema via
  `generate-json-schema` / `generate-ts`. Alongside it: `remote-control` with
  pairing, `mcp-server`, and `exec-server`. Both are marked `[experimental]`.

So "the CLI cannot do it" is false. Re-probe before relying on the specifics;
these are experimental surfaces.

### What blocks a decision

**The cost is RCP's lifecycle model, not the provider.** Everything RCP owns is
built on terminating subprocesses with durable resume: the recovery ladder,
Pause/Resume/Retry, restart safety, SSH PID wrappers, locks that release on
process death. A live session daemon inverts that. Over SSH especially, a
dropped connection is survivable today precisely because state is durable and
the lock releases; with a live bidirectional stream it becomes lost session
state.

The property that would be traded away: *every agent is either running a turn or
asleep with durable state.* That is what makes RCP restart-safe, and it is worth
more than responsiveness for research work where turns are minutes or hours
apart.

### The use case, if it is ever built

Live messaging is not for coordination — turn-based handoff serves that fine,
and the [graph-condition wake](handoffs/handoff-2026-08-07-graph-condition-wake.md)
covers the responsive cases through canonical state. It is for **interruption**:
"stop, wrong approach," "the cluster died," "I changed the framing." Mail cannot
do that, because it arrives at the next wake, and for a long turn that is an
hour of burned work.

Note the most valuable sender is the **human**, not another agent. If this is
ever built, the first version should be one live channel, human→agent, on the
turn the human is watching — a far smaller blast radius than agent-to-agent
streaming, and the only version whose value is obvious.

### Do not do in the meantime

Do not add a partial live channel "just for the orchestrator." A second
lifecycle model is the expensive part, and it is not less expensive for having
one caller.

---

## Q9 — How does peer-to-peer agent mail work once RCP is multiplayer?

**Status:** open. Raised 2026-08-07. Deferred until identity and multi-user land.
**Governing section:** [Watch delivery](research-control-panel-blueprint.md#watch-delivery).
**Related work:** [team-space identity and permissions handoff](handoffs/handoff-2026-08-08-team-spaces-identity-and-permissions.md),
[superseded actor identity handoff](handoffs/handoff-2026-08-07-actor-identity-and-permissions.md),
[orchestrator handoff](handoffs/handoff-2026-08-07-orchestrator.md).

The 2026-08-08 team-space design removes user-owned agent actors. Revisit the
questions below in terms of task/campaign authorization lineage, recipient
budget, project scope, and human consent rather than `owner_actor_id`.

### The question

The orchestrator ships a mail channel in **star topology only**: it may address
the seats it spawned, and they may reply to it. Seat-to-seat mail is not built.

In a single-player project that restraint costs nothing. Every peer case that
was tried collapsed into something better: cross-seat inference belongs in the
orchestrator, where it is adjudicated and logged in one place; resource
contention is a lease, not a message; and a blocker being resolved is a
canonical **graph** event that a graph-condition wake observes exactly, without
hearsay.

Multiplayer is what changes the calculus. Once several users share one truthful
RCP state, the natural shapes have no star to route through:

- one user's agent needs something from **another user**;
- one user's agent needs something from **another user's agent**; and
- a user wants to hand a seat to a collaborator without handing over their own
  agent's authority.

### What blocks a decision

The permission gymnastics, which are genuinely unsolved:

1. **Whose budget pays for a delivered message?** Delivery spends an invocation
   unit of the recipient. Cross-user, that means one user's agent can spend
   another user's budget — the ping-pong termination argument stops being a
   safety property and becomes an attack.
2. **What authority does a received message carry?** Nothing, by the hearsay
   rule. But an agent that acts on a peer's claim has still been influenced by
   an actor outside its owner's control, and the graph records the consequence
   without recording the influence unless the thread is retained.
3. **Can an agent address an actor its owner cannot?** It must not — an agent's
   reach is bounded by its owner's. That constrains the address book to
   something derived from group membership, not chosen by the agent.
4. **Consent.** Being addressable is a state a user should be able to decline,
   per project and per counterparty.
5. **Topology.** Graph adjacency was considered as the natural bound on who may
   talk to whom — two seats may talk when their control nodes are adjacent,
   making the comms topology derived from research structure and auditable. It
   is attractive and untested, and it interacts with cross-user permission in
   ways nobody has worked through.

### Do not do in the meantime

Do not generalize the orchestrator's star mail into peer mail because the
plumbing happens to allow it. The plumbing is the easy half; the budget and
consent questions above are the reason to wait.
