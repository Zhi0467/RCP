# Research Control Panel blueprint

**Version:** 0.36
**Status:** canonical

This is RCP's single design blueprint. It replaces the former v0.3-v0.5
snapshots and v0.6-v0.15 amendment files.

When a design decision changes, edit this file in place, bump the version above,
and add one concise entry to the changelog. Do not create amendment, delta, or
snapshot blueprint files. Git history is the version archive.

Acceptance scenarios in [`acceptance/`](acceptance/) are the executable promises
for user-visible behavior. [`open-questions.md`](open-questions.md) contains
raised but undecided questions and is deliberately non-normative.

## Changelog

- **0.36** — changed the project dock from horizontal overflow to browser-like
  proportional tab compression inside one capped span, with the active project
  retaining more width than inactive tabs.
- **0.35** — added a session-scoped multi-project dock with direct project-tab
  switching and closing, an index shortcut, and per-tab in-session view state
  without changing project, draft, or background-task ownership.
- **0.34** — added a current cross-project Experiment-loop board below the
  project shelf: one compact row per Experiment that has launched a loop,
  actionable and active work first, all finished work folded, unavailable
  projects retained from last-known state, and navigation into the canonical
  Runs detail rather than duplicate landing-page controls.
- **0.33** — made click, Enter, and Tab complete a selected official or
  provider-native skill into the visible Chat or Paper composer while retaining
  the separate structured invocation receipt.
- **0.32** — replaced active Ambiguities and agent Decision Proposals with one
  direct Decision lifecycle: agents queue makeable choices as `ready` or reopen
  them as `revisit`, the Inbox opens those node cards for the existing human
  ballot, and Seed/Refresh retain their labelled answers; historical records
  remain replayable without remaining authorable or visible as attention.
- **0.31** — replaced node-detail relation text with a complete vertical one-hop
  map, added two-window side-by-side comparison with reachable narrow-screen
  overlap, and made expansion a non-navigating full-screen relation inspection
  surface with one compact read-only node card.
- **0.30** — made provider limits recoverable inside the current Experiment
  episode and invocation: same-provider Retry resumes the exact native binding,
  while an explicit provider switch replaces it only after a successful joint
  Patch/watcher handoff; exposed recovery directly in Runs and made same-machine
  watcher instructions local rather than inviting a self-SSH.
- **0.29** — added desktop-only macOS dictation as editable composer input and
  provider-neutral temporary chat attachments: bounded files are claimed by one
  human turn, staged immutably on the execution host, named by path in a private
  turn block, retained for seven days, and never promoted into graph provenance.
- **0.28** — moved Experiment watcher authority from the arming conversation
  to the Experiment node and its live episode: permitted Work conversations may
  inspect and atomically maintain the same observer set, file identity selects
  the wake target, and completion always resumes the episode-bound chat and
  native session while ordinary conversation watchers remain self-wakes.
- **0.27** — added app-scoped provider-native skill inventories: each provider
  owns its refresh command and parser, startup refresh follows readiness over
  the existing local or SSH path, last-known-good results survive visible stale
  failures, and slash menus distinguish RCP Official packages from the selected
  provider and machine's native skills without widening run authority.
- **0.26** — added the `informs` Evidence-to-Decision and `addresses`
  Evidence-to-Blocker action handoffs; required a local causal check on every
  Patch-origin and correction contract; and made package activation explicit
  while composing graph audit, experiment causality, and evidence triage in the
  research-graph-audit workflow.
- **0.25** — made every non-superseded Decision directly human-decidable through
  a staged option ballot: Sync records the listed selection, decided status, and
  accepted standing together while atomically withdrawing competing pending
  Proposals; required coherent new Decision Proposals while preserving replay of
  legacy approval history.
- **0.24** — let an Experiment-loop agent retire its own staged observers through
  the atomic watcher handoff after it has settled the external work, absorbed
  into a concurrent graceful **Stop loop** rather than failing that turn; added
  durable paced polling and immutable grouped Experiment observers whose one
  ready-or-diagnostic wake is claimed exactly once. Generic Work observers
  remain strict three-field items with manual **Stop watching**.
- **0.23** — made every visible client detect canonical graph revision changes
  through a lightweight read-only probe and reconcile its project snapshot,
  without continuous graph replay or repurposing the ingestion Refresh action.
- **0.22** — made Research flow expose nested Research Questions as successive
  hierarchy columns before the existing Hypothesis/Decision, Experiment/Blocker,
  and Evidence stages; only `has_subquestion` determines question depth.
- **0.21** — gave every user-facing agent surface provider-native public-web
  search and fetch without widening its fixed filesystem, repository, paper, or
  graph authority; kept generic Seed/Refresh Patch correction offline over
  staged local inputs.
- **0.20** — allowed an Experiment-loop invocation to keep its focused
  Experiment's `current_summary` and `next_action` aligned with attempt changes,
  without widening authority to any other Experiment field or graph object.
- **0.19** — defined human attention as pending judgment: after Sync records
  either accepted or contested standing, an open Blocker leaves Inbox, project
  attention counts, and Runs **Needs action** without changing its independent
  operational status or readiness effect; exposed Blocker lifecycle status in
  the human node editor while preserving direct, non-Proposal agent updates.
- **0.18** — bound each Experiment-loop episode to one validated native provider
  session: an automatic watcher wake is a new task and budget unit that resumes
  that session with a compact continuation message, while every human Run still
  starts a fresh session; added the graceful episode-level **Stop loop** and made
  Runs an operational hierarchy ordered Running, Needs action, Completed. Generic
  Work watcher wakes remain fresh turns.
- **0.17** — changed Experiment control's human-set ceiling from a cap on
  agent-authored `ExperimentAttempt` records to a per-episode cap on
  Experiment-loop agent invocations; each human Run starts a fresh episode,
  watcher wakes consume its budget, and attempt bookkeeping remains semantic
  agent output.
- **0.16** — consolidated the complete blueprint into one current document;
  incorporated per-turn Discuss/Work, experiment control and watchers, minimal
  Proposals, live Patch validation, guarded node removal, reader-facing UI,
  direct provider-log ingestion, process-held remote locks, Proposal withdrawal,
  app-scoped provider readiness, Settings-owned skills, concurrent tasks, and
  native chat master-context deltas; removed all superseded designs.

## Purpose and product boundary

RCP is a local research control panel that turns agent-assisted research into:

- one project-global research graph;
- one human authority queue for consequential belief and decision changes;
- operational conversations and bounded experiment control;
- an append-only, replayable account of how the graph changed; and
- a human-authored paper introduction with a read-only writing coach.

The graph is both a research record and a control input. Epistemic structure
records what is believed and why. Action structure records what decisions and
blockers govern experiments. RCP may dispatch work from that structure, but it
does not become a scheduler, silently accept scientific conclusions, or treat
mutable operational observations as canonical truth.

The expensive-to-reverse commitments are the graph ontology, the Patch log's
meaning, the human authority boundary, and the separation between canonical and
operational state. Provider choice, UI layout, transport, and execution details
may evolve behind those commitments.

### Non-goals

