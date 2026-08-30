# Open questions

Design questions that are **raised and evidenced but not decided**. Current
product authority lives in [`design.md`](design.md) and [`specs/`](specs/);
[`acceptance/`](acceptance/README.md) records selected observable promises. This
file is deliberately non-normative.

An entry stays here until it is decided and incorporated into the applicable
current specification, or ruled out. Keep the evidence with the entry so the
next person does not re-derive it.

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
**Governing section:** [Watcher resources](specs/conversations-episodes-and-watchers.md#watcher-resources).

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
[authority specification](specs/authority-and-proposals.md). Building the first
must not silently alter the second.

### Secondary v2 lifecycle questions

- When, if ever, are permanently degraded or abandoned watcher records cleaned
  up? The rows are cheap, survive app close, and need no v1 user-facing cleanup
  action. Experiment agents may retire only scoped staged observers they have
  already settled; this does not decide broader retention policy.
- Does experiment control eventually need an enforceable repository lease, or is
  the v1 advisory active-loop marker enough? Human authority must remain explicit
  either way.

Graph-level scheduling across the research frontier is **no longer deferred**.
It is the bounded [Auto-research episode](specs/auto-research-and-branch-merge.md#episode-scope-budget-and-authority)
and remains outside this question—the two features share only the word
"control."

---

## Q5 — Should graph-writing agents be required to run an executable scanner?

**Status:** open. Raised 2026-08-03. No decision.
**Governing scenario:** [S59](acceptance/S59-staged-graph-audit-skills.md).
The implemented package boundary is in
[Official skills and workflows](specs/providers-and-containment.md#official-skills-and-workflows).

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

## Q6 — May an artifact selection emit a research action?

**Status:** open, and much narrower than when raised. The parent question —
whether RCP shows the researcher their own data, and in what shape — was decided
and now lives under
[Unified artifact viewer](specs/paper-artifacts-and-result-views.md#unified-artifact-viewer), driven by
[S114](acceptance/S114-see-your-results-without-leaving.md).
**Related:** [Q7](#q7--which-domains-can-rcp-serve-and-where-must-it-link-instead-of-host)
decides *for whom* this is worth building.

### What was decided, so it is not re-derived

RCP has no result-view type or surface. Agents produce ordinary task artifacts;
the common viewer can turn transient selections and human comments into an
ordinary chat draft. Artifacts are disposable by default, optionally kept as
live repository files, and carry no graph authority. Where they live, durable
versus disposable, and who draws are all settled there.

Two candidate designs were ruled out on the way and should not be revived:
a **pre-declared decision request plus session capture** (it assumes the human
knows which decision they are making before opening the tool, and makes ten
small edits into ten round trips), and **file observation as the main line**
(better plumbing, but it supplies provenance after the fact for a complaint
about not being able to see *during*). The second remains a candidate mechanism
for the external tail only.

### What is still open

Whether a human may ever turn an artifact selection into a distinct
**research-action control** — for example, select six runs and explicitly admit
them as Evidence with selection provenance. The present viewer does not do
this: selection only supplies bounded context to a human-sent Discuss or Work
turn, whose ordinary authority rules remain unchanged.

S114 deliberately contains no such control. A view there is read-only: it
changes no graph state, appends no Patch, and creates no Proposal. That was the
right first cut, and it leaves the question intact rather than answering it by
omission.

What blocks a decision: selecting runs and calling them evidence is a human
authority action, so it is legal under invariant 3 — but whether it asserts
directly or creates a Proposal has to follow from invariant 10b's narrow gating
rule, and that has not been checked against it. There is also no evidence yet
that the gesture is wanted: the loop S114 builds is *look, ask, look again*, and
nobody has used it long enough to know whether recording a conclusion from
inside a view is a real need or a tidy-sounding one.

Do not build an action bar into a view before S114 has been used on real work.

---

## Q7 — Which domains can RCP serve, and where must it link instead of host?

**Status:** open. Raised 2026-08-06. No decision.
**Related:** the shape boundary this predicate leans on is now decided under
[Unified artifact viewer](specs/paper-artifacts-and-result-views.md#unified-artifact-viewer);
[Q6](#q6--may-an-artifact-selection-emit-a-research-action) is what remains open there.

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

The critique that prompted this work ends by asking for an AI-written
interactive interface. In the
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

1. **Is this a design fact or a roadmap fact?** The predicate is testable and
   governs feature scope, which argues for the design. But it also reads as
   positioning, and the design hierarchy specifies implemented behavior. Where
   it belongs is undecided.
2. **Does the ontology survive the adjacent domains?** Node types are
   `research_question`, `hypothesis`, `experiment`, `evidence`, `decision`,
   `blocker`. Whether a specification-curve study or a benchmark comparison maps
   onto those without distortion has not been checked against a real project of
   either kind. If it does not, "adjacent" is doing more work than it can bear.
3. **Does the predicate hold at the seams?** A CSE project that produces a PDE
   solution field, or an interpretability project that wants a trace view, sits on
   both sides at once. The archived design snapshot's shape table answered this per shape;
   whether that is sufficient in a real mixed project is untested.

### Do not do in the meantime

- Do not add domain-specific node types or per-domain connectors. The predicate
  exists precisely so neither is needed.
- Do not claim support for a domain in user-facing documentation without running
  it through the predicate first.
- Do not treat the do-not-enter list as permanent contempt for those fields. It
  says RCP loses to their viewers, not that their work is out of reach — file
  observation of what an external tool saves is where they would be served, if
  ever. That mechanism was recorded and set aside, not adopted.

---

## Q8 — Should RCP hold live provider sessions so a running turn can be interrupted?

**Status:** open. Raised 2026-08-07. Deliberately deferred, not ruled out.
**Governing section:** [Durable task lifecycle](specs/providers-and-containment.md#durable-task-lifecycle).
**Related work:** [orchestrator handoff](archive/handoffs/handoff-2026-08-07-orchestrator.md).

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
- **Codex** — the installed build exposes a JSON-RPC app-server over stdio with
  persisted `thread/start` / `thread/resume` and `turn/start` lifecycles. RCP now
  uses one fresh app-server process per provider turn. That proves a richer wire
  protocol and Desktop-visible persisted threads; it does not keep a process
  alive between turns or make an in-flight human interruption channel.

So "the CLI has no bidirectional turn protocol" is false. Re-probe before
relying on specifics because app-server remains experimental. The open question
is still whether RCP should expose input to a running turn, not whether RCP can
select app-server as its per-turn transport.

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
and the [graph-condition wake](archive/handoffs/handoff-2026-08-07-graph-condition-wake.md)
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

**Status:** open. Raised 2026-08-07. Deferred until team spaces and orchestration land.
**Governing section:** [Watcher resources](specs/conversations-episodes-and-watchers.md#watcher-resources).
**Related work:** [orchestrator handoff](archive/handoffs/handoff-2026-08-07-orchestrator.md).

The confirmed team-space design has no user-owned agent actors. Concrete tasks,
workers, and episodes are addressable execution records; the ordinary and
project-orchestrator profiles are the permission principals. Peer mail must
therefore be designed from task/episode lineage, project membership, recipient
budget, scope, and human authorization—not an agent owner or actor directory.

### The question

The orchestrator ships a mail channel in **star topology only**: it may address
the seats it spawned, and they may reply to it. Seat-to-seat mail is not built.

In a single-player project that restraint costs nothing. Every peer case that
was tried collapsed into something better: cross-seat inference belongs in the
orchestrator, where it is adjudicated and logged in one place; resource
contention is a lease, not a message; and a blocker being resolved is a
canonical **graph** event that a graph-condition wake observes exactly, without
hearsay.

Team spaces change the calculus because several independently authorized root
tasks and episodes may coexist in one project. The unresolved cases are:

- a task in one episode needs a result from a task in another episode;
- an ordinary task wants to address a worker it did not spawn;
- a project member wants to transfer or share responsibility for a running seat;
  and
- a message would wake a recipient whose remaining budget and root authorizer
  differ from the sender's.

### What blocks a decision

The permission gymnastics, which are genuinely unsolved:

1. **Which authorization pays for delivery?** Delivery spends an invocation
   unit of the recipient. A task from episode A must not consume episode B's
   budget merely by addressing one of its workers. The recipient episode or
   root authorization needs an explicit admission rule or opt-in.
2. **What authority does a received message carry?** Nothing, by the hearsay
   rule. The recipient still acts under its own profile, task contract, scope,
   project membership, and root authorization. The retained thread must record
   the outside influence even though it grants no permission.
3. **Which tasks are addressable?** A task cannot name an arbitrary id and gain
   reach. The address set must be derived from permitted project and episode
   structure, active task state, and the sender's own scope.
4. **Who consents?** Cross-episode addressability may need consent from the
   recipient episode's root authorizer or a project-level policy. Project
   membership alone does not authorize spending another episode's budget.
5. **Topology.** Graph adjacency was considered as the natural bound on who may
   talk to whom — two seats may talk when their control nodes are adjacent,
   making the comms topology derived from research structure and auditable. It
   is attractive and untested, and it does not by itself solve budget consent or
   task addressability.

### Do not do in the meantime

Do not generalize the orchestrator's star mail into peer mail because the
plumbing happens to allow it. The plumbing is the easy half; the budget and
consent questions above are the reason to wait.

---

## Q10 — Should a client detect rollback of a familiar space?

**Status:** open. Raised 2026-08-28. Explicitly outside the first team-server
restore contract.
**Governing scenarios:** [S95](acceptance/S95-durable-team-space.md) and
[S104](acceptance/S104-backups-never-pause-work.md).

### Decided boundary

A replacement restored from backup preserves `space_id`, so saved clients still
recognize it as the same authority domain. The first restore workflow keeps the
service stopped while an operator confirms that the old copy cannot resume,
reviews the snapshot-time member roster, detaches captured live work, and
completes replay/readback. It does not claim that two copies can detect each
other or that a desktop can recognize an older snapshot of the same space.

### What remains open

Whether a later client/server protocol should detect that a familiar
`space_id` has moved backward to an older durable state, and what evidence could
do so without depending on the unavailable old server. A counter stored only in
SQLite or the backed-up data directory rolls back with the archive, so adding
one there does not solve the problem. An external witness, client-observed
monotonic receipt, or installation/restore lineage may help, but each changes
the offline-recovery and multi-client contract and has not been designed.

### Do not do in the meantime

Do not block the accepted one-lab restore on this future detection mechanism or
present `space_id` equality as rollback detection. The current safety boundary
is the explicit old-authority exclusion and stopped-service restore journal.
