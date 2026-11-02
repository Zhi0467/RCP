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
| 5. Budgets | `graphProjectionPerformance.test.mjs`, the `agentThreads.browser` harness | `web` |

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
  `transport_retry_owed` is true; and while a retry timer for the delivered
  turn is pending in the world, `_handoff_turn_settled` is false. (The
  converse does not hold by design: a queued or running turn, an undecided
  retry, or a `provider_auth` failure with sign-in candidates is unsettled
  with no timer.)
- **Safety** in every reachable state: at most one delivered turn and one
  Space wake per handoff; no wake while the Space thread is busy.
- **Liveness**: from every reachable state, environment actions alone
  (settle, timer fire, dispatch pass, restart) reach a state where the handoff
  is settled, or the delivered task offers `can_retry` or `can_resume` to the
  member.

### Mechanism

- `tests/lifecycle_explorer.py`:
  - **Replay, not snapshot.** A state is the action sequence that reached it.
    Each step builds a fresh `World` and replays the sequence. A `World` is
    the app `create_named_app` builds, as in the handoff tests in
    `tests/test_agent_thread_commands.py`: its store, `app.state.catalog`,
    its `BackgroundAgentTasks`, and the `on_task_settled` wiring that
    reconciles handoffs. The migrated empty database is created once and
    copied per `World`. In-memory engine state (`_workers`,
    `_retry_decided`, `_controls`) is therefore always real, never restored.
  - The starting state is one Space thread, one project thread, and one
    handoff created through the real handoff command handler, as those tests
    do; `admit_provider_task` is patched there the same way.
  - `Action(name, enabled(view), apply(world))`. `view(world)` is a hashable
    tuple read from rows: the handoff row's state, each turn's `status`,
    `failure_kind`, `can_retry`, `can_resume` and receipts, the Space thread's
    busy flag, and the pending timer list. Two sequences with equal views are
    one state.
  - BFS over views to depth 8. A failure prints the shortest action sequence.
    The test asserts a ceiling on the number of states visited, recorded
    from the first run, so a growing model fails visibly; the test timeout
    only stops a hang.
- Seams, all in the test:
  - `_schedule_transport_retry` appends to `world.timers` instead of starting a
    `threading.Timer`; the "timer fires" action pops one and calls the
    callback.
  - `store.now` is patched on the instance to a fixed clock that actions
    advance.
  - `_schedule_remote_reconciliation` is patched the same way, and
    `seconds_until_automatic_launch` is patched to 0 where `rcp.background`
    imports it.
  - Turn outcomes come through `background_tasks.stream`. The fake stream
    sets the execution flags the engine reads: `stage_host` and
    `stage_unreachable` for `transport_lost`, `provider_auth_refused` for
    `provider_auth`. The world wraps `on_task_settled` to set one
    `threading.Event` per operation; each action waits on it, so it is
    atomic.
- Actions: the source turn settles; the delivered turn settles as succeeded,
  `transport_lost`, `provider_auth`, other failure, or Stopped; a timer fires
  (admitted or refused); restart (a fresh engine over the same file, running
  startup reconciliation); sign-in release; human Retry or Resume; a dispatch
  pass. Attachment expiry is left out: it reads the wall clock, and a handoff
  turn carries no attachments.