- RCP is not a general project-management backlog.
- RCP does not replace Git, provider-native sessions, Slurm, process managers,
  notebooks, or experiment trackers.
- RCP does not infer that external work succeeded merely because it disappeared.
- RCP does not let an agent accept a belief, decide a governed Decision, or
  approve or reject its own Proposal.
- RCP does not let generated paper prose become canonical research truth.
- RCP does not maintain a second editable copy of materialized graph state.

## Project, repository, and state boundary

A project has a manifest, one global graph, a guarded project truth scope, and
exactly one canonical state repository. The state repository may be local or
remote. Its `.research/` directory contains append-only Patch history and
materialized outputs.

Repository membership and run focus are different:

- project truth scope is human-authored membership;
- a run selects a non-empty subset as contextual raw inputs;
- the whole graph and canonical `research.md` enter every graph-capable run;
- repository paths are always paired with their execution machine or host; and
- Work's selected repository scope is context, not an operating-system
  permission boundary.

Routes never write canonical state directly. All canonical reads and writes go
through the state workspace, its ownership locks, validation, and publication
protocol.

## Graph model and ontology

The six shipped authoring types are the product ontology:

- **ResearchQuestion** — the question being resolved;
- **Hypothesis** — a falsifiable claim and its current semantic status;
- **Decision** — a choice required by research execution;
- **Experiment** — a bounded test with optional precommitted completion criteria;
- **Evidence** — a durable observation with explicit provenance, optionally
  produced by an Experiment; and
- **Blocker** — a concrete impediment to progress.

Every node has an id, title, ordinary-language content, provenance standing, and
type-specific fields. Standing is `asserted`, `contested`, or `accepted` and
belongs to nodes, not edges. `Hypothesis.scope` records human-authored boundary
conditions. `Evidence.origin` records where the observation came from.
`confidence` is not a graph field.

Nested records such as `ExperimentAttempt`, belief transitions, sources, and
decision options are not independent graph nodes. Proposal, glossary, and
ontology records are side-car state with their own strict schemas. Historical
Ambiguity records remain in the replay schema only: new history cannot create or
resolve them, and they do not render or contribute to attention.

Project Settings does not expose ontology authoring. Historical extension types,
fields, and relations already present in append-only history remain replayable,
renderable, and valid. The shipped authoring surface remains fixed to the six
product types.

### Relations and graph structure

Relations are a closed, typed vocabulary. Each relation defines legal endpoint
types and a reading layer. The core shapes include:

- ResearchQuestion `has_subquestion` ResearchQuestion;
- ResearchQuestion framing Hypotheses, Decisions, Experiments, and Blockers;
- Experiment `tests` Hypothesis;
- Experiment `governed_by` Decision;
- Experiment `blocked_by` Blocker;
- Experiment `produces` Evidence;
- Evidence `informs` Decision without making the human-owned choice;
- Evidence `addresses` Blocker without by itself changing its recorded
  lifecycle; and
- Evidence epistemically supporting, weakening, refuting, contradicting, or
  being inconclusive toward a Hypothesis as permitted by the relation table.

These action handoffs make precursor experiments expressible without reversing
causality: a smoke, calibration, profiling, diagnostic, or feasibility
Experiment produces Evidence; that Evidence informs the downstream Decision or
addresses the downstream Blocker; and the resulting gate governs or blocks the
main Experiment. A downstream gate that the precursor is meant to settle is not
an input to that precursor.

Epistemic and action layers are projections over one graph, joined at
Experiments. Layers improve reading and layout; they do not define export,
authority, or scheduling by themselves. Readiness consults the specific base
relations whose semantics RCP understands, never an arbitrary custom relation
or `Experiment.status`.

Structural validation protects ids, references, endpoint types, uniqueness, and
replay safety. Authoring validation protects comprehensibility, graph-authority
rules, causation, and run-specific admission. Tightening authoring guidance does
not make valid historical structure unreplayable.

### Schema evolution and transfer boundary

Append-only history preserves historical ontology definitions and operations.
Extensions remain replayable even though new authoring is fixed to product
types. A transferable graph unit includes its ontology revision, Patch history,
and stable research semantics. Provider-native `SourceRef` locations are local
provenance pointers and are not claimed to be portable across installations.

Future schema widening must be explicit and migration-aware. Undecided ontology
or glossary-authority questions stay in
[`open-questions.md`](open-questions.md), not in agent prompts or opportunistic
implementation changes.

## Human and agent authority

Agents assert research structure. Humans hold semantic authority.

Only human actions may:

- set node standing;
- approve or reject a Proposal;
- decide any non-superseded Decision;
- accept a Hypothesis status transition;
- change project truth-scope membership; or
- authorize a new bounded Experiment-loop episode.

Contest and Agree are independent visible human controls. Clearing either
returns standing to `asserted`; selecting the other replaces it. Proposal
Reject and Approve likewise remain staged and reversible until Sync, then become
terminal historical resolutions.

A Decision's listed options are a dedicated human authority control, not an
ordinary node-edit field. On any non-superseded Decision, selecting one option
stages that `selected_option`, `status: decided`, and accepted standing in the
project draft. Sync commits them in one approval Patch and atomically withdraws
every pending Proposal that updates the same Decision. Editing the available
options remains separate from choosing among them.

Decision lifecycle also records whether a choice needs human attention. An
agent-created Decision is `open` while it is not yet makeable and may instead be
created `ready` when it is. Agents and humans may queue a Decision as `open`,
`ready`, or `revisit`; only the human ballot may write `selected_option` or
`status: decided`. `ready` and `revisit` enter the Inbox, while `ready` to `open`
is the human's ordinary "not yet" edit. A `ready` or `revisit` Decision must
have at least two distinct options. `revisit` preserves the previous selection
while reopening it after later evidence undermines the choice.

Ripeness is prompt guidance rather than an inferred or mechanically validated
scientific judgment. An agent must inspect the run-scope repositories, real
experiment state, and code rather than relying on the graph alone. Normally a
ready choice has no open governing Blocker, no pre-completion governed
Experiment, and enough rationale to explain what the choice turns on. RCP never
sets `ready` by itself.

Blocker standing and lifecycle status are independent. The human node editor may
set Blocker status to `open`, `resolved`, or `superseded`, and a graph-capable
agent may update the same field directly. These ordinary Blocker lifecycle edits
do not create or require a Proposal. Like other edits to judged node content,
they reset accepted or contested standing to asserted. A resolved or superseded
Blocker remains outside human attention; reopening it makes the asserted Blocker
await a fresh judgment.

### Minimal agent Proposals

An agent Proposal has one semantic shape: one Hypothesis changes status with
exactly one valid Evidence-to-Hypothesis epistemic edge as its cause.

The Proposal contains one `update_nodes` operation for one target. Ordinary
content edits, edges, Evidence, Blockers, merges, supersessions, and removals are
not Proposal-only merely because accepted material is nearby. An ordinary agent
edit to accepted node content returns that node to asserted review.

