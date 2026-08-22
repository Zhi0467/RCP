# Graph, history, and transitions

This specification owns RCP's canonical research graph, typed semantic
operations, append-only histories, transition preparation, replay, and graph
head identity. Authority to request those operations is defined in
[Authority and Proposals](authority-and-proposals.md).

## Product ontology

Current authoring uses six node types:

- **ResearchQuestion** — a question being resolved;
- **Hypothesis** — a falsifiable claim with an explicit scope and semantic
  status;
- **Decision** — a choice that governs research execution;
- **Experiment** — a bounded test with optional completion criteria and an
  agent-authored semantic attempt ledger;
- **Evidence** — a durable observation with provenance; and
- **Blocker** — a concrete impediment to progress.

Every node has an id, title, ordinary-language content, provenance standing,
and type-specific fields. Standing is `asserted`, `contested`, or `accepted`
and belongs to nodes, never edges. `confidence` is not a graph field.

Nested records such as Experiment attempts, sources, Decision options, and
belief causes are not nodes. Proposal, glossary, ontology, and historical
Ambiguity records are side-car state. Historical ontology extensions and
Ambiguities remain replayable; current authoring cannot create Ambiguities and
Project Settings does not expose ontology authoring.

## Evidence and claim-relative assessments

An Evidence node describes the observation itself:

- `observation` and `interpretation`;
- provenance, source references, and artifact references;
- `origin`;
- methodological `validity`; and
- methodological `role`, either `result` or `diagnostic`.

Current writes cannot set a node-global `strength`. `diagnostic` describes the
observation's method role; it is not a claim-level weight.

Every newly admitted epistemic edge from Evidence to Hypothesis carries one
strict assessment:

- `relevance`: `direct`, `indirect`, or `contextual`;
- `weight`: `limited`, `moderate`, or `strong`;
- optional bounded `scope`; and
- a bounded, normalized, nonduplicated list of `qualifications`.

The relation still owns direction: `supports`, `weakens`, `refutes`,
`inconclusive`, or Evidence-sourced `contradicts`. The assessment does not
repeat direction. Hypothesis-to-Hypothesis `contradicts`, Evidence `informs`
Decision, Evidence `addresses` Blocker, and every other relation reject an
Evidence assessment.

The same Evidence can therefore be strong and direct for one scoped Hypothesis
and limited or contextual for another. RCP neither combines several assessments
into a score nor infers a Hypothesis status, standing, Decision, Proposal, or
Evidence validity from them.

Historical `Evidence.strength` is a no-write compatibility input. The exact
value remains visible as `legacy_strength`; `diagnostic` also becomes the
current in-memory role, while every other old value yields role `result`. No old
ordinal label is mapped to an edge weight. A historical epistemic edge without
an assessment remains readable as an unassessed legacy relation.

## Relations and control structure

Relations use a closed typed vocabulary with legal endpoint types. Core shapes
include:

- ResearchQuestion `has_subquestion` ResearchQuestion;
- ResearchQuestion framing Hypotheses, Decisions, Experiments, and Blockers;
- Experiment `tests` Hypothesis;
- Experiment `governed_by` Decision;
- Experiment `blocked_by` Blocker;
- Experiment `produces` Evidence;
- Evidence `informs` Decision and `addresses` Blocker; and
- the Evidence-to-Hypothesis epistemic relations above.

Epistemic and action layers are projections over one graph. Only relations
whose semantics RCP understands may affect Experiment control. A precursor
Experiment produces Evidence that informs or addresses a downstream gate; a
downstream output is not attached backward as that precursor's input.

Structural validation protects ids, references, endpoints, uniqueness, strict
shape, and replay safety. Live authoring validation additionally protects
comprehensibility, authority, causal coherence, and task-specific admission.
Tightening live authoring does not invalidate a structurally valid historical
record.

That holds for authority validation too. In-memory adaptation may retire a
value and mark what it invalidated, so replay legitimately sees fields on an
operation that the original write never carried and a live write still may not
set. Every authority rule that lists the fields a Patch may change accepts those
adapted fields on replay; refusing them halts canonical history over RCP's own
migration.

## Typed graph operations

`Patch.ops` is an ordered list of the strict discriminated `GraphOperation`
union. The existing top-level `op` discriminator and persisted payload keys are
stable. Current operation families are:

- `create_nodes`, `update_nodes`, `remove_nodes`, `supersede_nodes`, and
  `merge_nodes`;
