# Graph transition manager implementation handoff

Date: 2026-08-17
Status: confirmed and ready to implement after typed graph operations

## Purpose

Replace the split graph-mutation lifecycle with one synchronous backend transition manager. Every human Sync, agent Apply, and Experiment-loop end-of-turn Patch is prepared to deterministic rule closure, validated, and committed as one coherent canonical revision or not committed at all.

This handoff supersedes `docs/handoffs/handoff-2026-08-17-graph-transition-manager.md`. Its unresolved questions are settled here. Do not resume the earlier design interview.

## Incident and required outcome

The motivating project reached a state where graph-derived Experiment control said an Experiment was ready, while its stored `status`, `current_summary`, and `next_action` still said it was blocked by a Blocker that no longer gated it. Human Sync could also return a new graph while the browser retained an old control projection.

The fix is not a one-off cleanup. The current graph, derived control, guidance validity, watcher events, and mutation response must all come from the same prepared transition and identify the same revision.

## Confirmed transition boundary

One backend transition manager is the only path from initiating graph intent to a canonical revision.

It is:

- synchronous;
- deterministic;
- stateless outside the candidate transition and canonical history;
- shared by human, ordinary-agent, orchestrator, Experiment-loop, branch, and merge graph writes; and
- owned by backend code.

It is not:

- an asynchronous repair worker;
- a second mutable graph store;
- a frontend lifecycle engine;
- an agent-authored rules system;
- a scientific inference engine; or
- a layout engine.

`HistoryManager` continues to own append locking, publication, and mechanical replay. The transition manager owns semantic preparation, generated effects, closure, and the coherent result envelope.

## Typed transition contract

Build the manager on the typed graph operations from the preceding handoff. Do not introduce a second untyped action representation.

A prepared/committed transition must contain:

- transition id;
- pre-state head/revision reference;
- ordered initiating actions with producer provenance;
- ordered generated actions;
- stable `rule_id` on every generated action;
- a cause reference from each generated action/event to an earlier action or event;
- ordered lifecycle events;
- ruleset tag;
- final graph state/head;
- final graph-derived control projection;
- guidance-validity projection;
- any other mutation response projections required by the current UI; and
- stable event ids for idempotent operational reconciliation.

Store each semantic operation payload once. Provenance and cause records reference it; do not duplicate whole graphs in the transition trace.

Replay applies the committed expanded operations in recorded order. It never reruns historical rules. Historical ruleset tags are provenance only.

## Rule registry and closure

Rules are strict, typed backend functions in one closed registry. Every rule has:

- stable id;
- explicit typed trigger contract;
- deterministic read set and generated actions/events;
- stable evaluation order; and
- no dependency on wall clock, randomness, UI state, provider output, or SQLite queries.

Preparation applies initiating operations in written order and evaluates triggered rules until closure. Add a deterministic cycle/non-termination guard and a bounded firing diagnostic. A cycle or contradictory generated effect rejects the transition; it never commits a partial candidate.

Do not build a user rule DSL or generic automation platform.

### Initial rule coverage

Keep the first registry narrow. Implement only lifecycle consequences already required by current product semantics:

1. open versus non-open Blockers determine graph-derived gating;
2. gating-affecting changes invalidate affected Experiment guidance;
3. current Experiment readiness/control is derived from the final graph, never copied from stored `blocked` state;
4. lifecycle status changes emit attributable transition events; and
5. the final mutation response is built from the same final candidate revision.

Do not invent scientific consequences, automatically change hypotheses, choose Decisions, create Evidence, or infer Experiment phases.

## Human staging and trigger manifest

A human staged edit remains non-canonical, but rule effects must be visible immediately.

The backend rule registry produces a compact conservative trigger manifest with a ruleset tag. The browser caches that backend-produced manifest only to decide whether a staged edit might require backend preparation:

- if the edit cannot trigger any current rule, apply it to the local draft immediately;
- if it may trigger a rule, immediately send the staged draft/edit to the backend preview path;
- show the complete preview candidate or the exact transition conflict;
- if the manifest is missing or its tag does not match the backend/project snapshot, fall back to backend preview; and
- on Sync, rerun the full staged batch against current canonical state under the append lock.

The client never computes rule outcomes. There is no independent client rule definition to drift. A backend ruleset update changes the manifest/tag returned by the backend and therefore changes preview behavior immediately after refresh; a stale tag fails safely to preview.

### Human preview conflict

When preview fails:

- retain the person's invalid staged input so it can be edited or reverted;
- retain the last valid draft projection separately;
- show the backend conflict attached to the initiating edit and generated rule/cause; and
- never display a partially prepared graph as though it were valid.

Sync commits the entire valid staged transition as one revision or commits nothing.

## Agent Apply and correction