Agents do not propose Decision outcomes: they queue the Decision itself as
`ready` or `revisit` and the human uses its ballot. Historical Decision
Proposals and approvals remain replayable. A pending historical Decision
Proposal restored from older state remains resolvable; approving it is adapted
to the same named `decision_choice` authority action used by the ballot, so
Decision outcomes still have exactly one producer. Agent-created Hypotheses
begin proposed. Agents cannot approve or reject Proposals, but may explicitly
withdraw any still-pending Proposal that later work proves obsolete or
duplicated.
Withdrawal replays no semantic operation. RCP records creation and resolution
provenance, including the originating task when available.

A loop may queue a pinned governing Decision as `ready` or `revisit`, and may
propose a transition only for a Hypothesis tested by its Experiment. The
Hypothesis transition is grounded by an Evidence edge asserted in the same
Patch. The human accepts the belief change, not the edge; edges have no standing.

### Guarded node removal

`remove_nodes` removes current nodes and their incident edges without rewriting
history. Every target must exist, must not have accepted standing, and, for an
Experiment, must have no active bounded loop. One invalid target rejects the
whole operation.

The human UI will not combine clearing accepted standing and removal in one
gesture; the standing change must first become canonical through Sync. Removal
may stale a dependent pending Proposal but does not implicitly approve, reject,
or withdraw it.

## Append-only history, validation, and replay

`.research/patches/` is the canonical append-only log. A Patch records semantic
operations, authoring provenance, and revision metadata. Materialized
`graph.json`, `research.md`, `glossary.json`, `proposals.json`, and related files
are derived outputs and are never hand-edited.

Patch publication is atomic. Human Sync publishes one visible batch directory.
Agent graph runs write exactly one semantic `patch.json` in RCP-owned scratch;
RCP supplies patch kind, author, revision, run scope, Proposal dependencies,
lifecycle revisions, and admission bookkeeping.

Validation runs before a new agent candidate enters history. A candidate rejected
during bounded correction consumes no canonical revision and remains retained in
scratch as task evidence. A rejection already recorded by an older or explicitly
auditable history path remains append-only history and replayable. RCP never edits
or deletes such a record to make current state look clean.

Replay applies Patches in revision order and halts at the first invalid recorded
revision. It never skips a bad Patch and continues into invented state. The last
coherent materialization remains readable while graph-authority operations are
blocked. Applying a Patch never mutates objects shared with a prior revision in
place.

There is no second checkpoint truth. Materialization derives state from the log,
with performance coming from efficient container forking and bounded caches that
cannot alter semantics.

### One structured graph channel

`patch.json` is the only graph-change channel out of an agent. RCP never parses a
Patch from stdout, Markdown, provider directives, or preview files. Work may
modify operational repositories, but those writes do not grant graph authority.
Preview artifacts are temporary, non-canonical, and independent of both reply
and Patch verdict.

### Uniform live Patch self-check and causal check

Every Patch-origin and Patch-correction provider pass receives the same concise
local causal check in its graph-authoring contract: Seed, Refresh, Work,
Experiment-loop, generic Seed/Refresh correction, and Work or Experiment-loop
graph correction. Discuss and Paper do not receive it because they have no
Patch channel. Correction instructions may point back to the retained contract,
but must explicitly require the corrected Patch to pass the same check.

Before finishing a Patch that creates or materially changes an Experiment,
Decision, Blocker, Evidence, or an edge among them, the agent asks what truly
gates each Experiment, what it will determine or unblock, what Evidence it
produces, which downstream Decision that Evidence `informs` or Blocker it
`addresses`, and whether every empirical gate on a main Experiment has its
precursor Experiment and Evidence handoff. Edge directions and node prose must
tell the same causal story; a downstream output must not be attached backward
as its precursor's input.

Each such pass also receives an RCP-staged validator client and exact command.
It checks the current `patch.json` against live canonical state through a
bounded request/response mailbox. Exit values distinguish valid, semantically
invalid, and validator unavailable; unavailable never becomes a semantic
correction loop.

The self-check is advisory. Apply reloads current state and reruns the same
semantic validator while holding the canonical append lock. Graph movement alone
is not rejection; only current semantic invalidity is. Validation stages
operations in written order while retaining whole-Patch lookup for legal forward
references. It never reorders operations.

## Agent surfaces and fixed capabilities

Run policy lives in separate Seed/Refresh, Discuss, Work, Experiment-loop, and
Paper modules. They may share launch, event, staging, and receipt plumbing, but a
shared helper never chooses policy from a `kind`, `surface`, or equivalent
discriminator.

Capabilities are fixed by surface and cannot be widened or narrowed by the
manifest or a skill:

- **Seed/Refresh** — scratch-only graph authoring over direct provider logs and
  repository pointers;
- **Discuss** — writable conversation scratch, read-only project/repository
  reasoning, no active Patch contract;
- **Work** — non-interactive unrestricted tooling and repository access;
  canonical `.research` remains forbidden by prompt contract;
- **generic Seed/Refresh correction** — scratch-only rewrite of `patch.json`;
- **Work graph/watcher correction** — same native Work session and unrestricted
  Work capability, with instruction changed to avoid repeating completed side
  effects; and
- **Paper coach** — read-only coaching with no graph or file-authoring channel.

Every user-facing agent surface — Seed/Refresh, Discuss, Work, and Paper coach —
has provider-native public-web search and fetch. That access does not widen the
surface's fixed filesystem, repository, paper, or graph authority. Generic
Seed/Refresh Patch-correction continuation remains instruction-constrained to
staged local inputs and offline operation.

Codex Work bypasses approval and sandbox enforcement. Claude Work uses its
non-interactive bypass-permissions mode. The canonical `.research` prohibition
is therefore a known prompt-enforced boundary for Work, never described as an
OS sandbox guarantee.

All provider turns preserve labelled final assistant messages as answers;
unlabelled traces, reasoning, and tool output are never promoted to the reply.

## Seed and Refresh ingestion

Seed and Refresh are the only transcript-ingestion operations. RCP gives the
execution-host agent:

- configured provider log roots on that machine;
- the project-level canonical `last_refresh_at` watermark;
- the full current graph and research rendering;
- exact repository pointers; and
- an optional human request.

The agent reads provider logs in place. RCP performs only bounded
existence/readability preflight. It does not parse, normalize, index, slice,
hash, cache, stage, transfer, or project provider conversation content, and it
does not maintain per-session cursors or coverage claims.

The watermark advances only after a Seed/Refresh Patch applies. Failed, paused,
interrupted, or rejected runs leave it unchanged. It is an overlap-tolerant
incremental-reading hint rather than an exactly-once record cursor. Large-corpus
fan-out remains provider-owned and read-only; the parent is the sole Patch
writer.

Seed/Refresh recovery reuses a validated prepared context on literal Resume,
retains failed Patch text and scratch, and returns agent-correctable validation
messages to the same session within a bound. A provider failure may be retried
or handed off according to durable task provenance; completed external effects
are never silently repeated.

The labelled final Seed/Refresh assistant message is retained on the durable
task and displayed beside its Patch outcome. This is where the agent reports an
ontology gap, missing Hypothesis scope, or other limitation that cannot be
represented in the shipped graph. Such a limitation is an answer, not a reason
to manufacture a Blocker, Decision, or other substitute node.

## Discuss, Work, and native chat context

