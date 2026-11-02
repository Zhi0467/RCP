# Dev-flow hardening (temporary design)

This file is deleted before merge. Its rules move to AGENTS.md (Verification),
`docs/install.md` (commands), and the docstrings of the new checks.

Goal: make each recurring failure a mechanical check. Every check runs in the
existing CI jobs (`lint`, `python-shard`, `web`), on PRs and on main alike.
Checks count work or reachable states. The only time values are per-test
timeouts that keep a hung check from stalling a shard.

Decided with the human on 2026-11-02: one combined PR; the explorer over real
code (not TLA+ or Hypothesis); per-file comment counts; tests under the ban;
the shipped set derived from its readers; small violations fixed here and the
rest on a shrinking allowlist; HTTP-only member journeys; one pre-commit
ratchet script for per-file rules; and real-data regressions feeding
pre-promote CI.

Each piece extends an existing mechanism.

| Piece | Extends | Runs in |
|---|---|---|
| 1. Handoff lifecycle explorer | `background_tasks.stream` test seam, store fixtures | `python-shard` |
| 2. Lint rules | `test_module_boundaries.py`, `test_route_inventory.py`, ruff | `lint`, `python-shard` |
| 3. Comment ban + ratchet | basedpyright baseline, `test_project_types.py` ratchet, ESLint suppressions | `lint` |
| 4. Member journeys | `AcceptanceAgentLauncher`, `test_agent_slice_served.py` | `python-shard` |
| 5. Budgets | `graphProjectionPerformance.test.mjs`, `_TracingAppStore`, `real_use_corpus.py` | `web`, `python-shard` |

