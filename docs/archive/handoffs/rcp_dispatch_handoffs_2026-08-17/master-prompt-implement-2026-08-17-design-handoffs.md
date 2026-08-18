# Master implementation prompt: 2026-08-17 confirmed RCP design handoffs

Implement the confirmed handoffs below in the current RCP repository. They are the human-approved design authority for this task; do not reopen their settled product decisions or ask for another acceptance confirmation.

Required handoffs, in dependency order:

1. `docs/handoffs/handoff-2026-08-17-typed-graph-operations.md`
2. `docs/handoffs/handoff-2026-08-17-evidence-assessments.md`
3. `docs/handoffs/handoff-2026-08-17-graph-transition-manager-implementation.md`
4. `docs/handoffs/handoff-2026-08-17-project-write-containment.md`
5. `docs/handoffs/handoff-2026-08-17-auto-research-graph-branches.md`
6. `docs/handoffs/handoff-2026-08-17-documentation-model-and-archive.md`

If these files were supplied outside the repository, copy them verbatim into `docs/handoffs/` first. The older `handoff-2026-08-17-graph-transition-manager.md` is a superseded design checkpoint, not current implementation authority.

## Operating constraints

The current worktree contains unrelated human and concurrent changes, including modified acceptance/design files, Web files, and handoff archive moves. Preserve all of them.

Before editing:

- read `AGENTS.md`, the six handoffs, the current blueprint/design modules, and the relevant existing acceptance scenarios;
- capture `git status --short`, `git diff --stat`, and the list of pre-existing dirty paths;
- do not reset, clean, stash, checkout, or overwrite the worktree;
- do not revert a change merely because it is outside these handoffs; and
- when a required file already has uncommitted edits, preserve and integrate those hunks deliberately.

The handoffs are intentionally scoped. Do not add hostile-user isolation, general graph branching, repository branches, a conflict viewer, a rule DSL, resource supervision, or a documentation application.

Do not stop after planning. Implement, test, inspect the served product where required, repair defects found, and complete the documentation/archive pass in this run.

## Collision-control protocol

Use one integration owner for all shared contracts and aggregator files. Parallel work is allowed only after the relevant contract has landed and only with explicit non-overlapping file ownership.

If using subagents, create isolated filesystem clones from the exact current tree, including its uncommitted state. Do not create clean worktrees from `HEAD` that omit the human's current changes. Each subagent returns a focused commit or patch limited to its assigned paths; the integration owner reviews and applies it.

The following files are integration-owner-only unless a phase explicitly transfers ownership:

- `src/rcp/core/models.py`;
- the new core operations and transition contract modules;
- `src/rcp/history/manager.py`;
- `src/rcp/storage/models.py` and database migration registration;
- `src/rcp/providers.py` and `src/rcp/agents/launcher.py`;
- `src/rcp/background.py`;
- `src/rcp/api/app.py`;
- `web/src/types.ts`;
- `web/src/App.tsx`;
- `web/src/styles.css`;
- `AGENTS.md`; and
- all central current documentation during the final pass.

No two workers may edit the same production file. When a fan-out requires one shared import/route/type aggregator, workers add new isolated modules and the integration owner wires them in serially.

Keep commits/change sets phase-scoped. Never mix documentation archive moves into core implementation commits; archive only after behavior is verified.

## Phase 1: typed graph operations

Implement `handoff-2026-08-17-typed-graph-operations.md` serially.

Required sequence:

1. Inventory every operation currently accepted by materialization, Proposal handling, humans, agents, and system/identity paths.
2. Land the strict core discriminated union without changing persisted JSON shape.
3. Refactor materialization, validation, authority, Proposal bookkeeping, agent schemas, history summaries, and test helpers to consume it.
4. Add prior-generation/round-trip/malformed-operation proof.
5. Run all focused core, validation, authority, history, Proposal, prompt/schema, and replay tests.

Do not begin downstream graph-schema work while any core consumer still relies on ad hoc operation dictionaries except at a narrow serialization/compatibility edge.

## Phase 1B: claim-relative Evidence assessments

Implement `handoff-2026-08-17-evidence-assessments.md` serially on the typed operation contract.

Required sequence:

1. Move current evidential weight off the Evidence node and onto typed Evidence-to-Hypothesis edge assessments.
2. Keep relation names as direction; add only relevance, weight, scope, and qualifications to applicable edges.
3. Preserve historical `Evidence.strength` as clearly labelled compatibility metadata without silently mapping it to current claim-relative weights.
4. Keep historical unassessed edges readable; require an assessment only for new applicable edges.
5. Refactor validation, authority/Proposal handling, research rendering, API, agent schema/prompt, and focused Web detail surfaces.
6. Update the retained grounded-belief/ontology acceptance promise only if it survives the later archive classification; otherwise rely on the current specification and tests.
7. Run all focused schema, compatibility, authority, rendering, API, and Web tests.

Do not begin transition-manager implementation until the final Evidence and edge schema is stable, because transition invalidation rules must consume it.

## Phase 2: graph transition manager

Implement `handoff-2026-08-17-graph-transition-manager-implementation.md` on the typed operation contract.

### Serial contract slice

The integration owner first lands:

- transition request/prepared/committed schemas;
- rule registry and closure/error contracts;
- compatibility representation for legacy Experiment `blocked`;
- guidance-validity metadata;
- canonical transition provenance/event ids;
- coherent project mutation projection; and
- the HistoryManager preparation/append boundary.

Do not fan out until these types and APIs are stable and focused contract tests pass.

### Non-overlapping fan-out after the contract

A safe split is:

- **Core/control worker:** transition rules, compatibility adapters, materialization/validation consumers, derived Experiment control. It must not edit HistoryManager after the contract slice.
- **Run/reconciliation worker:** watchers, SQLite event reconciliation, Work/Auto-research/Experiment-loop manager consumers. It adds focused modules where possible and leaves `background.py`/API wiring to the integration owner.
- **Web worker:** draft preview consumption, atomic project-snapshot replacement, resolved-Blocker filtering, stale-guidance display, and same-revision causal projection. It must not edit `App.tsx`, `types.ts`, or `styles.css`; return isolated component/hook changes and an exact integration note.
- **Test worker:** hermetic regression/compatibility tests in test-only files that no other worker owns.

The integration owner performs all shared wiring, resolves existing dirty Web hunks, and verifies the complete flow.

Update existing active acceptance contracts rather than creating one scenario per rule. Do not auto-delete resolved Blockers. Confirm in code and tests that human staging performs immediate backend preview when the backend-produced manifest says a rule may fire, and that an invalid agent Patch returns attributable correction input to the same session.

Pass focused tests before proceeding.

## Phase 3: provider-native project write containment

Implement `handoff-2026-08-17-project-write-containment.md` serially because it touches shared provider launch and Work/Auto-research call sites.

Required sequence:

1. Land one provider-neutral, project-bound write-scope resolver and durable fingerprint.
2. Route every fresh/resumed/retry/correction Work-like launch through it.
3. Replace Codex dangerous bypass with native workspace-write plus exact admitted roots.
4. Replace Claude bypassPermissions with the supported native unattended exact write allow-list.
5. Fail explicitly if the installed supported provider cannot enforce the declared roots.
6. Record truthful launch receipts and enforce resume binding.
7. Preserve public-network behavior and all narrower capability contracts.
8. Test local and remote command construction, cross-project exclusion, resume mismatch, and every Auto-research/Experiment continuation path.
9. Perform authenticated live provider probes where credentials are available.

Do not add Bubblewrap, Landlock, containers, new OS accounts, read secrecy, or process supervision.

## Phase 4: Auto-research graph branches and merge

Implement `handoff-2026-08-17-auto-research-graph-branches.md` on the completed transition manager.

### Serial branch contract slice

The integration owner first defines and lands:

- branch identity/base/head references;
- canonical branch metadata and append-only merge receipt schemas;
- branch-aware history/materialization interfaces;
- Patch merge provenance;
- episode-to-branch storage/API projection fields;
- merge eligibility and idempotency contracts; and
- Web/API public types.

Do not let separate workers invent these independently.

### Non-overlapping fan-out after the contract

Use a split such as:

- **History/transport worker:** branch replay, append, materialization, remote-state publication/recovery. It does not edit shared core models or HistoryManager signatures after the contract slice.
- **Episode/orchestration worker:** branch binding at episode creation; root, continuation, child Work, child Experiment, watcher, correction, settlement, and report routing. It owns the Auto-research run/storage modules assigned to it and does not edit API/Web files.
- **Merge worker:** a new graph-only merge task/prompt, semantic base-branch-main input preparation, transition-manager validation/correction, moving-main retry, and idempotent merge receipt reconciliation. Shared background/API registration remains integration-owner work.
- **Web worker:** minimal branch summary and Merge-to-main action in the Auto-research Runs detail, plus focused tests. It does not build a branch viewer, conflict viewer, discard action, or repository controls and does not edit shared `types.ts`, `App.tsx`, or `styles.css` directly.
- **Acceptance/test worker:** one cross-module branch/merge acceptance scenario and disjoint backend/API/Web test files.