For Auto-research Apply, ordinary Work Patch apply, branch apply, branch merge, and Experiment-loop end-of-turn apply:

- stage all typed initiating operations in written order;
- run the same manager preparation and closure used by human Sync;
- collect attributable diagnostics for every independent conflict up to the existing bounded diagnostic size;
- return operation index, rule id, cause chain, affected ids, and failed invariant to the same native agent session; and
- reuse the existing Patch-correction mechanism until the candidate is valid or the task ends.

A failed preparation writes no canonical revision and produces no operational side effect that claims the Patch was applied.

Do not advise agents to bundle causally unrelated edits merely to reduce revision count. One Apply remains one revision, but the Patch must be manager-valid as a whole.

## Blocker lifecycle

`Blocker.status = "resolved"` remains canonical graph state.

Resolving a Blocker:

- records the `open -> resolved` status change and transition event;
- makes it non-gating because only `open` Blockers gate;
- retains the Blocker node and its relations in canonical current state;
- invalidates affected current guidance where the gate changed; and
- does not automatically remove the Blocker or any incident edge.

Active Research-flow, attention, and gating UI omit resolved Blockers by default so they disappear from the current working view automatically. They remain available through history, direct detail, or an explicit resolved/history filter. Do not create a delete operation merely to simplify the UI.

Watchers waiting for `status == resolved` evaluate the committed final graph/event and fire once through the normal idempotent watcher path. No intermediate delete event is required.

## Experiment phase and derived blocking

`blocked` is not an intrinsic Experiment phase.

### Current writes

Remove `blocked` from current human and agent write schemas. An Experiment's stored lifecycle field continues to represent intrinsic work phase only, such as proposed, designing, implementing, debugging, running, analyzing, completed, abandoned, or superseded.

Every current surface obtains blocked/ready/gate reasons from the graph-derived Experiment-control projection. Do not synthesize or persist `status = blocked` when an open gate appears.

### Legacy compatibility

Older valid history may contain `Experiment.status = "blocked"`. Opening it in a newer RCP is a canonical no-op.

At the centralized compatibility-decoding boundary, map that legacy value in memory to a non-assertive intrinsic value such as `unspecified`. The compatibility value is displayable as “Phase not recorded,” is not writable by agents as a normal phase, and does not imply blocked or ready. Present gating only from the current graph.

Do not guess `implementing`, `running`, or another scientific phase, and do not append a migration Patch. The first later explicit phase edit may replace `unspecified` through an ordinary transition.

## Guidance validity

Keep `current_summary` and `next_action` as authored text. The manager never rewrites their scientific content.

Add backend-owned per-field validity metadata sufficient to distinguish retained historical text from current guidance. A gating-affecting transition marks both fields stale for every affected Experiment unless that field was already empty.

A field becomes current again only when a later explicit human or authorized agent operation updates that field after the invalidating transition. Keep this conservative rule: a transition that changes gating and also writes guidance still leaves the guidance stale. A later transition is required to affirm that the text was written against the final gate state. Do not attempt to infer semantic freshness from operation order.

The active UI must not present stale text as the current summary or next action. It may retain the text in history/detail with an explicit stale state. Empty current guidance is preferable to obsolete instructions presented as current.

The compatibility adapter marks blocked-era guidance stale in memory without writing history.

Guidance-invalidating changes include at least:

- `blocked_by` edge creation/removal;
- a linked Blocker entering or leaving `open`;
- governing Decision status/choice changes that alter readiness;
- pending Proposal changes used by Experiment control;
- other relation changes already consumed by `src/rcp/control.py`; and
- upstream Evidence-to-Hypothesis relation or assessment changes for Experiments whose causal program depends on that Hypothesis.

Use the existing control dependency logic for gating and the existing graph relations for causal guidance invalidation. Do not maintain a second independently authored dependency graph.

## One coherent mutation projection

Every successful mutation returns one revision-tagged project transition projection built from the final prepared/committed state. At minimum it contains the graph and Experiment-control map from the same revision. Any attention, run-control, or causal-layout input included in that response must carry the same head reference.

The browser replaces the prior project snapshot atomically. It must never splice a new graph into an old `experiment_control` projection while waiting for a later reload.

Preview responses are explicitly non-canonical and carry their base head plus ruleset tag. They cannot be mistaken for a committed project revision.

Causal layout remains a derived projection. It may consume backend-normalized causal dependencies or ranks from the same revision, but it does not participate in transition closure or generate graph actions. Existing SCC handling should rank a feedback component together rather than changing graph truth to break cycles.

## Cross-store operational reconciliation

Canonical graph history is authoritative; SQLite episode/task/watcher state is an operational projection. They cannot share one ACID transaction, especially with a remote state repository.

Every committed transition event receives a stable id in canonical transition provenance. Operational consumers apply events idempotently and record their consumption by event id/head. On restart or relevant poll, RCP scans committed transitions after the consumer watermark and reconciles any event that was committed but not yet reflected in SQLite.

