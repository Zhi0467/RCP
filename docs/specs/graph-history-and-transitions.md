# Graph, history, and transitions

This spec owns graph meaning, typed operations, transition preparation, replay,
and history reads. [Authority and Proposals](authority-and-proposals.md) owns
permission; [design](../design.md#cross-cutting-invariants) owns global invariants.

## Product ontology

Current authoring uses these node types:

| Type | Meaning |
| --- | --- |
| ResearchQuestion | A question being resolved |
| Hypothesis | A scoped falsifiable claim with semantic status |
| Decision | A choice governing research execution |
| Experiment | A bounded test, optional completion criteria, and attempt ledger |
| Evidence | An observation with provenance |
| Blocker | An observable impediment to starting an Experiment |

Each has an id, title, content, provenance standing, and type-specific fields.
Standing is `asserted`, `contested`, or `accepted` on nodes only; there is no
`confidence` field. Nested attempts, proxies, sources, options, and causes are
records, not nodes. Proposal, glossary, ontology, and historical Ambiguity are
side-car state. Historical ontology extensions and Ambiguities replay, but current
surfaces expose neither Ambiguity creation nor ontology editing in Settings.

An Experiment's `proxies` list `{stands_for, measure}` pairs; `limitations` lists
what the proxies and protocol miss. Both describe the design before results and
cannot be edited by an episode.

Model field descriptions and `RELATION_SPEC` own meanings, timing, and local
method. The graph-rules renderer combines them with code-owned endpoints, id
prefixes, and value vocabularies for agent contracts. The same descriptions fill
the Patch schema. Cross-node method remains prose. Authority belongs to each
task's contract; see
[graph rules in task contracts](providers-and-containment.md#graph-rules-in-task-contracts).
**Why:** copied field meanings drift from validation, while permission differs
between surfaces and must not be embedded in shared descriptions.

Kernel code asks `project_type_of(state)` for type roles, relations, retirement
fields, and prose names. `core/research_type.py` supplies research answers;
type-specific rules and rendering stay in the explicit research layer. The Web
mirror is `web/src/graph/researchType.ts`; the research-layer tests in
`tests/test_project_types.py` and `web/tests/projectType.test.mjs` prevent new
research-type knowledge elsewhere. Wire names need not be renamed.
**Why:** research meaning can vary while history and operations stay general.
Every project currently uses research, so persisting a one-value project type
would add a migration without behavior. A second type would need canonical
identity, model/schema registration, routed type rules, and an API-owned Web
ontology; this boundary alone does not make types pluggable.

## Evidence and claim-relative assessments

Evidence records `observation`, `interpretation`, provenance, source/artifact
references, `origin`, methodological `validity`, and `role` (`result` or
`diagnostic`). Current writes reject node-global `strength`.

Every new Evidence-to-Hypothesis epistemic edge requires an assessment:
`relevance` (`direct`, `indirect`, `contextual`), `weight` (`limited`, `moderate`,
`strong`), optional bounded `scope`, and bounded normalized unique
`qualifications`. Direction stays in `supports`, `weakens`, `refutes`,
`inconclusive`, or Evidence-sourced `contradicts`. All other relations reject
assessments, including Hypothesis-to-Hypothesis `contradicts`.

One observation can carry different weight for different claims. RCP neither
aggregates scores nor infers Hypothesis status, standing, Decisions, Proposals,
or Evidence validity. Historical strength remains visible as `legacy_strength`:
`diagnostic` maps to that role, other values to `result`, never to edge weight.
Old epistemic edges without assessments remain readable as unassessed.
`research.md` lists an assessment under accepted research only when both the
Hypothesis and the Evidence are accepted and the Evidence is not invalid.

## Relations and control structure

Relations have a closed vocabulary and legal endpoint types. ResearchQuestion
`has_subquestion` ResearchQuestion forms question trees, and `has_hypothesis`
connects a question to its claims. New `has_decision` edges connect only
Hypothesis to Decision; that ordinary association does not gate an Experiment.
Experiment `tests` Hypothesis, is `governed_by` Decision and `blocked_by` Blocker, and
`produces` Evidence. New `blocked_by` edges connect only Experiment to Blocker.
A Decision may be purely an Experiment input through `governed_by`, with no
Hypothesis attachment.
Evidence `informs` Decision and `addresses` Blocker, besides its epistemic
relations. `addresses` does not resolve a Blocker; resolution is an authorized
lifecycle update.

`requires_decision` is retired from authoring. Its historical spec and layer,
legacy Question→Decision and Question/Decision→Blocker edges, and their ids
remain readable and replay unchanged. Admission checks new edges only; unrelated
writes can retain legacy edges, and removal does not revalidate old endpoints.
Removing and recreating an edge is a new write, even for an explanation edit.
Retired names remain reserved against reuse as custom relations.

Only Experiment `produces` Evidence accepts `expectation`: `matched`, `diverged`,
or `no_expectation`, with `explanation` naming the relevant `expected_outcomes`.
It rejects Evidence assessments. Expectation belongs on the edge because an
observation may come from several Experiments or none. A divergence calls for
investigation of either the protocol or the finding.

Epistemic and action layers project one graph. Only understood relations affect
Experiment control. Precursor Evidence informs downstream gates; downstream
outputs are not attached backward as precursor inputs. New writes reject
relations whose endpoint types do not fit.

Structural checks protect ids, references, endpoints, uniqueness, strict shape,
and replay safety. Live checks add comprehensibility, causal coherence, writer
rules, and task admission. Stricter live rules do not invalidate structurally
valid history; replay rules accept fields introduced by compatibility adapters.

## Typed graph operations

`Patch.ops` is an ordered strict `GraphOperation` union. Its `op` discriminator
and persisted payload keys are stable:

| Family | Operations |
| --- | --- |
| Nodes | `create_nodes`, `update_nodes`, `remove_nodes`, `supersede_nodes`, `merge_nodes` |
| Edges | `create_edges`, `remove_edges` |
| Proposals | `create_proposals`, `resolve_proposals`, `withdraw_proposals` |
| Historical Ambiguities | `create_ambiguities`, `resolve_ambiguities` |
| Metadata and judgment | `upsert_glossary`, `set_standing`, `set_project_truth_scope`, `set_ontology` |

An operation without an apply rule raises, never silently no-ops.

Supersede and merge set the project type's retirement field to `superseded`
(`validity` for Evidence, `status` for other base types) and add `supersedes` or
`duplicate_of`. They retain nodes and edges; removal deletes incident edges too.
Research rendering omits retired Evidence. Historical `set_coverage` decodes with
no graph effect and is rejected on admission. Reading reports are absent from
snapshots, agent context, outputs, validation, and merge comparisons.

Glossary entries are project-wide definitions, not nodes or hover-generated text.
They follow the Patch's graph target. Identity is case-insensitive; admission
preserves existing spelling or the first new spelling in the Patch. For historical
cased duplicates, revision updates the first sorted spelling used by inline lookup
without deleting entries. Historical operations replay as recorded.

Admission gives nonblocking advice for newly introduced internal-run Evidence
without a producing Experiment, disconnected nodes, and normalized duplicate
titles of the same type. Any real connection clears disconnected-node advice;
it never asks agents to invent a scientific relation. Losing the last relevant
connection can introduce advice. The transition manager checks the complete final
candidate against the initial graph once, after generated effects. Validator exit
0 and Sync preview expose the same advice; replay does not rerun it.
**Why:** deterministic checks can flag shape, not scientific meaning. Quality
advice belongs in validation, not a mandatory second scanner.

Operation models reject extra fields and wrong types, including nested payloads.
Proposal operations use their intent-restricted union. Persisted-input adaptation
handles supported older wire shapes without rewriting bytes. Malformed input
reports operation index and field, plus Proposal identity for nested errors, and
never reaches admission. Core consumers use typed models; JSON conversion belongs
only at serialization and compatibility boundaries.

## Append-only history and graph targets

The canonical repository holds a main Patch log and persistent episode branch
namespaces. Human Sync publishes one visible batch by atomic directory rename;
hidden staging is ignored. Materialized graph, research, glossary, Proposal,
control, and branch files are replaceable outputs. Materialization replaces
container slots without mutating objects shared with earlier revisions.

Targets are `main` or `branch:<branch_id>`. Heads include target, integer revision,
and last transition id. Branch metadata binds project, chain-root episode/id,
kind, authorizer, creation time, immutable parent head, current head, and receipts.
An episode branch's parent head is a main head; a fork's is any exact main or
branch head.
Branch creation builds the complete layout in a unique temporary sibling and
atomically renames it into place under the write lock. Abandoned temporary
directories do not occupy the branch identity or block a retry.
Continuations write under their own episode ids to the root's branch namespace.
Replay uses the parent's replayed prefix through the parent head, nesting through
each ancestor branch down to main, then the branch log; movement of any ancestor
never rebases it. See [Forks](auto-research-and-branch-merge.md#forks) and
[persistent branches](auto-research-and-branch-merge.md#persistent-graph-branch).

## Patch provenance

RCP stamps `producer` (`human`, `agent`, or reserved `system` for identity and
migration), authorizer (`space_id`, `user_id`, display-name snapshot), agent
profile/task, episode where applicable, and source operation/effect identity.
Agents cannot supply their own authority. Branch Patches name the target; a main
merge also records source branch/episode/base/head, prepared main head, merge
task, and human dispatcher. Summary prose cannot replace those identities.

## Transition preparation

The synchronous transition manager handles Sync, Work and episode Apply,
Experiment handoff, branch Apply, and merge through the
[two core gates](authority-and-proposals.md#two-permission-gates).

A prepared transition records target/pre-head, ordered initiating groups and
provenance, generated operations and stable rule ids, causes referring to earlier
actions/events, lifecycle events, ruleset, final graph/head, Experiment control,
guidance validity, and mutation projections. Each semantic payload is stored
once; actions, causes, and events refer to positions. Deterministic hashes of
these persisted contracts form transition and event ids. Models reject coercion.

`rcp.lifecycle.v3` records one event per lifecycle-field change. Older rulesets
recorded `status` alone; replay computes trace events under their named ruleset.

## Rule closure

A closed backend registry gives each rule a stable id, typed trigger, deterministic
read set, outputs, and order. Rules cannot depend on time, randomness, UI, provider
output, or SQLite. Preparation applies initiating operations in order, appends
generated operations, and evaluates to closure. A bounded firing guard rejects
cycles, contradictory effects, or nontermination without partial append.

Rules derive control/readiness, invalidate affected guidance, emit lifecycle
events, and build final projections. Only open Blockers gate Experiments. Rules
do not write scientific prose, infer phases or Hypotheses, choose Decisions,
create Evidence, approve Proposals, delete resolved Blockers, or serve layout.

## Blockers, Experiment phase, and guidance

Resolving or superseding a Blocker retains it and its relations but removes its
gate. Active flow/attention omits it; history/detail retains it. Resolution
watchers fire once from the final committed event.

`blocked` is not a current Experiment phase. Old history adapts it in memory to
unwritable `unspecified`, displayed as phase not recorded; opening writes no
migration. Derived control alone expresses blocking/readiness.

`current_summary` and `next_action` are authored text with backend-owned validity.
Gate changes mark affected nonempty fields stale, including text written in that
transition. Only a later explicit authorized update refreshes that field. Active
UI never presents stale text as current. Invalidators include `blocked_by`, linked
Blocker lifecycle, governing Decisions and pending Proposals, and causal upstream
Evidence relations, assessments, or lifecycle. Dependencies use current graph
paths, not a second authored graph.

## Human preview and agent correction

A conservative trigger manifest carries the ruleset tag. Safe local edits may
update drafts directly; possible triggers, missing manifests, and mismatched tags
require backend preview. Preview names its base/ruleset and never appends. Failure
retains invalid input and the last valid candidate, naming the initiating action
and rule. Sync re-prepares the full batch against the locked head and commits all
or none.

Agent conflicts return bounded operation indexes, rule, cause chain, affected ids,
and invariant to the same native session's correction path. Rejected preparation
creates neither a revision nor a receipt claiming a commit.

Unreachable canonical state or a lost lock yields `unavailable`, not a semantic
`rejected` verdict. Its commit status is `absent`, `present`, or `unknown`; it is
not correctable or repairable. A replay halt remains a non-correctable rejection
until human repair. Tasks retain their Patch text.

Ordinary conversation Work offers **Apply again** through
`POST …/tasks/{operation_id}/apply-graph-update-again` with patch-capable identity
and merge fencing. A human click reapplies retained text without a provider run,
using the same source binding and current validation. It is offered for all
three commit statuses. Under the canonical run lock, refresh precedes binding
checks: an existing commit is recorded, not appended twice, even if subsequent
materialization fails. Failures before that check preserve stored `present` or
`unknown` certainty.

The outcome replaces the task graph update by compare-and-swap, then records task
events and chat receipts. Applied drops retained text; rejected may offer Repair;
unavailable may offer Apply again. Only the latest receipt offers actions; the
task projects `can_apply_again`, and status/wake summaries name commit status.

Apply again refuses missing text, an active turn in the same chat, and Experiment
or Auto-research child turns whose bookkeeping it would skip. It also refuses if
a later chat turn committed or might have committed (`applied`, or `unavailable`
with `present`/`unknown`), unless this turn's own binding is already canonical.
An `absent` update is checked before locking too; every update is checked after
refresh under the lock before append, covering later turns admitted while waiting.

## Coherent projections and operational events

Successful mutations return one `ProjectTransitionProjection`: graph, control,
guidance validity, head, transition id, and ruleset from the final state. No-change
responses use one current materialization and preserve its head.

Canonical history and SQLite cannot share a transaction. Consumers track stable
canonical events with separate exact main/branch watermarks and process them
idempotently in head order. A crash after append converges watcher, task, episode,
and merge-receipt state without repeating a Patch or event; SQLite never claims
an uncommitted append.

The [digest](api-web-and-desktop-projections.md) uses a server-side member/project
cursor over commit-ordered events. A new member marker starts empty at the current
cursor; an existing marker advances only on **Caught up**, to the displayed cursor.
**Why:** opening a project is not reading it, and acknowledging "now" would hide
work completed while the digest was on screen. Per-member server storage keeps
attention independent across people and consistent across devices.

## Replay and compatibility

Replay validates strict transition provenance and applies recorded expanded
operations in order without rerunning rules. Accepted heads must form one exact
target chain. Divergent/wrong-target transitions or structurally invalid accepted
revisions halt at the last coherent state, exposed read-only; they are not skipped.
Rejected candidates remain chronological receipts without semantics.

Link-rule retirement changes admission, not historical meaning. Old plain
Patches, expanded transitions, and cached projections retain their recorded
edges and layers. Recorded v3 lifecycle traces retain their interpretation
under the v4 ruleset; schema generation and Proposal dependency rules do not
change. Pending Proposals that would create an illegal edge remain inspectable
and withdrawable, but cannot be approved or refiled with that effect. A branch
merge carries the branch's edges as admitted, including a branch-only edge of a
since-retired shape: the branch judged it under the rules of its day, so main
keeps it as it keeps its own legacy edges, and consolidation re-homes both. A
merge that stopped on such an edge would leave its branch with no way to merge.
An edge shape neither side admitted, assembled by a merge answer, meets the current
rules, and a violation returns the answer for revision.

Supported older Patch, operation, Evidence, Experiment, and transition shapes adapt
in memory. Later ordinary writes may stamp current schema generation; unsupported
future generations require an update and remain read-only. Schema generation and
ruleset are independent. When retiring a field, update
`adapt_persisted_patch_document` and affected replay field rules together, including
adapter-introduced fields. Historical bytes never change.

Every graph model field in `core/models.py` carries one `GraphField` role:
`content`, `engine` (recomputed bookkeeping, never compared), or `identity`.
Proposal fields also name what RCP owns: filing stamps (dependencies, rule
version, filer, revisions) and resolution fields, which filing resets. The agent
Patch schema strips both sets, filing writes both, and merge identity excludes the
filing stamps plus resolution revisions; each set derives from the annotations. A pending source Proposal is a new filing on the destination with
current filing stamps, including its dependency rule, while retaining its identity
and authored content. Ordinary destination filings use rule 2. Only connected
multi-intent merge groups and atomic protected-edge replacements use rule 3,
whose dependency and approval semantics are defined in
`authority-and-proposals.md`. Replay preserves the recorded dependency rule;
that rule still controls staleness and approval standing.
Engine-written status and standing remain semantic. **Why:** v0.4.23 versioned
the dependency rule, and every branch holding an older pending Proposal failed
its merge self-check because the lists of filing fields had drifted apart. The
Proposal regression in `tests/test_branch_merge.py` varies the Literal and
integer filing fields through merge preparation, and the every-release update
test in `tests/test_server_upgrade.py` merges a branch each published release
populated. Checks for a persisted graph change:
`tests/test_branch_merge*.py`, `tests/test_episode_merge.py`,
`tests/test_history*.py`, `tests/test_agent_schema.py`, and
`tests/test_application_deployment.py`.

## History reads

One engine serves every history reader and materialization path. References are
`main@R`, `branch:<id>@R`, or either target's `@head`; resolved heads carry transition
ids. Missing, out-of-range, and pre-base revisions refuse rather than clamp.
Rejected receipts remain valid heads with previous accepted semantics.

Step records hold before/after heads and states, decoded Patch/operations, semantic
delta, and changed node/edge ids. Changes, merge context, `RefreshDelta`, revision
summaries, digest, consolidation coverage, and agent queries project these records.
Each keeps its own policy: canonical initiating operations prove merge authorship,
refresh retains routing/bounds, and raw `/history` includes rejected receipts.
Materialization, output repair, and branch Apply use the same coherent prefix on
degraded history; only mutation refuses it.

`diff(A, B)` is net semantic change regardless of ancestry, not a merge plan.
Branch Changes compares immutable base to head. Branch logs show accepted main
steps through the base as inherited, retaining `main@R` heads, then branch steps.
Main readers do not expose branch content through merge provenance.

The engine is shared across callers, including per-request branch wrappers, with
bounded memory and incremental append. Rebuildable checkpoints every K revisions
and a changed-id index live under `project-caches/<project-hash>/graph-history/`,
excluded from canonical history, SSH mirrors, backups, and transfers. Keys digest
all retained Patches including rejections, replay context, and engine version.
Missing, corrupt, or mismatched entries rebuild; canonical append never waits on
the cache. Each query pins one verified generation across pages.
**Why:** shared steps avoid per-view replay; checkpoints and the index bound cold
reads after restart. Keeping original operations preserves intent and authorship
that a semantic delta alone loses, without introducing another canonical log.

## Canonical publication

The state workspace owns atomic local/remote publication. Remote `.agent-run.lock`
and `.refresh.lock` files require live advisory lock holders; existence proves
nothing. Bytes may stage under `.research/.publish/`, but only the lock owner
publishes. Remote commit `present` succeeds and repairs outputs, `absent` rolls
back the unpublished mirror, and `unknown` quarantines it until canonical refresh
proves the outcome. There is no unfenced fallback apply.
