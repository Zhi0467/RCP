# Graph, history, and transitions

This spec owns graph meaning, typed operations, transition preparation, replay,
and history reads. [Authority and Proposals](authority-and-proposals.md) owns
permission. [AGENTS.md](../../AGENTS.md#stable-invariants) owns global invariants.

## Product ontology

ResearchQuestion, Hypothesis, Decision, Experiment, Evidence, and Blocker are the
current node types. [Core models](../../src/rcp/core/models.py) and their field
descriptions own their fields, meanings, and value vocabularies. Standing belongs
to nodes. Nested attempts, proxies, sources, options, and causes are records.
Proposal, glossary, ontology, and historical Ambiguity are side-car state.
Historical ontology extensions and Ambiguities replay, but current surfaces expose
neither Ambiguity creation nor ontology editing in Settings.

An Experiment's `proxies` field lists `{stands_for, measure}` pairs.
Its `limitations` field lists what the proxies and protocol miss.
Both describe the design before results. An episode cannot edit either field.

Model field descriptions and `RELATION_SPEC` own meanings, timing, and local
method. The graph-rules renderer combines them with code-owned endpoints, id
prefixes, and value vocabularies for agent contracts. The same descriptions fill
the Patch schema. Cross-node method remains prose. Authority belongs to each
task's contract. See
[graph rules in task
contracts](providers-and-containment.md#graph-rules-in-task-contracts).
**Why:** copied field meanings drift from validation, while permission differs
between surfaces and must not be embedded in shared descriptions.

Kernel code asks `project_type_of(state)` for type roles, relations, retirement
fields, and prose names. `core/research_type.py` supplies research answers.
Type-specific rules and rendering stay in the explicit research layer. The Web
mirror is `web/src/graph/researchType.ts`. The research-layer tests in
`tests/test_project_types.py` and `web/tests/projectType.test.mjs` prevent new
research-type knowledge elsewhere. Wire names need not be renamed.
**Why:** research meaning can vary while history and operations stay general.
Every project uses research. This code boundary is not a pluggable type system.

## Evidence and claim-relative assessments

Evidence describes observations, interpretation, provenance, validity, and role.
Current writes reject node-global strength. Every new Evidence-to-Hypothesis
epistemic edge requires the claim-relative `EvidenceAssessment` from
[core models](../../src/rcp/core/models.py). Direction stays in the relation.
Other relations, including Hypothesis-to-Hypothesis `contradicts`, reject assessments.

One observation can carry different weight for different claims. RCP neither
aggregates scores nor infers Hypothesis status, standing, Decisions, Proposals,
or Evidence validity. Historical strength remains visible as `legacy_strength`.
The value `diagnostic` maps to the diagnostic role. Other values map to `result`.
No historical strength maps to an edge weight.
Old epistemic edges without assessments remain readable as unassessed.
`research.md` lists an assessment under accepted research only when both the
Hypothesis and the Evidence are accepted and the Evidence is not invalid.

## Relations and control structure

[RELATION_SPEC](../../src/rcp/core/models.py) owns the closed vocabulary and
legal endpoints. Question trees use `has_subquestion`.
Questions connect to claims through `has_hypothesis`.
`has_decision` associates a Hypothesis with a Decision without gating experiments.
`governed_by` supplies Experiment decisions. `blocked_by` supplies Blockers.
`tests` identifies claims. `produces` provides Evidence provenance.
Decisions may be pure Experiment inputs. Evidence `informs` choices and
`addresses` Blockers. Addressing a Blocker does not resolve it.

Historical `requires_decision`, Question→Decision, Question→Blocker, and
Decision→Blocker edges retain their specifications, layers, and ids.
They replay unchanged. New authoring rejects retired shapes.
Unrelated writes and removal do not revalidate retained edges.
Recreating an edge requires new admission, even for explanation edits.
Retired names remain reserved.

Only Experiment→Evidence `produces` accepts an expectation.
Its explanation names the expected outcomes. It rejects epistemic assessments.
Expectation belongs to the edge because Evidence can come from multiple
Experiments or none. Divergence asks for investigation, not automatic judgment.
The epistemic and action layers project one graph.
Only understood relations affect control. Downstream outputs are not attached
back to earlier nodes as precursor inputs.

Structural validation protects ids, references, endpoints, uniqueness, shape,
and replay. Live checks add causal coherence, comprehensibility, writer rules,
and admission. Stricter live checks cannot invalidate structurally valid
historical adapter fields.

## Typed graph operations

`Patch.ops` is an ordered strict `GraphOperation` union defined in
[core operations](../../src/rcp/core/operations.py). Its discriminator and persisted
payload keys are stable. An operation without an apply rule raises an error.
It never silently does nothing.

Supersede and merge set the project type's retirement field to `superseded`
(`validity` for Evidence, `status` for other base types) and add `supersedes` or
`duplicate_of`. They retain nodes and edges. Removal deletes incident edges too.
Research rendering omits retired Evidence. Historical `set_coverage` decodes with
no graph effect and is rejected on admission. Reading reports are absent from
snapshots, agent context, outputs, validation, and merge comparisons.

Glossary entries are project-wide definitions, not nodes or hover-generated text.
They follow the Patch's graph target. Identity is case-insensitive. Admission
preserves existing spelling or the first new spelling in the Patch. For historical
cased duplicates, revision updates the first sorted spelling used by inline lookup
without deleting entries. Historical operations replay as recorded.

Admission gives nonblocking advice for newly introduced internal-run Evidence
without a producing Experiment, disconnected nodes, and normalized duplicate
titles of the same type. Any real connection clears disconnected-node advice.
The advice never asks agents to invent a scientific relation. Losing the last relevant
connection can introduce advice. The transition manager checks the complete final
candidate against the initial graph once, after generated effects. Validator exit
0 and Sync preview expose the same advice. Replay does not rerun it.
**Why:** deterministic checks can flag shape, not scientific meaning. Quality
advice belongs in validation, not a mandatory second scanner.

Operation models reject extra fields and wrong types, including nested payloads.
Proposal operations use their intent-restricted union. Persisted-input adaptation
handles supported older wire shapes without rewriting bytes. Malformed input
reports the operation index and field. Nested errors also report Proposal identity.
Malformed input never reaches admission. Core consumers use typed models.
JSON conversion belongs only at serialization and compatibility boundaries.

## Append-only history and graph targets

[Invariant 1](../../AGENTS.md#stable-invariants) owns append-only canonical history
and replaceable projections. Human Sync publishes one visible batch by atomic
directory rename, ignoring hidden staging. Materialization replaces container
slots without mutating objects shared with earlier revisions.

Targets are `main` or `branch:<branch_id>`. Heads include target, integer revision,
and the last transition id. Branch metadata binds the project, chain-root episode
and its id, kind, authorizer, and creation time. It also binds the immutable parent
head, current head, and receipts.
An episode branch's parent head is a main head. A fork's parent head is any exact
main or branch head.
Branch creation builds the complete layout in a unique temporary sibling and
atomically renames it into place under the write lock. Abandoned temporary
directories do not occupy the branch identity or block a retry.
Continuations write under their own episode ids to the root's branch namespace.
Replay uses the parent's replayed prefix through the parent head, nesting through
each ancestor branch down to main, then the branch log. Movement of any ancestor
never rebases it. See [Forks](auto-research-and-branch-merge.md#forks) and
[persistent branches](auto-research-and-branch-merge.md#persistent-graph-branch).

## Patch provenance

RCP stamps `producer` (`human`, `agent`, or reserved `system` for identity and
migration), authorizer (`space_id`, `user_id`, display-name snapshot), agent
profile and task, episode where applicable, and source operation and effect
identities. Agents cannot supply their own authority. Branch Patches name the
target. A main merge also records the source branch, episode, base, and head.
It records the prepared main head, merge task, and human dispatcher too.
Summary prose cannot replace those identities.

## Transition preparation

The synchronous transition manager handles Sync, Work and episode Apply,
Experiment handoff, branch Apply, and merge through the
[two core gates](authority-and-proposals.md#two-permission-gates).

[Transition models](../../src/rcp/core/transition_models.py) own prepared and
persisted payloads. Each semantic payload is stored once. Actions, causes, and
events refer to positions. Deterministic hashes form transition and event ids.
Models reject coercion.

`rcp.lifecycle.v3` records one event per lifecycle-field change. Older rulesets
recorded `status` alone. Replay computes trace events under their named ruleset.

## Rule closure

A closed backend registry gives each rule a stable id, typed trigger, deterministic
read set, outputs, and order. Rules cannot depend on time, randomness, UI, provider
output, or SQLite. Preparation applies initiating operations in order, appends
generated operations, and evaluates to closure. A bounded firing guard rejects
cycles, contradictory effects, or nontermination without partial append.

Rules derive control and readiness, invalidate affected guidance, emit lifecycle
events, and build final projections. Only open Blockers gate Experiments. Rules
do not write scientific prose, infer phases or Hypotheses, choose Decisions,
create Evidence, approve Proposals, delete resolved Blockers, or serve layout.

## Blockers, Experiment phase, and guidance

Resolving or superseding a Blocker retains it and its relations but removes its
gate. Active flow and attention omit it. History and detail retain it.
Resolution watchers fire once from the final committed event.

`blocked` is not a current Experiment phase. Old history adapts it in memory to
`unspecified`, which cannot be authored. The UI displays it as phase not recorded.
Opening the project writes no migration. Derived control alone expresses blocking
and readiness.

`current_summary` and `next_action` are authored text with backend-owned validity.
Gate changes mark affected nonempty fields stale, including text written in that
transition. Only a later explicit authorized update refreshes that field. Active
UI never presents stale text as current. Invalidators include `blocked_by`, linked
Blocker lifecycle, governing Decisions and pending Proposals, and causal upstream
Evidence relations, assessments, or lifecycle. Dependencies use current graph
paths, not a second authored graph.

## Human preview and agent correction

The conservative trigger manifest names its ruleset. Only safe local edits update
drafts directly. Possible triggers or an absent or mismatched manifest require
backend preview. The preview names its base and ruleset. It never appends.
Failure retains the invalid input and last valid candidate, with diagnostics
for the action and rule. Sync prepares the whole batch again under the lock.
It commits all of the batch or none. Agent correction returns bounded diagnostics
for the operation, rule, cause, id, and invariant to the same native session.
Rejection creates no revision or false commit receipt.

An unreachable canonical store or a lost lock yields an unavailable outcome,
not a semantic rejection. Commit certainty is absent, present, or unknown.
This outcome is neither correctable nor repairable. A replay halt remains
non-correctable until a human repairs it. Tasks keep the Patch text.
Ordinary Work Apply again reuses that text without a provider. It uses the
captured source identity, current validation, authority capable of writing a
Patch, and merge fencing.

Under the canonical run lock, refresh precedes source-binding checks.
Existing commits are recorded, never duplicated, even if later materialization
fails. Earlier failures retain present or unknown certainty.
Compare-and-swap replaces the graph outcome before task events and chat receipts.
An applied outcome drops the retained bytes. A rejected outcome may offer Repair.
An unavailable outcome may offer Apply again. Only the latest receipt offers
actions. Status and wake summaries expose commit certainty.

Apply again refuses missing bytes, active same-chat turns, or episode child turns
whose bookkeeping it would bypass. Later possibly committed chat turns also block
unless this turn's binding is already canonical. Outcomes with absent commit
certainty also run these checks before locking. All outcomes run the checks
after refresh under the lock to cover concurrent admissions.
All three commit statuses may offer Apply again.
[Its owner](../../src/rcp/runs/tasks/work_apply_again.py) keeps request eligibility
and settlement together.

## Coherent projections and operational events

Successful mutations return one `ProjectTransitionProjection`: graph, control,
guidance validity, head, transition id, and ruleset from the final state. No-change
responses use one current materialization and preserve its head.

Canonical history and SQLite cannot share a transaction. Consumers track stable
canonical events with separate exact watermarks for main and each branch.
They process the events idempotently in head order.
After a crash following append, watcher, task, episode, and merge-receipt state
converge without repeating a Patch or event. SQLite never claims an uncommitted
append.

The [digest](api-web-and-desktop-projections.md) uses a server-side cursor for each
member and project over commit-ordered events. A new member marker starts empty
at the current cursor. An existing marker advances only on **Caught up**.
It advances to the displayed cursor.
**Why:** opening a project is not reading it, and acknowledging "now" would hide
work completed while the digest was on screen. Per-member server storage keeps
attention independent across people and consistent across devices.

## Replay and compatibility

Replay validates strict transition provenance and applies recorded expanded
operations in order without rerunning rules. Accepted heads must form one exact
target chain. A divergent transition, a transition for the wrong target, or a
structurally invalid accepted revision halts replay at the last coherent state.
That state is exposed read-only. Replay does not skip the invalid revision.
Rejected candidates remain chronological receipts without semantics.

Link-rule retirement changes admission, not historical meaning. Old plain
Patches, expanded transitions, and cached projections retain their recorded
edges and layers. Recorded v3 lifecycle traces retain their interpretation
under the v4 ruleset. Schema generation and Proposal dependency rules do not change.
Pending Proposals that would create an illegal edge remain inspectable and
withdrawable. They cannot be approved or refiled with that effect.

A branch merge carries what its branch admitted under the rules of its day.
Main keeps its own history. Consolidation re-homes retired shapes on both.
Admission of the orchestrator's merge Patch receives the source head's state as
`ValidationContext.merge_source`. Content rules skip an edge, created node, or
update only when the merge writes it exactly as that head holds it.
`core/validation/carried.py` owns these exceptions. They cover edge shape,
evidence endpoints and assessments, legacy shapes, Evidence origin, grounded
Hypothesis scope, and queued ballots.

Identity, authority, human-only fields, a human's deprecation, belief causes,
source scope, and Proposals are checked on everything.
Content assembled by a merge answer must meet today's rules.
An invalid edge shape returns the answer for revision.
A merge that stopped on carried content would leave its branch with no way to merge.

Supported older Patch, operation, Evidence, Experiment, and transition shapes adapt
in memory. Later ordinary writes may stamp the current schema generation.
Unsupported future generations require an update and remain read-only. Schema
generation and
ruleset are independent. When retiring a field, update
`adapt_persisted_patch_document` and affected replay field rules together, including
adapter-introduced fields. Historical bytes never change.

Every graph model field declares a `GraphField` role: content, engine bookkeeping,
or identity. Proposal annotations distinguish filing stamps from resolution fields.
The agent schema strips both. Filing resets both.
Merge identity excludes filing stamps and resolution revisions.
Source Proposals retain their identity and content but refile with destination
stamps. Replay retains the recorded rules.
[Proposals](authority-and-proposals.md#protected-belief-proposals) owns staleness
and approval for each rule. Engine-written status and standing remain semantic.
They do not automatically count as bookkeeping.
**Why:** deriving these sets from annotations prevents drift between schema,
replay, and merge.
Verify filing-field variation and older published-writer branches, not just current
fixtures: `tests/test_branch_merge*.py`, `tests/test_episode_merge.py`,
`tests/test_history*.py`, `tests/test_agent_schema.py`,
`tests/test_server_upgrade.py`, and `tests/test_application_deployment.py`.

## History reads

One engine serves all history and materialization. References name exact revisions
or heads on main or a branch. Resolved heads include transition ids.
Agent commands also accept current branch names.
Storage resolves these names before scope checks. Pure engine
references remain canonical ids. Responses add mappings for queried names.
Missing revisions, revisions before the base, and out-of-range revisions are
refused. They are never clamped. Rejected receipts remain valid heads with the
preceding accepted semantics.

Shared step records carry the state and head before and after each step.
They also carry decoded operations, the semantic delta, and changed ids.
Changes, merge, refresh, summaries, digest, coverage, and queries project these
records under their own policies. Initiating operations prove merge authorship.
Refresh keeps its bounds and routing. Raw history includes rejections.
Materialization, repair, and branch reads share the coherent prefix when history
is degraded. Mutation refuses degraded history. Diff reports net semantics
independent of ancestry. It does not plan merges.
Branch Changes compares the immutable base with the head. Branch logs retain
inherited main heads followed by branch steps. Main readers never expose branch
content through provenance.

The shared engine uses bounded incremental memory. Its rebuildable checkpoints
and changed-id index live under project caches. They are excluded from canonical
history, mirrors, backup, and transfer. Cache keys digest all retained Patches,
including rejected Patches, along with replay context and version.
Missing, corrupt, or mismatched caches rebuild without blocking canonical append.
Each paged query pins a verified generation.
**Why:** shared steps and checkpoints bound reads while preserving operation intent
and authorship. They do not require another canonical log.

## Canonical publication

The state workspace owns atomic local and remote publication.
Remote `.agent-run.lock` and `.refresh.lock` files require live advisory lock
holders. Their existence alone proves nothing. Bytes may stage under
`.research/.publish/`, but only the lock owner publishes.
A remote commit with outcome `present` succeeds and repairs outputs.
An `absent` outcome rolls back the unpublished mirror.
An `unknown` outcome quarantines it until canonical refresh proves the outcome.
There is no unfenced fallback apply.