This applies to watcher completion/wake, episode settlement, task receipts, and branch merge receipts where relevant.

A crash after canonical append but before SQLite update must converge without repeating the canonical transition or delivering one event twice. A SQLite update must never precede and falsely claim a canonical append that did not occur.

## Schema compatibility

Use one centralized no-write compatibility boundary for older Patch, operation, Experiment, and transition shapes. Current core, manager, control, and UI code receive the current typed model.

- Old Patch bytes remain unchanged.
- Opening an old project appends no migration revision.
- A newer schema generation may be stamped only by a later ordinary mutation.
- An older RCP encountering an unsupported newer generation becomes read-only and asks for an update.
- Manager ruleset tags and persisted schema generations remain separate concepts.

## Non-goals

Do not:

- auto-delete resolved Blockers;
- generate scientific prose or judgment;
- make layout part of mutation semantics;
- implement a frontend rule engine;
- add asynchronous repair revisions;
- rerun rules during replay;
- commit intermediate closure states;
- guess legacy Experiment phase; or
- broaden the first rule set beyond existing lifecycle/control truth.

## Important implementation seams

Shared contracts must land serially. Expected seams include:

- typed operations and `src/rcp/core/models.py`;
- a new transition-manager core module and rule registry;
- centralized compatibility decoding;
- `src/rcp/core/materialize.py` and `src/rcp/core/validation/`;
- `src/rcp/history/manager.py` and revision/transition persistence;
- `src/rcp/control.py`;
- `src/rcp/watchers.py` and storage reconciliation;
- `src/rcp/runs/work.py`;
- `src/rcp/runs/auto_research_effects.py` and `auto_research_stream.py`;
- `src/rcp/runs/experiment_loop.py`;
- `src/rcp/service.py` and API mutation/projection paths;
- `web/src/App.tsx` snapshot replacement;
- `web/src/components/DetailDrawer.tsx` and `RelationMap.tsx`;
- `web/src/hooks/dagLayout.ts` and `useForceDag.ts` only for same-revision derived layout; and
- focused backend/web tests.

## Acceptance documentation

Do not create a separate prose scenario for every transition rule. Update the active scenarios that survive the documentation cleanup:

- the Blocker attention/current-truth scenario for resolved-but-retained behavior and stale guidance;
- the graph-condition watcher scenario for one final-state resolution wake;
- the live-canonical-state/revision-coherence scenario for atomic graph/control replacement; and
- the prerequisite-chain scenario only where causal projection wording changes.

Create a new active transition scenario only if those existing cross-module contracts cannot express the complete user journey without becoming contradictory. Unit and regression details belong in tests.

The durable promise is:

> One human Sync or agent Apply becomes one manager-valid revision or nothing. Resolving a Blocker retains it as resolved, removes its gate from current control, hides it from the active working view, marks affected guidance stale, and returns graph and control from the same revision. Human staging previews backend rule effects before Sync; an invalid agent Patch receives attributable correction input in the same session. Replay applies recorded expanded actions without historical rules.

## Verification

Required proof includes:

1. Deterministic rule ordering, closure, firing guard, and atomic conflict failure.
2. One human Sync produces one revision even with several staged initiating actions.
3. Human preview never appends history and locked Sync revalidates against current main.
4. Backend-generated manifest false positives use preview; missing/mismatched tags fail to preview; the client never computes outcomes.
5. Agent Apply conflict diagnostics identify initiating operation, rule, cause, and invariant, with no partial append.
6. `open -> resolved` retains the Blocker/edges, removes gating, hides it from active views, and wakes a resolution watcher once.
7. Current writes cannot persist Experiment `blocked`; legacy `blocked` opens as unspecified phase with derived gate truth and no canonical write.
8. Gating changes mark both guidance fields stale; a later explicit field update clears only that field; same-transition guidance remains stale.
9. Mutation responses and browser state contain one coherent graph/control/head and never mix revisions.
10. Replay reproduces exact states from expanded actions without loading historical rule code.
11. Prior-generation fixtures open byte-exact and unsupported future generations fail read-only.
12. Crash tests reconcile committed transition events into SQLite exactly once.
13. Auto-research, Experiment-loop, branch, and merge apply paths all use the manager.
14. Served-browser verification confirms resolved Blockers disappear from the active flow without being deleted and stale guidance is not shown as current.
15. Full backend, web, pre-commit, and served-app checks pass.

## Worktree and completion

The current worktree contains unrelated human/concurrent changes, including Web and documentation files. Preserve them; do not reset, clean, or overwrite their hunks.

After implementation, replace the old design-checkpoint handoff with this implemented contract in current specifications and archive both handoffs during the final documentation pass.
