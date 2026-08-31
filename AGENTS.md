# AGENTS.md

Canonical instructions for coding agents in this repository. `CLAUDE.md` imports
this file; do not duplicate it there.

## What RCP is

RCP is a local research control panel. It turns agent-assisted conversations and
bounded autonomous episodes into one durable research graph, a human authority
queue, operational task history, and a human-authored paper introduction.

Current authority, highest first:

1. [`docs/design.md`](docs/design.md) for product boundaries and cross-cutting invariants.
2. The applicable file in [`docs/specs/`](docs/specs/) for current behavior.
3. Active acceptance scenarios for selected observable promises.
4. Active decisions for rationale that remains easy to regress.
5. Active handoffs for human-confirmed work that is not yet complete.
6. [`docs/archive/`](docs/archive/) for history only.

Read [`docs/open-questions.md`](docs/open-questions.md) before deciding an issue it
covers. Report contradictions instead of silently choosing a source.

## Working loop

1. Read the relevant design, spec, source, and tests yourself.
2. Decide whether the change creates a new durable cross-module promise. Add and
   confirm an acceptance scenario only when it does; ordinary bugs and refactors
   use focused regression tests.
3. Plan file ownership, invariants, and checks before substantial edits.
4. Make small edits directly. Fan out larger implementation by coherent module
   boundary, while the main agent retains integration, verification, and review.
5. Run the focused checks, then the applicable baseline checks below.
6. Read the complete diff. A subagent's claim that tests pass is not evidence.
7. Update current behavior docs when semantics changed. Close or replace any
   handoff whose status changed in the same commit.

When another session changes the tree during long work, integrate through a real
three-way Git merge. Do not pipe a whole diff into `git apply`. The default local
workflow is direct work on `main`; use a dedicated branch and PR when the human
explicitly requests one.

## Commands and verification

Fresh-clone order matters because the Python wheel includes `web/dist`:

```bash
npm --prefix web ci
npm --prefix web run build
uv sync
```

Backend and documentation:

```bash
uv run pytest
uv run ruff check src tests
uv run pre-commit run --all-files
```

Web:

```bash
npm --prefix web run build
npm --prefix web test
```

Run the app:

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

Tests and builds are not enough for a user-visible feature, a reported product
failure, or a substantial route/background/view change. Serve the app, exercise
the affected path, inspect network and console errors, and check server logs. If
that cannot be done, state the exact verification gap.

Remote behavior requires a reachable host. Test against a copy of real app data
when migration or recovery correctness depends on records that fresh fixtures do
not contain. Never write to the human's real data directory from tests.

Before finishing, inspect pending and blocked acceptance scenarios for ones the
change made runnable or stale:

```bash
grep -l "^status: \(pending\|blocked-external\)" docs/acceptance/S*.md
```

## Stable invariants

These identifiers are cited from source and tests. Never renumber them.

1. **Canonical Patch logs are append-only.** Never edit or delete main or branch
   Patch history. Otherwise replay changes the past.
2. **Materialized graph files are outputs.** Never hand-edit `graph.json`,
   `research.md`, glossary, Proposal, coverage, or control projections.
6. **One canonical state repository.** Routes never write canonical files
   directly; `StateWorkspace` owns local/remote locking and publication.
6b. **One synchronous transition owns one semantic mutation.** Sync, Apply,
   branch Apply, and merge commit one expanded transition or nothing; replay does
   not rerun historical rules.
7. **Canonical and manifest writes are atomic.** Use the existing temporary-file
   and `os.replace` paths.
7b. **Materialization never mutates a shared contained model in place.** Replace
   container slots or whole attributes so a failed patch cannot corrupt the
   previous revision.
3. **Humans retain protected authority.** Only humans approve Proposals, change
   project truth membership, authorize episodes, or dispatch branch merges. The
   bounded branch orchestrator is the one explicit Decision exception.
3b. **Existing ResearchQuestions and Hypotheses are protected beliefs.** Agents
   may create new ones, but structural or semantic changes to existing ones use a
   Proposal. Never infer protected intent from operation shape.
4. **Agent capability is fixed in code.** Configuration cannot widen it. Discuss
   has no graph/project authority; Work uses exact provider-enforced write roots;
   ingestion writes only scratch; paper coach is read-only.
4b. **`patch.json` in the task stage is the only graph-change channel.** Never
   parse graph authority from answers, traces, artifacts, or repository edits.
5. **Context, graph target, and write scope are distinct.** Receiving a pointer
   or graph context grants no filesystem authority.
10b. **Only a captured Work turn has conversation graph authority.** Message
   wording or a stray file cannot upgrade Discuss.
10c. **Conversation scratch belongs to the stable chat, not one turn.** Clear the
   previous turn's patch on entry and fail closed if that cannot be proved.
10d. **Discuss and Work do not consume prior RCP chat transcripts.** A native
   provider session may continue, but stored chat history is never task authority.
8. **One RCP process owns one data directory.** OS advisory locks, not path
   existence, establish local and remote ownership.
9. **Failed runs retain scratch and patch text.** Delete a stage only after its
   graph patch applies successfully.
10. **Conversation and ingestion are different lifecycles.** They share launch
   plumbing only; chat never advances ingestion cursors or coverage.
10e. **Agent HTML previews cannot act on RCP.** Keep the opaque sandbox, bounded
   artifact discovery, no popups/forms/downloads, and no implied zero-network claim.