Real-data regressions are a working rule, not a check; the rule lives in
AGENTS.md (piece 3's diff).

## 1. Handoff lifecycle explorer

**One bounded exhaustive search over the real store and engine, for the
handoff and transport-retry machine only.** It is the one machine with a
history of bugs: #368 alone fixed a closed-thread restart, a retry race,
`provider_auth` settlement, and handoffs that never settled while a retry was
owed. Other machines get a model when a bug shows up in them.

### Alternatives

| Option | Checks real code? | Exhaustive? | Why not |
|---|---|---|---|
| TLA+/TLC | No; a parallel model drifts from the SQL | Yes | Needs Java and `setup-java`; a new language |
| Hypothesis `RuleBasedStateMachine` | Yes | No | New dependency; random, not exhaustive |
| **Explorer over the real store** | Yes | Within bounds | Chosen |

### What it checks

#368 put the "is a retry still owed" decision in one predicate,
`BackgroundAgentTasks.transport_retry_owed`, which both
`_rearm_owed_transport_retries` and `_handoff_turn_settled` call. The
explorer checks what that does not settle:

- **Agreement**: `owed_transport_retry_operation_ids` (the SQL superset in
  `storage/agent_tasks.py`) contains every operation for which
  `transport_retry_owed` is true, and a handoff reads as settled exactly when
  no retry timer for its delivered turn is pending in the world.
- **Safety** in every reachable state: at most one delivered turn and one
  Space wake per handoff; no wake while the Space thread is busy.
- **Liveness**: from every reachable state, environment actions alone
  (settle, timer fire, dispatch pass, restart) reach a state where the handoff
  is settled or a human control is shown. The open question this answers: what
  re-runs delivery when `can_retry` turns false without a settle event (an
  attachment batch expires)?

### Mechanism

- `tests/lifecycle_explorer.py`:
  - **Replay, not snapshot.** A state is the action sequence that reached it.
    Each step builds a fresh `World` (a real `AppStore` on a temp SQLite file
    plus a real `BackgroundAgentTasks`) and replays the sequence. In-memory
    engine state (`_workers`, `_retry_decided`, `_controls`) is therefore
    always real, never restored. Depth is small, so replay cost is fine.
  - `Action(name, enabled(view), apply(world))`. `view(world)` is a hashable
    tuple read from rows: the handoff row's state, each turn's `status`,
    `failure_kind` and receipts, the Space thread's busy flag, and the pending
    timer list. Two sequences with equal views are one state.
  - BFS over views to depth 8. A failure prints the shortest action sequence.
- Seams, all in the test:
  - `_schedule_transport_retry` appends to `world.timers` instead of starting a
    `threading.Timer`; the "timer fires" action pops one and calls the
    callback.
  - `store.now` is patched on the instance to a fixed clock that actions
    advance.
  - Turn outcomes come through `background_tasks.stream`, and the action waits
    for `_task_settled` on that one task with the shared wait helper before
    reading the view, so each action is atomic.
- Actions: the source turn settles; the delivered turn settles as succeeded,
  `transport_lost`, `provider_auth`, other failure, or Stopped; a timer fires
  (admitted or refused); restart (a fresh engine over the same file, running
  startup reconciliation); sign-in release; human Retry or Resume; the
  attachment batch expires; a dispatch pass.
- **Limit**: actions run one at a time, so check-then-admit races inside one
  action (for example around the retry admission in `background.py`) are out
  of reach. Races stay with targeted thread tests.
- Budget: the test's timeout is 60 s; the test records the state count so a
  growing model is visible.

Files: `tests/lifecycle_explorer.py`, `tests/test_lifecycle_handoff.py`.
Product code changes only for defects the model finds, each with its trace as
a regression case.

## 2. Lint rules

### (a) Host-shipped Python

About 50 files are shipped as source to hosts whose `python3` may be 3.9.

- **Syntax**: `[tool.ruff.per-file-target-version]` in pyproject maps each
  shipped glob to `"py39"`. Ruff 0.16 then reports 3.10+ syntax there, and
  `UP` stops proposing 3.10+ rewrites, which retires the `# noqa: UP041`
  pragmas in the two staged-command files once both are in the set. Today it
  flags one case: the parenthesized `with` at
  `transport/remote_turn_supervisor.py:249`; fix it.
- **Imports**: `tests/test_shipped_sources.py` checks each shipped file: top-
  level imports are in `sys.stdlib_module_names` minus modules newer than 3.9
  (for example `tomllib`), and there is no unguarded top-level `rcp` import
  (imports under `if`, `TYPE_CHECKING` or inside functions stay allowed).
- **The set**, derived from the six readers. Only whole files count:
  - `remote_bundle`: call the real module-list resolution
    (`_REMOTE_BASE_MODULES` plus each provider class's `__module__`) and map
    modules to files.
  - `importlib.resources` and `Path(__file__)` reads: AST scan for calls whose
    target is a string literal ending in `.py`.
  - `isolated_python_argv(x)`: resolve `x` to the attribute's literal
    assignment in the same class; a target the scan cannot resolve fails the
    test, so a new caller is never missed silently.
  - `_remote_script(name)` and `inspect.getsource`: excluded with a stated
    reason. They ship extracted functions, not the file, and the file itself
    imports `rcp` (`transport/state.py`).
  - Self-tests cover each resolver on a small sample. The test also asserts
    that the ruff glob list equals the derived set.
- Out of scope: the inline `script = """…"""` strings in `run_stage.py`,
  `state.py` and `sources/preflight.py`.

### (b) Polled routes stay cheap

The web polls about 14 routes (`RefreshScheduler`, `useAgentThreads`, the
`AgentThreadView` live poll, and others).

- `tests/test_polled_routes.py` with `tests/fixtures/polled_routes.json`, which
  lists each polled `(method, path)` with its web call site. The test asserts
  each is in `_FROZEN_ROUTE_INVENTORY`.
- It calls each route on a seeded app with one tracer, `trace_requests(app)`
  in `tests/helpers.py`, built from `_TracingAppStore` (`test_storage.py`): a
  SQLite trace callback on the app's own store connection factory records
  statement count, `BEGIN IMMEDIATE` and DML; a patched `subprocess` records
  `git`, `ssh` and `rsync`; `refresh_if_stale` and `open_display` are recorded
  as remote reads. Piece 5 reuses the same tracer.
- Today's violators go into a shrinking allowlist in the same JSON, each with a
  one-line reason: team device pairing GET (`BEGIN IMMEDIATE`),
  `GET /api/space/machines/{id}/browser` (SSH), `GET /api/space/runs`
  (conditional write plus remote refresh), `GET /api/agent-threads`
  (`ensure_agent_threads` writes), `GET /api/update-notice` (HTTP plus
  `git rev-parse` when stale), the project chat transcript (`open_display`).
- The browser fetch-budget test (piece 5) reads the same JSON: a URL a screen
  fetches more than once in its window must be listed.

### (c) Idempotency-Key admission

- **Rule**, in `test_route_inventory.py`: every `(method, path)` the web sends
  an `Idempotency-Key` to declares the header, and its endpoint calls
  `client_request_admission` directly or through a named helper
  (`_space_control_admission`). The web side is derived by scanning `web/src`
  for the header name and resolving each call's path template; the route side
  is an AST check of the endpoint body.
- **Today**: only project pause (`api/tasks.py`, the pause handler) and Space
  thread pause (`api/agent_threads.py`) ignore the key the web sends through
  `controlThreadTask`. Pause stores no operation or episode, and pausing twice
  is already a no-op, so the fix is on the web: `controlThreadTask` stops
  sending the key for `pause`.

Shared AST helpers move, unchanged, from `test_module_boundaries.py`
(`_python_files`, `_call_name`) into `tests/architecture.py`, and both old and
new tests import them from there.

## 3. Comment ban with allowlist and ratchet

**Rule.** No comments in code, except:
- **Invariant citations**: the comment line matches `(?i)invariant \d+[a-z]?`.
- **Lint pragmas**: `noqa`, `type: ignore`, `pyright:`, `basedpyright:`, `fmt:`,
  `eslint-disable*`, `@ts-*`, `prettier-ignore`.
- **Docstrings and JSDoc** stay allowed everywhere. AGENTS.md puts module-local
  promises in their owner's docstring, and many owners are private, so the ban
  covers only `#`, `//` and non-JSDoc `/* */` comments.

**Counting unit**: one per comment line; a multi-line block counts each line.
Shebangs and encoding lines are not comments.

**Current counts.** About 7,500 inline comment lines in Python (`src/rcp`
4,888, `tests` 2,512, `supervisor`/`packaging`/`scripts` 146) and about 980 in
the web (`web/src` 542, `web/tests` 438). Only 6 Python comments cite an
invariant today. Before the baseline is written, add the number to each
invariant owner that AGENTS.md names whose promise sits in an uncited comment
(for example the 10f watermark comment in `materialize._apply_patch`), so a
later sweep cannot delete it.

**Mechanism.**
- **Python**: `scripts/comment_ratchet.py`, a pre-commit hook with
  `pass_filenames: false`, like basedpyright. It uses `tokenize` over every
  tracked `.py` file. Its baseline `.ratchet/baseline.json` is
  `{file: {rule: count}}`, sorted, one file per line, so concurrent PRs merge
  by line. A run fails when a count rises; it rewrites the file when a count
  falls and drops deleted files. CI's `pre-commit run --all-files
  --show-diff-on-failure` fails when the shrunk baseline is not committed.
  This is the only ratchet script; any later per-file Python rule joins it.
- **TypeScript**: a local ESLint rule `rcp/no-comments` in `eslint.config.js`
  using `sourceCode.getAllComments()`. Its baseline is ESLint 10's bulk
  suppressions file `web/eslint-suppressions.json` (`--suppress-all`),
  counted per file and rule; unpruned suppressions fail by default.
  - Add a config block for `tests/**/*.mjs` that enables the rule.
  - `lint` runs `eslint src tests`.
  - The pre-commit ESLint hook filter widens to `^web/(src|tests)/` and the
    suppressions file; the prettier hook excludes the suppressions file.
- **Order**: both baselines are generated in the last code step, after every
  other step has changed source.

**Alternatives.** Ruff has no "no comments" rule (`ERA001` only finds
commented-out code), and ESLint's `no-warning-comments` matches keywords, not
all comments. The Python side needs a small counter; the TS side reuses
ESLint's own suppressions.

**AGENTS.md diff** (223 lines today, 228 after; the ceiling is 230):

```diff
@@ Verification
-- Pre-commit runs `basedpyright` against `.basedpyright/baseline.json`. Fix new
-  errors. Never use `--writebaseline`. The baseline only shrinks.
+- Pre-commit runs `basedpyright` against `.basedpyright/baseline.json` and the
+  comment ratchet against `.ratchet/baseline.json` and
+  `web/eslint-suppressions.json`. Fix new errors. Commit shrunk baselines;
+  never regrow one or use `--writebaseline`.
+- Write no comments: name it, test it, or put the why in the owning spec.
+  Invariant citations, lint pragmas, and docstrings stay allowed.
+- A bug that a live drive or team rehearsal finds also adds its data shape to
+  the installed-upgrade or release-upgrade seed in the same PR.
```

The last bullet is the real-data rule. Its first entry ships in the 0.4.38
release fix PR, which seeds an archived consolidation chat into the
installed-upgrade data after build 2966's team rehearsal refused on it.

## 4. Headless member journeys on a fake provider

**Two HTTP journeys over the confirmed problem area, on the acceptance agent.**

- **Provider.** `AcceptanceAgentLauncher` (`agents/acceptance.py`) is already
  a provider mode with no CLI, wired through `create_app(acceptance_agent=True)`.
  Add a `journey` scenario selected by a structured script file in the chat
  scratch folder, not by prompt wording. The script lists turns; each has
  answer text, staged-command calls (handoff), and an outcome:
  `succeed` or `fail:<failure_kind>`. A failure emits
  `AgentEvent(event="error", failure_kind=...)` directly, because the
  acceptance launcher refuses hosts and `classify_agent_failure` only derives
  `transport_lost` for a host. That event also marks the stage unreachable,
  which is what a real transport loss does.
- **Runner.** `tests/test_member_journeys_served.py`, cloned from
  `tests/test_agent_slice_served.py`: uvicorn with the lifespan on, `httpx`,
  sign-in via `/api/owner/redeem`. Time:
  `limits.AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS` is patched to zeros, and
  `seconds_until_automatic_launch` is patched where `rcp.background` imports
  it.
- **Journeys**, each passing or failing on HTTP state:
  1. A handoff delivers, the project turn succeeds, and the Space thread wakes
     once with the report.
  2. The delivered turn fails with `transport_lost`, the auto-retry succeeds,
     and the handoff settles and wakes the Space thread once.
- The approval-card second tap is already covered by
  `test_agent_slice_served.py`. Schedules gain a journey when a lifecycle bug
  appears there.
- Do not fake `launch_admitted` (`test_launch_fakes.py` forbids it). Do not use
  `background_tasks.stream` here: journeys cover prompts and staging.

## 5. Budgets in CI

- **Bundle**: `web/tests/bundleBudget.test.mjs`, in the `web` job after
  `npm run build`. Set `build.manifest: true` and read
  `dist/.vite/manifest.json`, because `emptyOutDir: false` leaves stale chunks
  in `dist/assets`. Budget gzip bytes per named chunk and the initial-load
  total in `web/bundle-budget.json`, about 5% above today (entry JS 291 KB,
  PaperWorkspace 273, chatMarkdown 133, entry CSS 88, Terminals 85,
  GraphViews 48). Raising a ceiling is a reviewed edit.
  - Alternative: `size-limit` adds a dependency for the same comparison.
- **Fetch counts**: `web/tests/fetchBudgets.browser.test.mjs`, reusing the
  `agentThreads.browser` mocked-API harness with Playwright's `page.clock`.
  Measure first, then record ceilings for: an idle open thread over 10 s; a
  running thread over 10 s; a hidden tab (expect no task or transcript reads;
  the `AgentThreadView` live poll is already visibility-gated, so this pins
  it); and an idle project tab (only `resource-watermarks` beats). Repeated
  URLs must be listed in `polled_routes.json`.
- **Backend work**: `tests/test_work_budgets.py`, using piece 2b's tracer.
  Extend `real_use_corpus.py` with a large seed (a 2,000-message thread and
  50 projects, seeded once per module). Measure first, then record statement
  counts per request for the thread list and for `resource-watermarks` across
  the 50 projects. Transcript paging is a product change; open an issue, no
  budget here.

## Risks

- **CI time**: the explorer (one model, under 60 s), two served journeys and
  the large seed land in `python-shard`. The PR reports the added time per
  shard.
- **Flaky served tests**: journeys run threads; they wait on HTTP state with
  the shared wait helpers and never sleep.
- **Baseline merge friction**: sorted, one-line-per-file baselines merge by
  line; a conflict is resolved by rerunning the script.
- **Explorer fidelity**: replay keeps engine state real, but races inside one
  action are out of reach (see piece 1).

## Ownership, order, and checks

Shared contracts stay serial: `pyproject.toml`, `.pre-commit-config.yaml`,
`web/eslint.config.js`, `tests/helpers.py`, and AGENTS.md.

| Step | Owner files | Check |
|---|---|---|
| 1 | `tests/lifecycle_explorer.py`, `tests/test_lifecycle_handoff.py`, fixes it finds | `uv run pytest -n0 tests/test_lifecycle_handoff.py` |
| 2 | `tests/architecture.py` (moved helpers), `tests/test_shipped_sources.py`, ruff per-file targets, the `with` fix | `uv run ruff check src/rcp`, the pytest file |
| 3 | `tests/test_polled_routes.py`, `polled_routes.json`, `trace_requests` in `tests/helpers.py`, the Idempotency-Key rule, the web pause change | the pytest files, `node --experimental-strip-types --test web/tests/agentThreads.test.mjs` |
| 4 | The journey scenario in `agents/acceptance.py`, `tests/test_member_journeys_served.py` | the pytest file |
| 5 | Bundle and fetch budgets | `npm --prefix web run build`, then `node --experimental-strip-types --test web/tests/{bundleBudget,fetchBudgets.browser}.test.mjs` (Playwright Chromium installed) |
| 6 | `tests/test_work_budgets.py`, the large seed | the pytest file |
| 7 | `scripts/comment_ratchet.py`, both baselines, hooks, ESLint rule and config, invariant numbers | `uv run pre-commit run --all-files`, `npm --prefix web run lint` |
| 8 | AGENTS.md diff, `docs/install.md` commands, delete this file | `uv run pytest -n0 tests/test_agent_instructions.py` |

Steps 1, 2, 4 and 5 touch disjoint files and can run in parallel; steps 3 and
6 share `tests/helpers.py` and run in that order. Step 7 runs after every code
step because it records baselines over the final source.

Keep the tests proportional: each rule gets a self-test on a small source
sample, not a corpus.