Discuss and Work are per-turn modes in one conversation. The mode is captured at
submit time, stored visibly on the turn, and retained by Pause, Resume, Retry,
and correction. Changing the composer affects only the next ordinary turn.

Discuss answers the request without graph or repository mutation. Work is the
human's authorization for operational execution and one optional semantic
`patch.json`. Work may finish without a Patch; no net graph change spends no
revision. Its operational answer and graph outcome remain independently visible.

Chat is not transcript ingestion. RCP never reads, indexes, copies, projects,
prompts with, validates, or authorizes from prior RCP chat transcript content.
Canonical chat history exists for display only. A provider session identifier may
continue native provider context without turning that transcript into RCP input.

### Master context and compact deltas

The first ordinary turn in an RCP-owned native chat session receives one master
context containing stable graph, focused-node, repository, package, schema, and
output pointers plus both Discuss and Work contracts. Seeing both contracts is
not cumulative authority: exactly one explicit turn marker activates one mode.

Each later ordinary native resume sends only:

1. `This is a Discuss turn.` or `This is a Work turn.`;
2. the logical turn id;
3. the human message unchanged; and
4. one compact replacement block when stable context actually changed.

RCP commits the new context baseline only after a successful turn and binds it
to the exact provider, execution machine, native session, project, conversation,
and focused node. A failed or interrupted turn does not advance it. Contract,
repository, Settings, and enabled-package pointer changes are deltas; unchanged
values are omitted. Provider and execution-machine identity remain native-session
bindings rather than silently switching under Resume.

Paused Resume, Retry, graph correction, watcher correction, Experiment-loop, and
watcher-wake messages keep explicit continuation contracts instead of pretending
to be new human turns.

### Human chat input

The macOS desktop composer may use Apple's network-backed Speech service to turn
one microphone segment into ordinary editable text. The microphone action sits
immediately left of **Send** and exists only in the desktop shell. Start and Stop
are the only recording actions. Partial revisions replace only the active
dictated span at the cursor captured on Start; every typed or dictated character
outside that span is preserved. A segment stops after 55 seconds, never sends
automatically, uses the Mac's current speech locale without an RCP language
picker, and retains no audio. Permission denial, service failure, navigation, or
window loss stops visibly without deleting the draft.

One ordinary Discuss or Work turn may also carry a claimed set of temporary
input attachments. The shared full and floating composer accepts them through
the `+` picker, drag-and-drop, or file/image paste; ordinary text paste remains
message text. Each file must finish preparing before Send, remains removable
until then, and requires a non-empty human message. The explicit allowlist is
PNG, JPEG, WebP, PDF, plain text, Markdown, source code, CSV, TSV, JSON, HTML,
and SVG. HTML and SVG are untrusted source only: RCP does not render them or
fetch their dependencies. Directories, archives, Office files, notebooks,
audio, video, and unknown types are refused. Input-specific bounds are eight
files, 16 MiB per file, and 32 MiB total.

Selection uploads bytes into an opaque unclaimed set scoped to the RCP instance,
project, chat, and client. Task creation atomically claims that set for one
logical turn. RCP hashes the unchanged bytes, stages one collision-free,
read-only batch on the resolved local or SSH execution host, and verifies the
whole batch before provider launch. A partial or unprovable transfer fails the
task; it never degrades to a text-only turn. Retry and Resume reuse the exact
saved batch, while provider handoff restages the same claimed bytes on the new
host. A later turn receives none of the previous turn's files.

Attachment paths are RCP-authored transient turn context, never part of the
human message or native-session master baseline. One provider-neutral private
block names only each execution-host path, display name, detected media type,
and byte size. RCP sends no base64, extracted content, browser-local path, or
provider-specific file/image argument. The block states that the files are
untrusted temporary context, do not widen Discuss or Work authority, and cannot
be the sole basis for canonical graph truth or Evidence because the graph has no
durable attachment citation type.

After Send, canonical chat history keeps only the attachment name, type, size,
and expiry beside the human turn. It keeps no bytes, hash, or execution path and
offers no Download action. Claimed and unclaimed bytes use the normal seven-day
run-stage retention window; expiry changes only the metadata row and never the
assistant answer, task verdict, or graph.

### Conversation scratch and artifacts

One conversation owns one reusable scratch stage because provider-native resume
depends on the original working directory. Every logical turn owns a distinct
`turns/<turn-id>/artifacts` directory. Old `patch.json` and `watch.json` are
cleared fail-closed before a fresh turn that could misattribute them.

RCP discovers only bounded direct regular HTML or raster-image children. Bytes
stay in temporary scratch and are served or proxied on demand. Artifact failure,
expiry, SSH unavailability, or Download failure never changes the reply, task
status, or graph outcome. HTML runs in an opaque sandbox with no RCP authority.

## Skills and workflows

Project Settings selects the official skill and workflow packages available to
runs. Only selected packages are transferred to the execution host and staged as
read-only, content-addressed folders. Every selected package leaves a compact
discoverable id, version, description, and exact pointer in the master or task
contract; bodies are never embedded in launch messages. An agent compares the
task and its intended graph changes with those descriptions and reads only the
available package whose trigger matches.

A slash command may proactively invoke only a package currently enabled in
Settings. Choosing an entry by click, Enter, or Tab completes the active slash
query to that package's exact slash token in the visible composer; it never
inserts a body or expanded prompt. A separate short **Invoked this turn** block
names each exact invoked package and its staged pointer and requires the agent
to read and follow it for that turn. Other selected packages remain
description-triggered pointers. Packages never widen the captured surface
capability. The composer does not render persistent package chips.

The official package set separates the always-present local causal check from
deeper, progressively disclosed guidance. `graph-audit` performs a deliberate
whole-graph structural and lifecycle pass. `experiment-causality` recursively
checks main experiments, precursor experiments, Evidence, Decisions, and
Blockers for complete and correctly directed action chains. `evidence-triage`
checks provenance, strength, validity, and interpretation of load-bearing
Evidence, including action Evidence. The `research-graph-audit` workflow depends
on all three and runs them in that order: broad graph structure, experiment
causal closure, then evidence provenance. Merely staging any of them does not
make every Seed or Refresh run a full audit.

Package registry, version, dependency, staging, and receipts are implemented
contracts. An executable mandatory graph-scanner remains only the unconfirmed
proposal in [S59](acceptance/S59-staged-graph-audit-skills.md) and is not part of
this blueprint.

Provider-native skills are a separate app-scoped inventory, never additions to
the official registry or project Settings. After the app becomes healthy, each
provider profile refreshes its configured machine targets once, downstream of
the existing executable-path, version, authentication, and model-catalog check.
Local and remote commands use the same provider runner and SSH login-shell path
as readiness. The exact refresh command and normalized result live in app
SQLite, keyed to the provider and machine target; no inventory enters the
manifest or `.research`.

A successful refresh atomically replaces the last successful result. Failure
retains those skills and their successful version and hash, marks them visibly
stale, and records the current diagnostic. With no prior success there is
nothing to offer. Project open, navigation, explicit readiness refresh, and
launch never refresh this inventory; startup is the sole automatic refresh.