- `create_edges` and `remove_edges`;
- `create_proposals`, `resolve_proposals`, and `withdraw_proposals`;
- historical `create_ambiguities` and `resolve_ambiguities`;
- `upsert_glossary`;
- `set_coverage`;
- `set_standing`;
- `set_project_truth_scope`; and
- `set_ontology`.

Every operation model rejects extra fields and wrong current types, including
strict nested node, assessment, ontology, source, attempt, and Proposal
payloads. Proposal semantic operations use a narrower union discriminated by
their explicit `intent`; they cannot smuggle an arbitrary graph operation.

Compatibility decoding is centralized at the persisted-input boundary. It may
adapt a valid older wire shape into the current typed in-memory model, including
old Proposal intent and prior field names, without rewriting the source bytes.
Malformed current input reports its operation index and field; malformed nested
Proposal input also identifies the Proposal. It never reaches canonical
admission.

Consumers dispatch on the typed models. Converting back to JSON is allowed only
at explicit serialization and compatibility boundaries; core validation,
authority, materialization, transition preparation, and history summaries do
not independently reinterpret dictionaries.

## Append-only history and graph targets

The project canonical repository contains one append-only main Patch log and,
when Auto-research has run, persistent append-only graph-branch namespaces.
Main and branch Patch files are never edited or deleted. A human Sync becomes
one atomically published visible batch. Hidden staging is ignored until its
directory rename.

A graph target is either `main` or `branch:<branch_id>`. A graph head always
contains its target, integer revision, and last transition id; a bare integer is
not globally unique. An Auto-research branch additionally records its project,
episode, immutable base main head, branch kind, authorizing human snapshot,
creation time, current head, and merge receipts.

A branch materializes by replaying the accepted main prefix through its immutable
base and then its branch Patch log. Mutable main materializations are never
copied and treated as branch truth. Main can advance without changing the branch
base or branch history.

Materialized `graph.json`, `research.md`, glossary, Proposal, coverage, control,
branch summary, and related files are derived outputs. Materialization replaces
container slots rather than mutating objects shared with an earlier revision.
Routes never hand-edit these outputs or canonical Patch files.

## Patch provenance

A Patch records its semantic operations and RCP-owned provenance. `producer` is
`human`, `agent`, or reserved `system`. `system` is limited to RCP-owned
identity and migration revisions. New human and ordinary-agent Patches snapshot
the root authorizer as `space_id`, `user_id`, and display name. Agent Patches
also carry profile and direct task identity; episode work carries its episode
id. RCP, not an agent payload, supplies these fields.

Branch Patches identify their branch target. A committed branch merge on main
also names the source branch and episode, branch base and head, main head used
for the candidate, merge task, and human dispatcher. Free-text summary is never
the only merge identity.

## Transition preparation

The synchronous backend transition manager is the sole semantic path from an
initiating graph intent to a new main or branch revision. It handles human Sync,
ordinary Work Apply, Auto-research Apply, Experiment-loop handoff, branch Apply,
and branch merge.

A prepared transition contains:

- its target and pre-head;
- ordered initiating operation groups with producer provenance;
- ordered generated operations;
- a stable rule id for every generated operation;
- cause references to earlier actions or events;
- ordered lifecycle events with stable event ids;
- the ruleset tag;
- the final state and head;
- final graph-derived Experiment control;
- per-field guidance validity; and
- every other mutation projection returned to the client.

One semantic operation payload is stored once; action, cause, and event records
refer to its position. Transition and event ids are deterministic hashes of the
persisted action, head, ruleset, and provenance contract. Current transition
models are strict and reject type coercion.

## Rule closure

Rules are typed backend functions in one closed registry. Each has a stable id,
typed trigger, deterministic read set and outputs, and stable order. A rule may
not depend on time, randomness, UI state, provider output, or SQLite.

Preparation applies initiating operations in written order and evaluates rules
to closure. Generated operations follow the initiating operations. A bounded
firing guard rejects cycles, contradictory effects, or nontermination without a
partial append.

The current registry is deliberately narrow:

- only open Blockers gate Experiments;
- gate-affecting changes invalidate affected Experiment guidance;
- readiness and control derive from the final graph;
- lifecycle changes emit attributable events; and
- the mutation projection is built from the final candidate.

The manager does not author scientific prose, infer a scientific phase, alter a
Hypothesis, choose a Decision, create Evidence, approve a Proposal, delete a
resolved Blocker, or modify the graph to satisfy layout.