The integration owner wires shared modules, verifies exact authority, and runs conflict/crash/remote tests.

The merge agent is graph-only, human-dispatched, uses orchestrator graph authority, and does not spend the concluded research episode's invocation budget. Main remains editable while the branch runs. A successful merge appends one attributable main transition; the branch persists and is never discarded.

Pass focused branch, history, episode, watcher, merge, API, Web, and remote-state tests before documentation work.

## Phase 5: full integration verification

Before restructuring documentation:

1. Review the entire production diff against all four behavior handoffs.
2. Search for bypasses and old contracts, including raw `Patch.ops` dictionaries, `Experiment.status = "blocked"` current writes, resolved-Blocker auto-deletion, mixed graph/control mutation responses, Codex dangerous bypass, Claude bypassPermissions, and Auto-research graph appends to main.
3. Run focused suites for every phase.
4. Run the complete backend test suite.
5. Run Ruff and the repository pre-commit suite.
6. Run Web typecheck, unit tests, and production build.
7. Run served-browser drives for transition preview/current-state behavior and Auto-research branch merge, inspecting browser console, network requests, and backend logs.
8. Run remote-state/provider checks where the environment makes them possible; report a skipped live dependency truthfully rather than claiming it passed.
9. Repair all defects attributable to this work before proceeding.

Do not treat green unit tests as sufficient for the user-visible graph and Runs changes.

## Phase 6: documentation model and archive cleanup

Implement `handoff-2026-08-17-documentation-model-and-archive.md` last, with one documentation owner to avoid collisions.

Required sequence:

1. Inventory current blueprint/design modules, decisions, handoffs, acceptance scenarios, links, and pre-existing dirty documentation changes.
2. Create concise `docs/design.md` and complete current `docs/specs/` that describe the now-implemented system.
3. Correct the orchestrator Decision authority contradiction and document graph-only branches, agent-native merge, typed operations, transition semantics, and cooperative provider-native write containment.
4. Update `AGENTS.md` only where required for the new documentation and acceptance workflow.
5. Classify acceptance scenarios. Move implemented minor/regression/unit/single-module/redundant scenarios intact to `docs/archive/acceptance/`; retain pending and important cross-module journeys active. Do not fold archived scenario text into specs.
6. Move implemented/superseded handoffs and absorbed decisions/design files to their archive directories with `git mv`.
7. Archive the old blueprint after its current content has been represented; eliminate duplicate live authority.
8. Generate or update the compact active acceptance index and link/uniqueness checks.
9. Verify all current links and run the relevant documentation/pre-commit checks.

At the end, `docs/handoffs/` must contain only genuinely unimplemented confirmed work. Archive all six handoffs from this dispatch, including this master task's superseded transition checkpoint.

## Acceptance discipline for this dispatch

Do not restore the old “one scenario per bug” behavior during implementation.

- Typed operations: tests, no standalone new scenario.
- Evidence assessments: update a retained ontology/grounded-belief contract only if needed; no automatic new scenario.
- Transition manager: update the retained cross-module Blocker/watcher/revision scenarios; add a new one only if the retained contracts cannot state the durable journey coherently.
- Project write containment: update the retained boundary/remote-run contract; no automatic new scenario.
- Auto-research graph branch and merge: create one active cross-module scenario.
- Documentation cleanup: documentation checks, no product scenario.

The handoffs are already confirmed. Do not pause for scenario approval.

## Final review and report

Before declaring completion:

- compare final behavior against every required statement and non-goal in all six handoffs;
- inspect `git diff` for lost pre-existing hunks or unrelated rewrites;
- ensure no task-generated temporary files, credentials, provider caches, or personal project data enter the repository;
- ensure archived documents are not cited as current authority; and
- ensure every reported test/live-drive result is exact.

Return a concise implementation report containing:

- phase-by-phase change summary;
- commits or isolated change sets used;
- important schema/compatibility choices;
- tests, builds, browser, remote, and live-provider checks actually run;
- any checks skipped because an external dependency was unavailable;
- retained risks or follow-up defects that are genuinely outside these handoffs; and
- confirmation that pre-existing worktree changes were preserved.