Chat and Paper slash menus place **RCP Official Workflows** and **RCP Official
Skills** before the native group for the currently selected provider and
execution machine. A native selection is per-turn structured metadata carrying
provider, machine, successful provider version, inventory hash, and skill name.
Click, Enter, or Tab completes its generic slash token into the visible composer
without altering launch flags, permissions, graph authority, or repository
authority. A stale native selection that the CLI no longer accepts fails visibly
without falling back.

## Experiment control and watchers

Experiment control and generic watchers are separate mechanisms. The Experiment
loop owns readiness, bounded invocations, and graph admission; the agent owns the
meaning of its attempt records. A watcher only checks whether named external
work remains in its system and requests a wake of the conversation that armed it.

### Readiness and loop invocation budget

An Experiment's **Run** action is available only when:

1. every `governed_by` Decision is decided with a selected option;
2. none of those Decisions has a pending Proposal;
3. no `blocked_by` Blocker is open;
4. no current loop episode still has an automatic invocation available through a
   queued/running task or a live/pending watcher.

Readiness is derived and never reads `Experiment.status`. Ordinary Work remains
available while Run is disabled. The active marker suppresses duplicate loops
but is not a repository lease.

A human pressing Run starts one bounded Experiment-loop episode with a durable
episode id and invocation 1. Every attributed watcher wake consumes one further
unit of the human-set `invocation_ceiling`. The button itself creates no semantic
`ExperimentAttempt`; the agent records and closes attempts only when that is
useful scientific bookkeeping. Attempt status never gates Run, advances the
counter, identifies a watcher, or resets an episode.

Provider-task Resume and Retry retain the episode id and invocation number of
the interrupted or failed turn. Live Patch validation, in-session Patch
correction, watcher-file correction, and a later repair of a rejected graph
reflection also remain recovery inside that invocation. They never consume a
second loop unit and must not repeat completed operational side effects.
Only the newest unresolved operational task in the newest episode may Resume or
Retry; a successful sibling, a later invocation, or a later human-started episode
makes an older operational continuation stale. A patch-only graph repair may
still reflect retained completed work, but it cannot rerun operational work or
reopen the old episode.

A watcher wake is an incremental continuation of the same bounded episode. It
may inspect the named work, edit or debug the relevant repository, launch more
work, and arm more watchers while loop invocations remain. RCP does not infer
scientific attempt boundaries from watcher rows; the agent records and closes
`ExperimentAttempt` records when the experiment's meaning calls for it.
That same focused-Experiment Patch may update `current_summary` and
`next_action` when an invocation introduces or closes attempts or changes what
should happen next. The prose must describe the resulting canonical attempt
ledger and actual next step; it may remain unchanged when still accurate, and
`next_action` becomes null when no further action remains. This does not widen
the loop's authority beyond its own Experiment.

### Episode native sessions and graceful stop

Every episode has exactly one active validated native-session binding at a
time: provider, session id, execution host, and the exact reusable chat stage. A
human Run always
starts a fresh episode and a fresh native session — including Proposal or Blocker
resolution, invocation-limit reauthorization, and restart after **Stop loop** —
so native context never grows across a human authority boundary. An automatic
watcher wake instead resumes that binding: it is a new durable RCP task with
`trigger="watcher"`, the next invocation number, its own answer and handoff, and
an explicit `watcher_wake` continuation cause that is not task Resume. Task
Resume continues one paused task at the same invocation.

Watcher provenance never chooses the resumed session; the newest human-authorized
episode does. An Experiment watcher belongs to its Experiment node and the
episode that accepted it, not to the conversation, provider, or machine that
created it. A completed watcher or ready group may wake the newest live episode
for that node when its node attachment and check execution host remain valid;
the episode supplies the provider, policy, bound chat, and native session. Origin
conversation, provider, invocation, and machine remain provenance only. A stale
node or episode, stopped loop, missing durable binding, or wrong check host stays
visible and cannot silently become a generic conversation wake.
Before the atomic claim and before spending the invocation, RCP validates that
the bound session and exact stage still exist on the pinned machine. A transient
unavailability leaves the watchers unnotified for a later pass; a missing or
mismatched binding becomes an exact Needs-action diagnostic and never silently
launches a fresh session.

The active session already holds the immutable contract, so an automatic wake
sends a short continuation message rather than rebuilding it: what RCP accepted
from the preceding turn, the delivered watcher ids, fresh file pointers, and the
three valid exits. Execution machine, truth scope, and authority stay pinned for
the episode while graph, research, schema, and output pointers refresh every
turn. Provider, model, and reasoning also stay pinned unless the human explicitly
chooses **Switch provider** to recover a failed or paused invocation. Changed
repository, ontology, or package pointers
are appended as one compact replacement block that becomes the episode baseline
only after a mechanically successful joint Patch/watcher handoff. A graph-level
rejection is recorded truthfully and does not erase an otherwise accepted
operational handoff.

A recognized provider usage, session, quota, or credit limit is recoverable
inside the same episode and invocation. **Retry provider** rechecks the provider
and resumes the exact saved native session and stage; it does not spend another
invocation. **Switch provider** is an explicit human recovery action that keeps
the episode, invocation, execution machine, truth scope, governing decisions,
watchers, and operational history, but starts a provisional native session for
the chosen provider and sends the full current Experiment contract plus the
exact failure diagnostic and a prohibition on repeating completed operational
side effects. The active binding changes atomically only after that session
returns a mechanically successful joint Patch/watcher handoff. Failure leaves
the previous binding authoritative and the task recoverable. Automatic wakes,
watcher provenance, and silent fallback can never switch providers.

**Stop loop** is an idempotent, durable, restart-safe episode-level action
meaning "finish the current turn, then disable automatic continuation." RCP
persists the stop request before returning success and before any unclaimed
compatible watcher can win a new wake. With no unresolved loop task, it
terminally stops every compatible current or adopted watcher and settles
immediately; incompatible historical watcher work remains pending. An unresolved
loop task is the current turn and finishes normally: its valid Patch and semantic
bookkeeping apply, and those existing compatible watchers plus every valid
watcher its final handoff emits are retained as `stopped`. If the turn pauses, fails, or is interrupted, the
stop remains unsettled while the task stays available for Resume or Retry. A
claim that committed first wins; otherwise the stop wins and no wake task is
created. Stop never cancels the current task, kills external work, deletes a
watcher, edits Experiment status, creates or closes an attempt, or discards a
valid Patch. Task Resume and Retry remain recovery of the already-authorized
turn but can never clear the stop intent or reenable automatic delivery, and a
fresh human Run stays disabled until the turn resolves. The next Run starts a
fresh episode whose staged watcher state includes the stopped episode's records
as inspectable context with no delivered trigger.

Recovery of a bound episode never silently falls back to a fresh provider
session. If RCP proves the pinned session, exact stage, or continuation context
unusable, it records the exact diagnostic and rejects same-provider Resume or
Retry while offering an explicit provider switch when the pinned execution
machine and episode context remain usable. A subsequent or already-persisted
**Stop loop** may abandon only recovery of that
already-terminal task, with a durable receipt and all task, Patch, watcher, and
event history preserved; it terminalizes compatible watchers and settles so the
next human Run can establish a new authority boundary. An in-flight graph repair
that was created before Stop remains part of the authorized turn and may finish,
but Stop prevents launching a new repair from an old rejected result.

