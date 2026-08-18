# Graph transition manager design checkpoint

Date: 2026-08-17

## Purpose

Checkpoint the diagnosis and ongoing design interview for a backend graph transition
manager. This is an active handoff, not an accepted scenario or implementation plan.
No production code has been changed for this work.

The next session should continue the design interview, settle the remaining decisions,
then follow `AGENTS.md`: write and confirm the acceptance scenario before changing the
blueprint, planning implementation, or writing code.

## Confirmed incident

A real project reached a contradictory revision:

- Revision 73 set an Experiment's stored `status` to `blocked`, wrote a blocked
  `current_summary` and `next_action`, created a Blocker, and linked the Experiment with
  `blocked_by`.
- Revision 76 changed the Blocker status to `resolved`.
- Revision 77 removed the Blocker. Materialization also removed its incident edge.
- At revision 79, neither the Blocker nor the edge existed.
- The backend Experiment-control projection correctly reported `ready: true` with no
  gate reasons.
- The Experiment still stored `status: "blocked"` and the obsolete blocked prose.

The relevant canonical mirror is under the normal RCP state-cache directory. Do not
include identity records or other personal fields from that data in reports.

This is not an orphan-edge defect or primarily a stale materialization. It is a split
truth in the lifecycle model:

- `src/rcp/control.py` derives Experiment readiness from the current graph.
- `src/rcp/core/models.py` independently permits `Experiment.status = "blocked"`.
- `current_summary` and `next_action` can independently retain obsolete language.
- human Sync currently returns only a `GraphState`; `web/src/App.tsx` can splice that
  graph into a project snapshot while retaining the prior `experiment_control` map until
  a later reload.

## Confirmed design direction

### One backend mutation boundary

Use one synchronous backend graph transition manager as the only boundary at which a
human or agent request can become a canonical graph revision.

The manager is not:

- an asynchronous repair worker;
- a second mutable state store;
- a frontend rule engine; or
- a replacement for Auto-research or Experiment-loop operational policy.

The canonical graph and append-only transition history remain the source of truth. UI
caches and backend display caches are disposable projections.

### Rules are closed backend code

Lifecycle rules live in one closed, code-owned registry of typed deterministic backend
functions. They are not project settings, user-authored configuration, or a general rule
DSL.

Each rule has a stable id, an explicit trigger contract, and explicit generated actions and
events. Rule execution cannot depend on a database query, wall clock, randomness, an agent,
or UI state. Only reviewed RCP code changes the registry.

The registry also produces a compact conservative trigger manifest, cached by the client
with the ruleset tag. The browser uses that manifest only to answer whether a staged edit
*could* fire a rule:

- if no rule can fire, the browser updates the local draft without a backend request;
- if a rule could fire, the browser asks the backend manager for a draft preview;
- false positives are acceptable, but false negatives are not; and
- a missing or mismatched manifest falls back to backend preview.

The client never implements rule outcomes.

### A request is not a revision

A human or agent Patch is initiating intent and provenance. It is not itself a revision.
For every proposed transition, the manager:

1. loads one current canonical pre-state under the existing ownership and append locks;
2. checks the initiating action and authority;
3. stages the requested semantic operations;
4. applies relation-specific deterministic rules in stable order until closure;
5. records protected scientific or human judgments as review/invalidation facts rather
   than guessing them;
6. validates the complete post-state and all manager invariants; and
7. commits the initiating actions, generated actions, ordered events, causes, and final
   post-state as one canonical revision, or commits nothing.

Intermediate rule states may appear in the ordered transition trace, but they are never
independently publishable revisions.

### Canonical revisions retain the causal trace

Every committed revision records one ordered, auditable transition trace rather than only
an unexplained final operation list. Each action identifies:

- its semantic operation;
- whether it came from the initiating human or agent request or from a manager rule;
- the stable `rule_id` for a generated action;
- the earlier action or event that caused it; and
- emitted lifecycle events such as `Blocker open -> resolved`.