10f. **The ingestion watermark advances only after accepted Apply.** It is an
   overlap-tolerant timestamp, not an exactly-once cursor.
10g. **One episode, one graph target, one validated session/stage, one graceful
   Stop fence.** Never silently fall back to a fresh session or main graph.
11. **`answer` is the human reply; `message` is a trace.** Preserve the provider's
   final-assistant label.

## Structural rules

- Policy stays with its concrete owner. Do not add `kind`, `surface`,
  `patch_kind`, or equivalent selectors to shared execution plumbing merely to
  collapse visible policy.
- `BackgroundAgentTasks` is the common launch/runtime engine. Auto-research,
  Experiment recovery, watcher admission, and report owners intentionally call
  named engine internals, while the engine calls named owner functions. This is
  navigational modularity, not a plugin boundary; do not manufacture registries,
  facades, or event buses to hide the coupling.
- `api/app.py` is explicit composition plus run dispatch, startup recovery, and
  watcher runtime. Do not extract another control layer without measured owner
  collisions or a concrete testing problem.
- An orchestrator-triggered chat is specialized child Work only when its durable
  child-route row exists. Missing route identity intentionally follows ordinary
  Work for compatibility; do not convert this to a new failure without a product
  decision.
- Persisted task requests cross the compatibility decoder in
  `storage/request_compat.py` before callers see them. Its per-kind retirement
  allowlist is closed. Add a field only for a shipped, now-retired field whose
  removal preserves meaning; unknown fields must remain for strict rejection.
- Canonical Patch history carries the same duty with a worse failure: a retired
  field on a stored operation, or one the in-memory adapter adds while retiring a
  value, halts replay and leaves the graph read-only. Handle both in
  `adapt_persisted_patch_document` and in the replay branch of every field rule.
- Permission contracts are code, not manifest configuration. Every launch names
  its capability explicitly.
- Structured deliverables are file-backed. Conversational prose is the labelled
  provider answer; do not create a second answer file.
- There is no server uninstall, by design: install converges, so a bad install is
  corrected and rerun. Teardown is the sequence in the operations spec.
- Limits and timeouts live in `limits.py`, except schema constants that belong
  beside the model they constrain.
- Remote-executed code is shipped from its source module, never hand-copied into
  a command string.
- Prompt prose describing enforcement must render the same resolved object used
  by enforcement. Do not maintain parallel human-written allowlists.
- Graph branches are the narrow Auto-research graph exception, not Git branches,
  repository rollback, branch discard, or a conflict editor.
- One SQLite file is acceptable. Add compound transactions for proven harmful
  partial-write windows; do not split `AppStore` for aesthetic breadth alone.

## Documentation lifecycle

- Current behavior belongs in specs. Durable user journeys belong in acceptance.
  Rationale for an active easy-to-regress tradeoff belongs in decisions.
- A handoff is active work, not a diary. Its opening status must name what is
  implemented, what remains, and which decisions are settled.
- When a handoff decision changes, update its plan and status in the same commit.
  Rejected work is closed, not “not done.” Never leave mutually contradictory
  old and new plans active in one file.
- When work completes, is rejected, superseded, or abandoned, archive the handoff
  immediately. If later work materially changes scope, archive the predecessor
  and create a new handoff rather than appending a second plan.
- Archived material is evidence only and must never be cited as current authority.
- Delete stale instructions instead of adding caveats. A rule duplicated across
  AGENTS, specs, and handoffs will drift.

## Conventions and local facts

- Python uses `uv`, `pyproject.toml`, Pydantic, and `from __future__ import annotations`.
- Ruff settings live in `pyproject.toml`; do not assume them.
- The web layer consumes backend state; it never derives it. A derivation whose
  inputs are all backend state belongs to the projection, which exports the
  decision — `EpisodeResponse.health`, `recommendation`, `live`, `can_*`,
  `ExperimentControlState.graph_reasons`. Only derivations with a UI-specific
  input, such as the trust-view lens, stay client-side. `web/src/types.ts` is the
  one place a response shape is restated, and a lifecycle it fully exports is
  sealed there with an opaque type, as `EpisodeStatus` and `AgentTaskStatus` are,
  so branching on one cannot compile.
- `.research/`, `.recovery/`, and `web/dist/` remain outside formatting hooks.
- `pre-commit --all-files` sees tracked files only; account for every new path.
- Never trust a piped test command's exit status unless `pipefail` is set.
- Use shared test wait helpers rather than copied short polling loops.
- Literal expiry dates are test time bombs; derive them from the test clock.
- Going public is one bundled transition, not a visibility flip: it turns on
  branch protection and retires the private-source deploy key together. Read
  `decisions/2026-08-27-main-is-the-server-update-channel.md` before any of it.
- Two entrances are managed: the browser from `rcp serve`, and the source-built
  desktop app. Everyone builds from source, so the frozen release bundle is not
  a maintained target. Rebuild Tauri only when native files change.

## Maintaining this file

Keep this file near 200 lines: target 180–220 lines, with a hard ceiling of 230
lines enforced by tests. Add only cross-cutting rules a coding agent must see on
every task. Move behavior, rationale, long failure histories, UI details, and
module-specific procedures to their owning documents. When adding a line, remove
or consolidate something of equal value; never grow the file by append-only notes.