When the current episode reaches `invocation_ceiling`, RCP starts no automatic
wake. Completed ungrouped watchers and ready groups remain visibly pending and
unconsumed. The next human Run starts a fresh episode and, when a compatible
watcher or group is pending, atomically claims and delivers it as invocation 1
with its original attribution. This is the only counter reset; creating or
resolving a Proposal does not reset or resume the loop by itself.

Debug bookkeeping precommits a mechanical fault, change, and predicted effect
when the agent chooses to record a debug attempt. Scientific disappointment is
not a mechanical fault. Optional completion criteria are pinned and shown for
interpretation but never mechanically control start, retry, or exit. A Proposal,
Blocker, or other human-authority pause is an exit from the current episode;
after resolution, a human Run starts the next authorized episode.

### Experiment-loop context

Every budgeted invocation is self-sufficient without inventing a second context
system. The provider receives only the short immutable-contract pointer. The
staged contract file contains the normal RCP ontology, authority, method, local
causal check, focused-node and one-hop context, exact repository pointers, and
Patch, validator, watcher, schema, and artifact paths.

The contract points to a small per-invocation loop-control JSON file containing
only the phase, episode id, invocation counts, pinned governing Decision bundle,
live drift, advisory completion criteria, and delivered watcher or group ids.
Current watcher records are staged separately and named by path rather than expanded
into the contract. Semantic attempts remain in the Experiment in canonical
`graph.json`, and their agent-facing shape remains part of the existing Patch
schema; RCP does not stage a duplicate attempt snapshot or schema. It never
supplies prior chat transcripts.

The Experiment-loop contract builder and invocation-input staging are dedicated
modules. Generic Work prompt construction contains no Experiment-loop branch or
fallback wording. A missing or inconsistent Experiment, episode, invocation,
pinned ceiling, decision bundle, or watcher binding fails closed before provider
launch; RCP never substitutes semantic attempt counts or a generic Work contract.

An initial Run is marked as the beginning of an episode. A watcher wake resumes
the episode's native session and distinguishes the delivered coalesced watcher
set or immutable watcher group from other active, degraded, completed, or stopped
Experiment watchers. It never interprets one completion as an attempt boundary. Resume and Retry preserve the
original objective and binding but receive a compact live control file before
acting. Patch and watcher corrections receive only the retained contract,
current output paths, and exact diagnostics needed to repair their deliverable.
When a human Run reauthorizes pending completion, the phase explicitly names
human reauthorization: it is invocation 1 of the new episode while the staged
watcher records retain their older origin provenance.
The validator receives the immutable Experiment, episode, and pinned-decision
binding and validates against live canonical state; it receives no conversational
context. No additional loop-context validation layer exists: atomic invocation
admission and the existing live semantic Patch validator are the enforcement
boundaries.

The Experiment-loop Patch kind may update its own attempt/status, create
Evidence and Blockers, assert legal epistemic edges, attach newly created outputs
to its own Experiment, and create the two legal Proposal shapes within its pinned
upstream/tested boundary. It may not set standing, directly decide a Decision,
apply a Hypothesis status change, edit its pinned bundle, remove nodes, or use
status as control.

### Watch delivery

There are two physical watcher-file targets, with no discriminator field. An
ordinary conversation's non-empty `watch.json` suspends and later wakes that
same conversation. Its items remain strict: each contains only a self-contained
observational `check_command` with literal identifiers, an absolute `log_path`,
and an absolute `cwd`. An Experiment's watcher file instead owns that node's one
observer set and always wakes its live bounded loop. Every Experiment-loop
invocation writes that file; a permitted node or project Work conversation may
write the exact same resource while independently writing its own `watch.json`
in one turn. The file path is the targeting. No handoff contains a node,
episode, provider, session, host, or wake-kind field.

An Experiment watcher list may mix strict observer items, an optional non-blank
`group` label on an observer, and explicit `{stop_watcher_id, reason}` items. A
stop item names one staged watcher from the permitted project, Experiment, and
current compatible episode; it has a non-blank reason and no command or path.
Duplicate, unknown, already-notified, out-of-scope, or incompatible stop ids
reject the complete handoff atomically. An accepted stop permanently retires
that observer from polling and delivery, retaining its agent provenance, reason,
and time. It is the agent's statement that it has already settled the external
work with its existing Work tools, never RCP's claim to have cancelled that
work. A graceful **Stop loop** that retires those same observers first does not
invalidate the running turn's stop items: their retirement is already
satisfied, each record keeps the loop's own disposition, and the
already-authorized turn finishes normally instead of correcting a race it
cannot win.

An observer item without `group` keeps independent delivery. Observer items
with one label in one accepted Experiment-loop handoff form one immutable group
bound to that operation and label; a group contains at least two newly armed
observers. Later work creates a new group rather than changing membership.
Stop items never join a group. A non-empty Experiment handoff means an observer
continues, an old observer is retired, or both; after applying dispositions,
`[]` or a stop-only handoff that leaves no live observer is valid only when the
same Patch explicitly records success, a Proposal, or a Blocker that exits or
pauses the loop.

RCP admits Experiment watcher maintenance at one node-resource boundary using
the durable actor task: project, resolved node scope, Work capability, resource,
operation, current episode, and allowed fields. Client fields, path staging,
origin chat, maintenance provider, and maintenance execution machine grant no
authority. A node chat may maintain only its focused Experiment; a project Work
chat may maintain live Experiment resources in that project. Discuss may read
staged operational state but cannot mutate it. **Stop loop**, invocation budget,
native-session binding, governing Decisions, standing, and approvals remain
human-only or otherwise protected.

RCP binds a replacement to the target node and current episode, runs its initial
check on the episode's execution host, and retains the initiating operation and
chat only as creation or disposition provenance. Maintenance runs in and replies
to its own Work session, spends no Experiment invocation, creates no semantic
attempt, and never replaces the episode's last accepted handoff or native
session. Validation, initial checks, retirements, grouping, and replacement
inserts commit against one current snapshot; Stop, claim, or another maintenance
turn has one visible winner.

Watcher instructions describe the check location relative to the current Work
turn. When the Work turn and watcher execution host are the same machine, the
contract says **this machine** and directs the agent to use a local cold-login
shell; it must not name that host as a remote target or suggest SSHing back into
itself. Only a genuinely different watcher execution host is named as remote.

Checks run from a cold login shell with a hard timeout. Exit `0` means gone,
`1` means still present, and any other value means the check cannot answer.
Initial validation is atomic; one invalid item arms, retires, or groups none.
After arming, a healthy active observer schedules its next check two minutes
later. A timeout, runner or transport error, or other exit schedules durable
consecutive-error backoff at 2, 4, 8, 15, then 30 minutes; only exit `1` resets
that error count and healthy interval. Every delay has deterministic identity
jitter within plus or minus ten percent. `next_check_at` and the error count
survive restart, so no not-yet-due observer is polled early or reset into a
burst. Degraded errors never mean completion or automatic retirement, and only
exit `0` completes an observer. A stopped observer is never selected again,
regardless of an old due time.