Replay applies the recorded semantic operations in order and does not rerun the rules or
need the provenance metadata to reproduce state. Watcher delivery, authority auditing,
diagnostics, and UI explanations may consume the durable trace.

Keep this representation compact: store each semantic operation payload once plus ids and
provenance references. Never duplicate whole pre- or post-transition graphs in the trace.

### Every revision is manager-valid

The central invariant is:

> A state is not a graph revision until the initiating action and all deterministic
> triggered rules have reached closure and the resulting graph satisfies the manager's
> complete rule set.

This applies to every graph-writing surface.

Confirmed revision boundaries:

- one human **Sync** is one manager transition and one revision, even when it contains
  many ordered initiating actions;
- one Auto-research `apply` command is one revision;
- separate Auto-research `apply` commands remain separate revisions; and
- one Experiment-loop end-of-turn Patch is one revision.

This changes the current human batch behavior: `HistoryManager.append_batch_from_state`
currently records several individually replayable Patch revisions inside one atomically
published batch. A future Sync must not contain rule-incomplete intermediate revisions.

### Human staging and agent Patch preparation are different surfaces

A human staged edit is not a commit. The human should nevertheless see current rule effects
without waiting for Sync:

1. apply the edit to the local non-canonical draft;
2. use the cached conservative trigger manifest;
3. when no rule could fire, update the draft immediately without a backend round trip;
4. when a rule could fire, run a backend manager preview and show the returned draft graph
   or exact conflict; and
5. on Sync, rerun the complete draft transition against current canonical state under the
   append lock and either commit one revision or commit nothing.

For Auto-research Apply and Experiment-loop end-of-turn Patch apply, stage all initiating
operations in written order and attempt rule closure. Rule evaluation is best-effort for
diagnostics: it should identify the initiating operations, rules, and generated effects
involved in a failure so the existing agent correction mechanism can repair the Patch.
Best-effort never means partial canonical commit.

Incompatible rule effects are not automatically manager defects. Several graph edits in
one Patch may trigger individually sensible rules whose consequences contradict one
another. That is a transition conflict attributable through the causal trace. Agent prompts
should prefer a single causal edit per Patch unless bundled edits are known not to causally
contradict each other.

### Replay uses committed actions, not historical rules

Each committed revision may carry a manager ruleset tag as provenance. The tag is not a
migration mechanism:

- historical tags are never bumped, retagged, or rewritten;
- replay never queries or re-executes a historical ruleset;
- replay applies the fully expanded actions recorded in each committed revision, in
  their recorded order; and
- a later ruleset tag applies only to later revisions.

The manager guarantee is enforced when the revision is created. Replay remains
mechanical and deterministic.

### Schema evolution is invisible and append-only

An RCP schema update must not create migration work for the user or an agent. Opening an old
project in a newer RCP is a canonical no-op: no Patch is rewritten, no migration revision is
appended, and no project-open hook mutates the graph.

A small centralized schema-compatibility module at the decoding/materialization boundary
upcasts older persisted shapes into the current in-memory model using conservative,
deterministic adapters. An adapter runs only when its older shape is encountered. Current
manager and UI code receive the current model rather than carrying scattered legacy
branches.

Compatibility rules are:

- every persisted-schema change ships with its backward-compatible reader/adapter in the
  same RCP update;
- old Patch bytes and append-only history remain untouched;
- the first ordinary mutation produced by the newer RCP may stamp the current schema
  generation, but it is not a migration revision;
- an older RCP that encounters a newer schema generation is read-only and says that an RCP
  update is required; and
- a schema change that cannot conservatively read older valid data does not ship until that
  compatibility is designed.

CI must keep representative prior-generation fixtures and prove compatibility,
materialization, and unchanged canonical history. A valid older project failing to open is
an RCP compatibility defect, not an inbox task. This schema generation is separate from the
manager ruleset tag; neither replay nor compatibility code queries historical rulesets.

### Blocker resolution is a status transition and rule trigger

`resolved` remains a real `Blocker.status` value. Do not replace it with an action-only
model.

The ordinary semantic transition is:

```text
Blocker.status: open -> resolved
```