- **Limit**: actions run one at a time, so check-then-admit races inside one
  action (for example around the retry admission in `background.py`) are out
  of reach. Races stay with targeted thread tests.

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
- **The set**, derived from the readers. A call that ships a whole file adds
  it; a call that ships only extracted functions (`inspect.getsource`) adds
  nothing.
  - `remote_bundle`: call the real module-list resolution
    (`_REMOTE_BASE_MODULES` plus each provider class's `__module__`) and map
    modules to files.
  - Wrappers that read a whole file from a fixed package, each named with its
    package in the test: `_remote_script(name)` → `rcp/transport/`,
    `terminal_source(name)` → `rcp/terminals/`. The AST scan takes the
    literal `name` at each call.
  - `importlib.resources.files(pkg).joinpath("x.py")` and `Path` chains with
    literal segments (`Path(__file__).parent / "x.py"`,
    `root.parent / "agents" / "staged_hidden_read.py"`): resolved from the
    literal segments relative to the module.
  - Any reader call whose target the scan cannot resolve to a literal fails
    the test with its location, so a new caller is never missed silently.
    `isolated_python_argv` is not a reader: its scripts are staged from the
    `importlib.resources` reads in `command_protocol.py`, which the scan
    already covers.
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
  as remote reads.
- Today's violators go into a shrinking allowlist in the same JSON, each with a
  one-line reason: team device pairing GET (`BEGIN IMMEDIATE`),
  `GET /api/space/machines/{id}/browser` (SSH), `GET /api/space/runs`
  (conditional write plus remote refresh), `GET /api/agent-threads`
  (`ensure_agent_threads` writes), `GET /api/update-notice` (HTTP plus
  `git rev-parse` when stale), the project chat transcript (`open_display`).
- Piece 5's fetch-budget test reads the same JSON.

### (c) Idempotency-Key admission

- **Rule**, in `test_route_inventory.py`, over a hand-kept fixture
  `tests/fixtures/idempotent_routes.json` that lists each keyed
  `(method, path)` with its web call site, like `polled_routes.json`:
  - every listed route declares the header, and its endpoint calls
    `client_request_admission` directly or through a named helper
    (`_space_control_admission`), checked by AST over the endpoint body;
  - the web side checks only that the files and counts of the
    `"Idempotency-Key"` literal in `web/src` match the fixture's call sites,
    so a new sender cannot appear without an entry.
- **Today**: only project pause (`api/tasks.py`, the pause handler) and Space
  thread pause (`api/agent_threads.py`) ignore the key the web sends. Pause
  stores no operation or episode, and pausing twice is already a no-op, so the
  fix is on the web: both senders, `controlThreadTask` in
  `web/src/agents/agentThreadsApi.ts` and `operateTask` in `web/src/App.tsx`,
  stop sending the key for `pause`.

Shared AST helpers move, unchanged, from `test_module_boundaries.py`
(`_python_files`, `_call_name`, and what `_call_name` needs: `_import_aliases`,
`_module_package`, `SOURCE`) into `tests/architecture.py`, and both old and new
tests import them from there.

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

**AGENTS.md diff** (223 lines today, 229 after; the ceiling is 230):

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

**One HTTP journey over the confirmed problem area, on the acceptance agent.**

- **Provider.** `AcceptanceAgentLauncher` (`agents/acceptance.py`) is already
  a provider mode with no CLI, wired through `create_app(acceptance_agent=True)`.
  Add a `journey` scenario selected by a structured script file in the chat
  scratch folder, not by prompt wording. The script lists turns; each has
  answer text and staged-command calls (handoff). Every turn succeeds: the
  acceptance agent runs locally, and `transport_lost` is only classified for a
  remote host, so failure and retry stay with the piece 1 explorer.
- **Runner.** `tests/test_member_journeys_served.py`, cloned from
  `tests/test_agent_slice_served.py`: uvicorn with the lifespan on, `httpx`,
  sign-in via `/api/owner/redeem`. `seconds_until_automatic_launch` is
  patched to 0 where `rcp.background` imports it.
- **Journey**, passing or failing on HTTP state: the member asks the Space
  thread to hand work to a project, the handoff delivers, the project turn
  succeeds, and the Space thread wakes once with the report. This covers the
  prompts, staging and command path the explorer's `stream` seam skips.
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
  URLs must be listed in `polled_routes.json`; this is the one place that
  rule is checked.
- **Backend work**: already pinned. `test_resource_watermarks.py` asserts a
  fixed read count for the batched heartbeat, and
  `test_agent_threads_storage.py` a fixed statement count for the thread
  list. No new backend budget test. Transcript paging is a product change;
  open an issue.

## Risks

- **CI time**: the explorer (one model) and one served journey land in
  `python-shard`. The PR reports the added time per shard.
- **Flaky served tests**: journeys run threads; they wait on HTTP state with
  the shared wait helpers and never sleep.
- **Baseline merge friction**: sorted, one-line-per-file baselines merge by
  line; a conflict is resolved by rerunning the script.
- **Explorer fidelity**: replay keeps engine state real, but races inside one
  action are out of reach (see piece 1).

## Ownership, order, and checks

Shared contracts stay serial: `pyproject.toml`, `.pre-commit-config.yaml`,
`web/eslint.config.js`, and AGENTS.md.

| Step | Owner files | Check |
|---|---|---|
| 1 | `tests/lifecycle_explorer.py`, `tests/test_lifecycle_handoff.py`, fixes it finds | `uv run pytest -n0 tests/test_lifecycle_handoff.py` |
| 2 | `tests/architecture.py` (moved helpers), `tests/test_shipped_sources.py`, ruff per-file targets, the `with` fix | `uv run ruff check src/rcp`, the pytest file |
| 3 | `tests/test_polled_routes.py`, `polled_routes.json`, `idempotent_routes.json`, `trace_requests` in `tests/helpers.py`, the Idempotency-Key rule, the web pause change | the pytest files, `node --experimental-strip-types --test web/tests/agentThreads.test.mjs` |
| 4 | The journey scenario in `agents/acceptance.py`, `tests/test_member_journeys_served.py` | the pytest file |
| 5 | Bundle and fetch budgets | `npm --prefix web run build`, then `node --experimental-strip-types --test web/tests/{bundleBudget,fetchBudgets.browser}.test.mjs` (Playwright Chromium installed) |
| 6 | `scripts/comment_ratchet.py`, both baselines, hooks, ESLint rule and config, invariant numbers | `uv run pre-commit run --all-files`, `npm --prefix web run lint` |
| 7 | AGENTS.md diff, `docs/install.md` commands, delete this file | `uv run pytest -n0 tests/test_agent_instructions.py` |

Steps 1, 2 and 4 touch disjoint files and can run in parallel. Step 3 needs
step 2's `tests/architecture.py`, and step 5 reads step 3's
`polled_routes.json`. Step 6 runs after every code step because it records
baselines over the final source.

Keep the tests proportional: each rule gets a self-test on a small source
sample, not a corpus.