A missing, malformed, initially uncheckable, or unexplained-empty
Experiment-loop handoff enters the same native session's loop-handoff correction
without spending another loop invocation. That correction inspects authoritative
external state and either writes valid observers for work that exists, or writes
`[]` plus a success/Proposal/Blocker Patch validated through the existing live
Patch validator. It cannot resubmit or alter completed operational work. If it
cannot establish either continuation or explicit exit, the task fails visibly
and stays Retryable. RCP never silently converts absence into “nothing to
watch.”

Completed compatible ungrouped watchers coalesce into one distinctly attributed
wake: a fresh Work turn outside Experiment control or the next Experiment-loop
invocation inside it. A grouped observer never enters delivery independently:
its group is
ready only when no member remains active and every non-retired member is either
complete or has reached five consecutive observation errors. The latter is one
diagnostic readiness condition at the capped backoff tier, not completion,
failure, or automatic retirement. A fully retired group is historical and never
wakes. Compatible ready groups may coalesce into one wake, but no group is split
or merged with another group. Queue creation, group or watcher claim,
episode-budget admission, and the notified ledger commit atomically, so restart,
polling races, callback retry, Resume, and Retry cannot create a second wake.
Compatibility is the current delivery policy, not the immutable origin episode
or invocation, so ready work from different invocations and arming conversations
on the same Experiment can share one wake while retaining individual
provenance. The transaction proves that the queued task still matches the
watchers' project, node, live episode, and check execution host. It
also distinguishes an automatic next invocation from a human Run that
reauthorizes pending completion as invocation 1 of a fresh episode. A wake never
occupies the human message slot, never mechanically creates or closes an
`ExperimentAttempt`, and never widens its bound Patch policy. It consumes one
Experiment-loop invocation only when the task is successfully queued. It never
races ahead of an active turn in the same conversation.

The final Experiment-loop Patch and watcher disposition are one recoverable
handoff keyed by the root operation for that invocation. RCP validates both
before committing either semantic reflection or new observers, uses the Patch's
source operation as an idempotent canonical commit identity, and gives the
watcher set deterministic identities. After interruption it reconciles an
already committed Patch or watcher set instead of appending or arming it again.
The durable episode-exit receipt is written only after the canonical exit Patch
is confirmed.

The agent never reads the watcher database. Before each loop turn and every
conversation allowed to inspect a live Experiment resource, RCP stages a
bounded watcher-state file in the exact scratch workspace and points to it from
the applicable contract. Work also receives the exact writable Experiment file
and an explicit statement that its completion wakes the bounded loop; the
conversation's own watcher pointer explicitly states that it wakes that
conversation. Discuss receives no Experiment mutation pointer. Staging is
discovery, not enforcement: ingest repeats the node-resource permission check,
including for a guessed path. The state names immutable group identity and membership,
per-member status, last error, consecutive-error count, and delivered watcher
or group ids, and retains a stopped observer's disposition provenance, reason,
and time. Its selection is explicit: an automatic wake stages every member
of every delivered group or ungrouped watcher even after its claim plus the
other relevant active, degraded, and completed-unnotified records; a fresh
initial Run stages no delivered ids alongside those observers and the immediately
preceding human-stopped episode's records; a human reauthorization stages the
claimed group even though it is now notified. Stopped records are context, never
triggers.

The mapping from an Experiment file to a wake target relies on the v1 invariant
that an Experiment has at most one live loop. If simultaneous live loops per
Experiment are ever allowed, node identity will no longer select one target and
this file contract must gain a deliberate replacement design rather than an
inferred fallback.

A group wake continuation names the immutable group and every member. It states
that no member is still observed active; exit-`0` members are only gone, while
degraded members have unknown external state and must be inspected before the
agent relaunches, cancels, or records an outcome. This diagnostic prompt changes
neither the episode-native session, Patch authority, nor invocation accounting.

RCP never infers that a degraded watcher is dead. Loop-level **Stop loop** is the
Experiment's human operational authority, so Experiment Runs offers no
per-watcher human Stop action; an ordinary Work watcher keeps its individual
**Stop watching** authority. An Experiment agent may instead retire only a
staged compatible observer by the explicit file disposition above, after its own
operational work has settled it. Neither human nor agent observer retirement
changes any semantic attempt. Human Stop atomically acknowledges any unclaimed
active, degraded, or just-completed watcher; it cannot race a claimed
notification into waking afterward. An agent disposition and notification claim
also have one atomic winner. A watcher whose Experiment was removed is
terminally retired rather than poisoning later delivery passes. These
dispositions are shown as timeline events, not scientific conclusions.

Live-output delivery, durable output offsets, debounce/batching for output,
repository leases, stale-record policy, direct graph manipulation, and graph-wide
scheduling remain open in [`open-questions.md`](open-questions.md).

## Background tasks, concurrency, and provider readiness

Agent work is durable background work owned by the RCP process, not by a browser
view. Seed, Refresh, Discuss, Work, Experiment-loop, watcher wakes, corrections,
and Paper coaching share task persistence, events, Pause/Resume/Retry, and native
session receipts without sharing policy.

Unrelated tasks may run concurrently, including multiple conversations in one
project. Only overlapping turns in the same conversation are refused because
they share a native session, scratch outputs, and validator mailbox. Canonical
Patch publication remains serialized by its append lock.

A paused attempt remains actionable until Resume or Retry creates its child.
Successful child completion supersedes the paused banner and does not block later
messages. Retry and Resume preserve exact mode, capability, stage provenance, and
external-side-effect diagnostics.

Provider readiness is an app-process concern. Startup begins an asynchronous,
coalesced probe shared across windows and callers. Readiness is cached for its
configured lifetime and explicit Refresh bypasses the cache. UI navigation never
owns or repeats warmup, and warmup never disables ordinary application use.

## Remote execution and canonical locks

Remote `.research/.agent-run.lock` and `.research/.refresh.lock` are regular
advisory-lock files held by a dedicated SSH child whose remote process owns an
OS `flock`. File existence is not ownership. Process or connection death releases
the lock without deleting a marker path.

Publication is fenced by the same lock owner. Bytes may stage under
`.research/.publish/`, but only the process holding `.refresh.lock` moves them to
canonical paths. A Patch commit point is observed as present, absent, or unknown;
RCP never falls back to an unfenced apply after losing ownership.

Live contention waits and stays nonterminal. RCP automatically reclaims only an
empty legacy lock directory it can prove safe. A populated directory, symlink,
or special entry is preserved and reported because ownership cannot be proved;
the product never tells the human to delete a lock path manually.

Exactly one RCP process owns a data directory. `open` reuses a healthy owner or
gracefully replaces an unavailable one; explicit `serve` performs the same
graceful takeover after recoverable work is paused.

## Reader-facing application surfaces

- The project shell places one compact project-tab dock immediately to the right
  of its back/index control. The dock has one capped span: inactive tabs shrink
  proportionally as it fills, the active tab retains a wider share, and labels
  truncate without horizontal dock overflow. Opening a project from a project
  card or the cross-project Experiments board appends and activates its named
  tab, while reopening a docked project activates it without duplicating or
  moving it. The dock remains visible on the project index. `Command–T` returns
  there without closing tabs; `Option–Command–Left` and
  `Option–Command–Right` wrap through open project tabs unless focus is in an
  editable control.