Whether that update came from an authorized human editor or an authorized agent, the
manager observes the transition and triggers this chain in the same revision:

1. record the `open -> resolved` transition event;
2. invoke the resolved-Blocker removal rule;
3. remove the Blocker from the candidate current graph;
4. remove all incident relations;
5. find the relation-dependent closure;
6. recompute deterministic downstream projections and invalidations; and
7. validate and commit the final graph.

The final current graph contains no resolved Blocker. Its resolution remains durable in
the append-only action trace and history.

If any rule or final invariant fails, the entire transition fails and the pre-state
remains current.

### Existing graph-derived Experiment gating is retained

Do not introduce a second `gates[]` model. RCP already has the correct core mechanism:

- `experiment_graph_precondition_reasons` in `src/rcp/control.py` derives gating from
  current `governed_by` Decisions, pending Proposals, and open `blocked_by` Blockers;
- the node drawer renders the resulting text; and
- the embedded Relation map renders the current incident subgraph.

The derivation function is not itself the bug. The risk is computing or transporting it
separately from the graph revision.

The manager's committed result must therefore feed one backend-built projection in which
the graph, graph-derived Experiment control, and causal layout inputs all identify the
same canonical revision. The UI must never combine a new graph with an old control map or
rederive lifecycle truth locally.

All downstream control code consumes what the manager committed, never what the human or
agent requested.

### `blocked` is derived Experiment state

An Experiment can and should be blocked. What is rejected conceptually is an independent
mutable truth that can contradict the causal graph.

Confirmed model direction:

```text
intrinsic Experiment phase: implementing
manager-derived graph state: blocked
effective presentation: blocked
```

After the final open Blocker is resolved and removed:

```text
intrinsic Experiment phase: implementing
manager-derived graph state: eligible
effective presentation: implementing / eligible
```

`blocked` is therefore a manager-derived fact, not an independently authorable intrinsic
Experiment phase. The existing graph-derived readiness and reasons should implement this
rather than a duplicate gate store.

Historical Patch logs already contain `Experiment.status = "blocked"`; they must remain
byte-for-byte unchanged and decodable for append-only replay. The exact conservative,
no-write compatibility adapter from that legacy value to an intrinsic current phase is not
yet decided.

### Retain stale guidance, but do not present it as current

`current_summary` and `next_action` are interpretive text, not gating truth. The manager
cannot deterministically invent replacement scientific prose when an upstream dependency
changes.

Confirmed behavior:

- retain the old text on the Experiment and in history;
- record backend-owned validity/staleness for each field;
- a stale field must not be presented as the Experiment's current guidance;
- updating one field against current graph truth clears only that field's staleness; and
- the other field remains stale until explicitly refreshed.

The exact schema and UI presentation are not yet decided. The semantic distinction is
settled: retain the context, but do not silently label it current.

Confirmed invalidation boundary:

- propagate through the relation-specific causal descendant closure for semantic changes,
  including ResearchQuestion, Hypothesis, Decision, or Blocker lifecycle status;
- include a Decision's selected option, Evidence validity, causal relation creation or
  removal, and upstream node removal or supersession; and
- do not invalidate downstream guidance for cosmetic edits such as title wording,
  rationale phrasing, or source-reference changes.

Protected conclusions remain protected: invalidation does not choose a Decision, judge a
Hypothesis, answer a ResearchQuestion, or invent a new scientific summary.

### Watchers observe transition events

Existing graph-condition watchers can wait for a Blocker to reach `resolved`. Because the
same manager revision then deletes that Blocker, watcher evaluation cannot rely only on a
final node lookup.

The committed transition trace must expose the intermediate `open -> resolved` event. A
watcher armed before the transition consumes that event exactly once even though the final
graph lacks the node.

A watcher declared by the same Experiment-loop end-of-turn handoff is armed after that
revision and observes only later transitions; it must not retroactively consume the
transition that preceded its arming.

### Auto-research integration

Auto-research keeps its own operational policy, session, worker, budget, command, and
idempotency machinery. Its Patch paths become clients of the same graph manager.