## Blockers, Experiment phase, and guidance

Resolving or superseding a Blocker retains the node and its relations in current
canonical state. Because only `open` gates, the final control projection stops
treating it as a gate. Active Research-flow and attention projections omit it by
default; history and direct detail retain it. A status-resolution watcher sees
the committed final event and fires once.

`blocked` is not a current intrinsic Experiment phase and current authoring
rejects it. Older history maps `blocked` in memory to the unwritable
`unspecified` compatibility phase, presented as phase not recorded. Derived
control alone says whether the Experiment is blocked or ready. Opening such a
project writes no migration revision.

`current_summary` and `next_action` remain agent- or human-authored scientific
text. Backend-owned validity metadata says whether each field is current. A
gate-affecting transition marks every nonempty affected field stale, including
guidance written in that same transition. Only a later explicit authorized
update makes that individual field current again. Active UI does not present
stale text as current guidance.

Invalidators include `blocked_by` changes, linked Blocker lifecycle changes,
governing Decision or relevant pending-Proposal changes, and upstream
Evidence-to-Hypothesis relation or assessment changes for a causally dependent
Experiment. RCP uses the current control relations and graph causal paths; it
does not maintain a second authored dependency graph.

## Human preview and agent correction

The backend publishes a conservative transition trigger manifest with its
ruleset tag. A local human edit that cannot trigger a rule may update the draft
immediately. A possibly triggering edit, a missing manifest, or a mismatched tag
uses backend preview. Preview is non-canonical, names its base head and ruleset,
and never appends history.

A failed preview preserves both the person's invalid input and the last valid
draft candidate, and attributes the conflict to its initiating action and rule.
Sync re-prepares the complete staged batch against the locked current head and
commits one revision or none.

Agent Apply uses the same manager. A conflict identifies bounded operation
indexes, rule, cause chain, affected ids, and invariant and returns that input to
the same native session's correction path. No rejected preparation creates a
canonical revision or an operational receipt claiming that it did.

## Coherent projections and operational events

Every successful mutation response carries one `ProjectTransitionProjection`:
graph, Experiment control, guidance validity, head, transition id, and ruleset
from the same final state. A no-semantic-change response uses one current
materialization and preserves its exact head rather than synthesizing a new
identity.

Canonical history and SQLite cannot share one transaction. Lifecycle events are
therefore stable canonical facts. Each operational consumer records an exact
graph-target watermark and applies later events in canonical head order,
idempotently. Main and branch consumers never share a watermark. A crash after
canonical append but before watcher, task, episode, or merge-receipt projection
converges without repeating the Patch or delivering an event twice; SQLite never
claims an append that has not committed.

## Replay and compatibility

Replay validates strict persisted transition provenance, then applies recorded
expanded operations in order. It never reruns rules. Accepted transition heads
must form one exact target-specific chain; replay halts before a divergent or
wrong-target accepted transition and exposes the last coherent state in degraded
read-only form.

Rejected historical candidates remain chronological receipts and contribute no
semantics. A structurally invalid accepted revision halts replay; RCP never skips
it and invents a later state.

Older supported Patch, operation, Evidence, Experiment, and transition shapes
adapt in memory without writes. A later ordinary mutation may stamp the current
schema generation. A future unsupported generation makes an older RCP
read-only and asks for an update. Persisted schema generation and transition
ruleset tag are independent.

## Canonical publication

Local and remote publication is atomic and owned by the state workspace. Remote
`.agent-run.lock` and `.refresh.lock` are regular files held by live advisory
lock-holder processes; file existence is not ownership. Remote bytes may stage
under `.research/.publish/`, but only the process holding the canonical lock
publishes them.

A remote Patch commit is observed as present, absent, or unknown. Present
succeeds and repairs derived outputs, absent rolls back an unpublished mirror,
and unknown is quarantined until a canonical refresh proves the outcome. No
unfenced fallback apply is allowed.

## Verification contracts

The core durable journeys are [S13 replay halt](../acceptance/S13-replay-halts.md),
[S74 boundary failure](../acceptance/S74-boundary-inputs-fail-closed.md),
[S76 graph-condition wake](../acceptance/S76-graph-condition-wake.md),
[S81 live canonical state](../acceptance/S81-live-canonical-state.md), and
[S125 branch merge](../acceptance/S125-auto-research-graph-branch-merge.md).