- Each tab restores that project's panel and ephemeral in-session view state.
  Closing a tab removes only that dock entry: it never deletes the project,
  changes canonical state or staged drafts, or stops background work. Closing
  the active tab selects the right neighbor, otherwise the left, and closing the
  last tab returns to the index; reopening a closed project starts at Overview.
  Tabs cannot be reordered, and deleting a project through the index removes its
  tab. Open tabs survive hiding and reopening the same desktop window but reset
  on page reload or full app quit/relaunch. A docked inactive project is not kept
  mounted or polled merely because its tab is open.
- The **project index** keeps project cards first, followed by one distinct
  cross-project **Experiments** board. The board includes only Experiment nodes
  with loop history and projects each node's current or latest episode into one
  compact horizontal row. It reports current state using the same health truth
  as Runs: Needs action before In progress, with all Finished rows folded by
  default and successful, abandoned, and superseded outcomes kept distinct.
  Last-known rows from an unavailable project remain visible and labelled
  unavailable. Selecting a row opens that project's Runs detail; the index adds
  no loop-control authority and no unread or since-last-visit bookkeeping.
- **Overview** shows current project state and the latest plain-language revision
  summary.
- **Inbox** contains pending Hypothesis-status Proposals, Decisions whose status
  is `ready` or `revisit`, and open Blockers whose standing remains asserted.
  A Decision is a row that opens its existing node inspector and ballot; a
  Proposal, which is not a node, keeps its inline judgment controls. Accepted
  and contested open Blockers remain operational graph state but no longer
  await human judgment. Historical Ambiguities never render or count.
- **Research** presents question-centered graph paths and a bounded DAG view.
  Its Research-flow layout gives each `has_subquestion` depth a successive
  horizontal column, then places Hypothesis/Decision, Experiment/Blocker, and
  Evidence in their semantic-stage columns after the deepest visible question.
  Other relations affect vertical ordering but never question depth.
- **Runs** is the operational control surface for Seed/Refresh research
  ingestion, bounded Experiments, and asserted open graph Blockers—not generic
  chat or coaching tasks. It carries no page title and is ordered by what matters
  now: **Running**, **Needs action**, then **Completed**, with the first matching
  state winning. Accepted and contested Blockers leave **Needs action** after
  Sync without being operationally resolved.
  An Experiment-loop task is the deliberate exception to the chat exclusion
  because its Patch kind and control node make it research execution. Pressing an
  Experiment's Run navigates here and opens its run detail rather than a floating
  node-chat window. That detail reports loop health, current activity, invocation
  budget, watcher health and provenance, and each immutable watcher group as its
  own operational unit with per-status summary, member status, and degraded-error
  detail. It reports resolved execution with native-session continuity and the
  Experiment's meaning. Failed or paused loop turns expose direct **Retry
  provider** and **Switch provider** recovery alongside the independent
  loop-level **Stop loop** action. The detail does not include an **Open agent
  task** button; full task history and diagnostics remain available from History.
- **Chats** groups node and project conversations with immutable turn labels,
  inline task progress under the triggering message, and no global task banner.
- **Paper** provides a human-authored Markdown Write/Preview pane and read-only
  coaching.
- **Settings** owns project execution profiles, repositories, and enabled
  packages, but not ontology authoring.
- **History/Agent tasks** retains complete operational attempts, continuations,
  diagnostics, provider identity, graph outcomes, and versions of staged
  packages.

Glossary terms already in history render as best-effort whole-term inline
definitions in node prose, chat answers, and Proposals. There is no standalone
Glossary destination and no new glossary-authoring authority until the open
question is decided.

Node detail is a persistent, resizable floating inspection window that clamps to
the viewport and closes when entering Chats. Its Relations section is a stable
vertical one-hop map: incoming neighbors above, the focused node in the middle,
outgoing neighbors below, and every labelled relation visible without a nested
scroll area. Selecting a neighbor opens one companion detail beside the original;
at most two detail windows remain open, with reachable overlap instead of
unreadable shrinking on narrow viewports. Expanding the map opens the same
one-hop graph in a full-screen overlay without navigating away. Selecting a node
there updates one compact read-only inspection card, whose explicit action may
open the full node window; the overlay adds no graph-authoring authority. Chat
list width and Paper/editor split are likewise adjustable where specified by
their acceptance scenarios. A Decision detail promotes its question and
deduplicated options into a visually distinct, accessible single-choice ballot
above prose Context, with lifecycle, canonical selection, and staged selection
visible.

Revision summaries are producer-authored ordinary prose with titles rather than
ids, operation names, or inventory counts. Rendering deterministically resolves
known historical ids, preserves unknown slash-delimited text, derives truthful
fallbacks from operations when prose is absent, and quotes only consequences
already stored in a Proposal. It never invents scientific causality or writes a
second canonical summary.

## Paper authorship boundary

The paper introduction is human-authored, non-authoritative Markdown. Its
canonical sections cover the research question, adjacent questions, literature,
high-level methods, main results, and why the work deserves publication and
communication.

The writing coach may read the draft and graph, identify unsupported claims,
ask focused questions, and point to relevant evidence. It may not edit the draft,
emit replacement prose, write a Patch, or turn paper text into graph truth.
Provider-native session ids preserve coaching continuity without importing prior
RCP transcript content as a new authority source.

## Implementation architecture

The application is one FastAPI backend with SQLite operational storage and one
React/Vite frontend, optionally wrapped by Tauri. Codex CLI and Claude Code run
as local or SSH subprocesses. Provider registry, capability profiles, context
assembly, run policies, transport, history, graph validation, paper storage,
skills, and web views remain explicit module boundaries documented in
[`../AGENTS.md`](../AGENTS.md).

Pydantic models are the schema layer. Agent-facing schemas are strict. Limits,
timeouts, retention windows, and cache bounds live in central configuration;
schema constants remain next to the model contract they constrain.

The frontend is never the owner of background work. Desktop window close, app
Quit, backend ownership, packaging, and update behavior follow their acceptance
scenarios and must preserve resumability and ownership truth.

An open project probes only the current accepted canonical graph revision while
visible. A newer revision triggers the existing authoritative project
reconciliation, so graph content and Proposals converge across browser and
desktop clients without manual reload. An unchanged probe neither replays
history nor returns the graph. Reconciliation preserves client-side human drafts;
the existing stale-base state exposes conflicts after the graph advances.

## Verification and change discipline

The blueprint defines current decisions. Acceptance scenarios define observable
completion. A code change is complete only when its relevant scenario passes at
the cheapest driver that can prove the behavior, plus repository baseline checks.
UI behavior requires a served-app browser drive; remote behavior requires a
reachable host.

Canonical history and materialized files are never edited to make tests pass.
When implementation and blueprint disagree, record the disagreement and resolve
it deliberately. Undecided matters go to [`open-questions.md`](open-questions.md)
rather than being guessed inside implementation.