The staged `validate` command should run a manager dry-run against the current graph:

1. parse and authority-check the proposed Patch;
2. expand deterministic manager rules to closure;
3. validate the final candidate; and
4. report the preview without writing a revision.

The `apply` command reruns the same preparation under the canonical write lock. Validation
is advisory until this locked rerun, as it is today.

Current useful seams to preserve:

- Auto-research in-turn Apply already routes through `_apply_work_patch` in
  `src/rcp/runs/work.py`;
- `source_effect_id`, Patch hashes, and the durable Apply ledger provide idempotency;
- final settlement reuses an exact already-committed in-turn Apply rather than spending a
  duplicate revision; and
- multiple in-turn Apply commands may intentionally create multiple manager-valid
  revisions.

The manager returns the committed graph result and transition receipt. Auto-research may
continue with refreshed canonical state, but the manager does not spawn workers, spend
episode budget, or choose Auto-research continuation.

### Experiment-loop integration

The Experiment loop also retains its distinct authority, correction, watcher-handoff,
native-session, budget, and episode policies.

Its flow becomes:

1. validate the agent-authored `patch.json` and `watch.json` under Experiment-loop
   authority;
2. send the graph Patch through the manager exactly once;
3. receive the committed post-state and ordered transition receipt;
4. persist/arm future watcher work under existing Experiment-loop policy; and
5. decide the operational ending from the manager's final result, not from raw agent
   intent.

The manager may generate deterministic effects outside the Experiment agent's direct
edit scope only under an explicit system rule id and causal provenance. Those effects do
not widen the agent's authority.

Confirmed control principle:

> Experiment control uses what the manager returned, not what an agent or human requested.

For example:

- creating an open Blocker that remains in the final graph is a genuine pause;
- resolving a Blocker causes automatic deletion, so it is not a remaining pause merely
  because the raw Patch mentioned a Blocker; and
- completing the controlled Experiment is an ending only when the manager's committed
  result contains the valid completed state.

Operational work may have happened even if its graph reflection is rejected. Preserve the
current separation: record that operational outcome truthfully and use the existing
correction/repair policy; never pretend external work rolled back with a graph rejection.

### Causal Research-flow layout

Research flow must rank nodes by causal dependency depth, not by node type. A Blocker that
gates an Experiment must appear in an earlier horizontal depth even though the stored edge
reads `Experiment blocked_by Blocker`.

Normalize stored relations to prerequisite-to-dependent directions for layout, for example:

- stored `Experiment blocked_by Blocker` -> causal `Blocker -> Experiment`;
- stored `Experiment governed_by Decision` -> causal `Decision -> Experiment`;
- `Experiment produces Evidence` -> causal `Experiment -> Evidence`; and
- `Evidence addresses Blocker` -> causal `Evidence -> Blocker`.

Node type affects styling, not rank. The existing SCC condensation and stable longest-path
code in `web/src/hooks/dagLayout.ts` is reusable algorithmic evidence, but the final design
must not leave causal lifecycle/layout truth independently owned by the browser.

`docs/acceptance/S87-experiment-prerequisite-chains.md` already defines the intended causal
program:

```text
real precursor gates
  -> precursor Experiment
  -> Evidence
  -> downstream Decision or Blocker
  -> main Experiment
```

The precise policy for genuine causal or epistemic cycles remains undecided.

## Minimal manager abstraction

The intended abstraction is small:

```text
GraphTransitionManager.prepare(pre_state, initiating_actions)
    -> expanded ordered actions
    -> transition events and rule receipts
    -> validated post_state
```

The same preparation path supports a non-writing preview. Human draft preview and agent
validation are clients of that path; only the locked commit path may publish its result.

`HistoryManager` should continue to own locking, append-only persistence, publication, and
mechanical replay. The graph manager owns transition semantics and closure. Run controllers
consume the committed result.

A symptom-only fix would be small but would preserve the split lifecycle model. The coherent
change is a medium-large cross-cutting refactor, not a rewrite. A preliminary estimate from
the current seams is roughly 1,200-2,000 production lines plus a comparable amount of tests
and specification work across approximately 15-25 files. Re-estimate after the acceptance
contract is final.

## Important current implementation seams

- `src/rcp/core/models.py`: Experiment and Blocker status vocabularies, Patch, GraphState,
  relation specifications.
- a new narrow core schema-compatibility boundary will own conservative legacy decoding;
  do not distribute compatibility branches through rules or consumers.
- `src/rcp/core/materialize.py`: atomic Patch application; node removal already removes
  incident edges.
- `src/rcp/core/validation/`: structural, authority, and surface-specific semantic checks.
- `src/rcp/history/manager.py`: canonical append lock, candidate validation, single append,
  human batch append, materialization, and publication.
- `src/rcp/control.py`: current graph-derived Experiment readiness and reasons.
- `src/rcp/watchers.py`: graph conditions currently evaluate final `GraphState` and treat a
  missing target as removed.
- `src/rcp/runs/work.py`: shared Work/Experiment-loop Patch apply seam.
- `src/rcp/runs/auto_research_effects.py` and
  `src/rcp/runs/auto_research_stream.py`: Auto-research validate/apply and final-settlement
  idempotency.
- `src/rcp/runs/experiment_loop.py`: watcher persistence, semantic endings, and episode
  binding.
- `src/rcp/service.py` and `src/rcp/api/app.py`: human Sync and project/API projection.
- `web/src/App.tsx`: current new-graph/old-control splicing hazard.
- `web/src/components/DetailDrawer.tsx` and `web/src/components/RelationMap.tsx`: existing
  gate text and embedded one-hop relation graph.
- `web/src/hooks/dagLayout.ts` and `web/src/hooks/useForceDag.ts`: fixed type lanes layered
  over existing topology/SCC code.

## Remaining design questions

Continue the grilling session one question at a time. Important unresolved branches include:

1. **Transition envelope.** Settle the exact initiating-action, generated-action, event,
   cause, rule-id, ordering, ruleset-tag, and final-revision schema.
2. **Legacy blocked phase.** Decide how the centralized no-write compatibility adapter maps
   historical `Experiment.status = "blocked"` into an intrinsic current phase without
   rewriting append-only history or guessing scientific state.
3. **Human preview conflicts.** Decide what the UI retains and presents when one rule-relevant
   staged edit produces a manager-preview conflict: the invalid input, the last valid draft
   projection, or both with explicit separation.
4. **Agent diagnostic closure.** Decide how many causally independent conflicts a best-effort
   preparation reports before returning correction input, while never constructing a
   partially committable result.
5. **Guidance validity schema.** Decide the exact per-field metadata and how retained stale
   `current_summary` and `next_action` appear in the active UI versus history.
6. **Same-transition refresh.** Decide how a Patch that changes an upstream dependency and
   deliberately refreshes downstream guidance in the same manager transition proves the
   new text is based on the final candidate rather than being invalidated again.
7. **Cross-store delivery.** Canonical graph history and local SQLite cannot share one ACID
   transaction, especially for a remote state repository. Specify the durable,
   idempotent reconciliation boundary for transition events, watcher completion, episode
   settlement, and crash recovery.
8. **Cycle policy.** Decide how causal ranks represent genuine SCCs/feedback cycles and how
   the UI makes them explicit.
9. **Projection contract.** Decide whether Sync and other mutation responses return a full
   project transition projection or a smaller revision-tagged manager result followed by
   one authoritative snapshot, while guaranteeing no mixed revision reaches the UI.
10. **Rule coverage.** Enumerate the first explicit relation/status rules beyond resolved
   Blocker removal. Do not invent scientific consequences inside implementation.

## Proposed acceptance promise to refine and confirm

Repository rules require a human-confirmed scenario before planning or code. The current
candidate promise is:

> Given an Experiment whose current graph gate is an open Blocker, every backend and UI
> surface shows the current derived gate and Research flow places the Blocker at an earlier
> causal depth. When an authorized human or agent changes that Blocker's status to
> `resolved`, one manager transition records the status event, automatically removes the
> Blocker and its incident relations, closes every deterministic downstream rule, retains
> but marks affected Experiment guidance stale, and commits one rule-valid revision. A
> watcher armed before the transition observes `resolved` exactly once even though the final
> graph contains no Blocker. The graph, Experiment-control projection, causal layout inputs,
> and mutation response identify the same revision, and no current surface reports that the
> Experiment is blocked by the removed node. Replay applies the recorded expanded actions in
> order without consulting historical rulesets. Auto-research and Experiment control consume
> the manager's committed result rather than raw agent or human intent.

Human staging in this promise is non-canonical: a local trigger-manifest check avoids
unnecessary calls, while a potentially rule-relevant edit receives a backend manager
preview before Sync. Older canonical history opens in a newer RCP without any migration
write; compatibility adapters present the current model in memory, and only a later ordinary
mutation may introduce the current schema generation.

The scenario should reference rather than duplicate:

- `docs/acceptance/S53-*` for Blocker attention;
- `docs/acceptance/S76-graph-condition-wake.md` for status-condition delivery; and
- `docs/acceptance/S87-experiment-prerequisite-chains.md` for the causal research program.

It should include both hermetic backend/API proof and a served-browser path because the
reported failure was user-visible and the Research-flow layout is frontend-visible.

## Likely implementation boundaries after scenario confirmation

Per `AGENTS.md`, land shared contracts serially before fan-out. Likely later seams are:

- graph core/history: manager, action/receipt contracts, closure, replay compatibility;
- run orchestration/watchers: manager preview/commit consumers and transition-event
  reconciliation;
- service/API/cache: one coherent post-transition projection;
- web: consume backend lifecycle truth, stale-guidance state, and causal ranks without
  independent lifecycle rules.

Do not start implementation from this handoff. First finish the design questions, confirm
the acceptance promise, write the scenario, and update the canonical blueprint in place with
an internal version/changelog bump.

## Verification expectations

When implementation is eventually authorized:

- focused tests for fixed-point rule closure, stable ordering, atomic failure, and no partial
  state leakage;
- trigger-manifest tests proving non-triggering human edits stay local, possible triggers use
  backend preview, and manifest mismatch fails safely to preview;
- human draft tests proving preview never commits and Sync remains one canonical revision;
- agent preparation tests proving contradictory bundled edits return attributable correction
  diagnostics without a partial append;
- replay tests proving expanded actions reproduce exact states without historical rules;
- prior-generation fixtures proving a newer RCP opens old history without writing or changing
  Patch bytes, plus a newer-generation guard that makes an older RCP read-only;
- one-Sync/one-revision tests;
- manager-preview parity tests for staged validators and locked commit;
- Blocker resolve/delete and downstream invalidation tests;
- transition-event watcher regression proving `resolved` fires exactly once before deletion;
- Auto-research Apply idempotency and final-settlement reuse tests;
- Experiment-loop tests proving ending decisions use the manager result;
- API tests proving graph/control/layout revision coherence;
- causal-layout tests including Blocker-before-Experiment and the chosen cycle policy;
- web tests proving stale blocked state and prose are not presented as current;
- full repository baseline and `uv run pre-commit run --all-files`; and
- served-app browser execution with console, network, and server-log inspection.

## Worktree caution

At this checkpoint the worktree already contains unrelated user/concurrent changes,
including acceptance/design documents and Web files, plus archive moves of older handoffs.
Preserve them. The only file created for this checkpoint is this active handoff.

## Suggested skills

- `grill-me` / `grilling` to finish the unresolved design tree one question at a time.
- `browser:control-in-app-browser` for the eventual mandatory served-app acceptance drive.
- `frontend-design:frontend-design` only after the causal ranking and stale-guidance contracts
  are settled, when reshaping Research flow and current/stale presentation.

## Immediate next step

Resume the grilling session with the highest-upstream unresolved question. The next question
should settle the conservative no-write adapter for a legacy current
`Experiment.status = "blocked"`: how to recover an intrinsic phase without guessing or
changing old Patch files. Give a recommendation, ask only that question, and do not start
implementation.
