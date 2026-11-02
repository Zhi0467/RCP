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
| 4. Member journey | `AcceptanceAgentLauncher`, `test_agent_slice_served.py` | `python-shard` |
| 5. Budgets | the `agentThreads.browser` mocked-API harness | `web` |

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
  retry, or a `provider_auth` failure awaiting sign-in is unsettled with no
  timer.)
- **Safety** in every reachable state: at most one delivered turn and one
  Space wake per handoff; no wake while the Space thread is busy.
- **Liveness**, within the bound (see Mechanism): some sequence of
  environment actions (settle, timer fire, dispatch, restart) reaches a state
  where the handoff is settled, or the delivered task offers `can_retry` to
  the member.

### Mechanism

The explorer drives the handoff machine the way the handoff tests in
`tests/test_agent_thread_commands.py` already do: no worker threads, turns
settled by direct store writes, delivery and wakes by explicit dispatch.

- **World.** `create_named_app` over the manifest fixture, with
  `_spawn_record` patched so no worker runs and `admit_provider_task` patched
  as those tests patch it. The starting state is one Space thread, one project
  thread, and one handoff created through the real handoff command handler.
  The whole temporary tree (data directory and manifest fixture) is built
  once and copied per `World`, because delivery also writes transcripts,
  scratch and project state outside the database.
- **Search.** Depth-first with replay: the live world is kept for the first
  child and rebuilt by replaying the prefix only when backtracking. The bound
  is the smallest depth at which the #368 handoff defects are reachable with
  their fixes reverted (a never-settled handoff while a retry is owed, the
  retry race): the builder reverts each fix locally, finds the depth that
  reports it, and records that as the reason for the bound. 60 s is a
  ceiling, not a target; the test asserts its state count so a growing model
  fails visibly.
- **View.** A hashable tuple with positional names (operation ids are fresh
  per replay): the handoff's derived state from `target_operation_id`,
  `settled_at`, `notified_operation_id` and `refusal_reason`; each turn's
  `status`, `failure_kind`, `can_retry` and `can_resume`; the Space thread's
  busy flag; and the pending timers. Equal views are one state.
- **Actions**, each followed by `dispatch_thread_handoffs`, as the tests do:
  - the source Space turn, the delivered project turn, or a wake turn settles
    by `store.complete_agent_task`, or by `store.fail_agent_task` with
    `failure_kind` `transport_lost` or `other`. A `transport_lost` settle then
    calls `background._auto_retry_transport_loss(record)`, the engine's own
    post-settle hook, which writes the `transport_auto_retry` receipt and
    schedules the timer, so timers come from product code, never from the
    test;
  - a pending retry timer fires;
  - human Retry of the delivered turn;
  - restart: discard the engine and its timer list, build a new
    `BackgroundAgentTasks` over the same files with the same four seams
    applied again, pass it to every later dispatch, and call
    `recover_at_startup()`, which re-arms owed retries.
  `provider_auth` stays out of the explorer: its settlement depends on a
  recorded login that admission, patched out here, would write, and its
  existing tests cover it.
  Attachment expiry is left out: it reads the wall clock, and a handoff turn
  carries no attachments. The clock advances only on a timer firing, by that
  timer's delay.
- **Seams.** `_schedule_transport_retry` appends to `world.timers`;
  `_schedule_remote_reconciliation` is a no-op, as in the tests;
  `seconds_until_automatic_launch` is patched to 0 where `rcp.background`
  imports it; `store.now` reads the world's clock.
- **Bounded liveness.** Only states at depth at most bound − 3 are checked:
  from each, environment actions alone (settle, timer fire, dispatch,
  restart) must reach a settled handoff or a member offer within 3 steps.
- **Limit.** Actions are atomic, so check-then-admit races inside one action
  are out of reach. Races stay with targeted thread tests.

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
- **The set.** A file is shipped when one of these reads it whole:
  - `remote_bundle`: its module list moves into a helper,
    `remote_bundle_modules(driver)` in `providers/__init__.py`, which the test
    calls and maps to files.
  - Whole-file wrappers, named with their package in the test:
    `_remote_script(name)` reads `rcp/transport/<name>`, and
    `terminal_source(name)` reads `rcp/terminals/<name>`. The scan takes the
    literal `name` at each call.
  - A reader call: a `.read_text()` or `.read_bytes()` whose receiver chain is
    rooted at `Path(__file__)` (directly or through a local bound to it, such
    as `root` in `browser/service.py`) or at `importlib.resources.files(...)`,
    with literal path segments. Only `.py` targets count; `.js`, `.sh` and
    `.service` reads are ignored.
  - A reader call or wrapper call whose target is not literal fails the test
    with its location. Exempt by name: the wrapper bodies themselves and
    `_module_source` in `providers/__init__.py`.
  - `inspect.getsource(X)` where `X` is an imported module counts as a
    whole-file read of that module (`repository_preview.py` ships
    `rcp/repository_window.py` this way); a non-module or non-literal
    argument adds nothing.
  - `_paper_tree_remote_script` in `transport/state.py` reads all of
    `state.py` but ships two extracted functions. It switches to
    `inspect.getsource` on those two functions, so the whole-file rule holds
    and `state.py` stays out of the set. Those two functions are knowingly
    unchecked by the py39 rules.
  - Self-tests cover each resolver on a small sample. The test also asserts
    that the ruff glob list equals the derived set.
- Out of scope: the inline `script = """…"""` strings in `run_stage.py`,
  `state.py` and `sources/preflight.py`.

### (b) Polled routes stay cheap

The web polls about 14 routes (`RefreshScheduler`, `useAgentThreads`, the
`AgentThreadView` live poll, and others).

- `tests/test_polled_routes.py` with `tests/fixtures/polled_routes.json`, which
  lists each polled `(method, path)`.
- It calls each route on an app seeded with one project, one machine, one
  chat and one agent thread, whose ids fill the path parameters, and with
  every staleness timestamp old enough that `refresh_if_stale` would fire.
  One tracer, `trace_requests(app)` in `tests/helpers.py`, built from
  `_TracingAppStore` (`test_storage.py`), records: statements, `BEGIN
  IMMEDIATE` and DML through a SQLite trace callback on the app's own store
  connection factory; `git`, `ssh` and `rsync` through a patched
  `subprocess`; outbound HTTP through a patched `urllib.request.urlopen`;
  and `refresh_if_stale` and `open_display` as remote reads.
- A route passes when it has zero `BEGIN IMMEDIATE`, DML, subprocesses,
  outbound HTTP and remote reads. Statement counts are not budgeted here.
- Coverage limit: the list is hand-kept, and its completeness is checked only
  on the screens piece 5's fetch-budget test loads. A poll added elsewhere is
  caught at review.
- Today's violators go into a shrinking allowlist in the same JSON, each with a
  one-line reason: team device pairing GET (`BEGIN IMMEDIATE`),
  `GET /api/space/machines/{id}/browser` (SSH), `GET /api/space/runs`
  (conditional write plus remote refresh), `GET /api/agent-threads`
  (`ensure_agent_threads` writes), `GET /api/update-notice` (HTTP plus
  `git rev-parse` when stale), the project chat transcript (`open_display`).
- Piece 5's fetch-budget test reads the same JSON.

### (c) Idempotency-Key admission

- **Scope.** Every server endpoint that accepts the key already admits it,
  so no server rule is added. The drift that happened ran the other way: the
  web sent the key where the server ignores it.
- **Today**: project pause (`api/tasks.py`) and Space thread pause
  (`api/agent_threads.py`) do not declare the key, yet the web sends it.
  Pause stores no operation or episode, and pausing twice is already a no-op,
  so the fix is on the web: `controlThreadTask` in
  `web/src/agents/agentThreadsApi.ts` and `operateTask` in `web/src/App.tsx`
  stop sending the key for `pause`.
- **Pinned in tests**: `web/tests/agentWindow.test.mjs` already asserts which
  headers `controlThreadTask` sends; its pause case changes to assert no key,
  and it gains resume and retry cases asserting the key. `operateTask` gets
  the same three cases in the test file that covers `App.tsx` task controls.

Shared AST helpers move, unchanged, from `test_module_boundaries.py`
(`_python_files`, `_call_name`, and what `_call_name` needs: `_import_aliases`,
`_module_package`, `SOURCE`) into `tests/architecture.py`; both
`test_module_boundaries.py` and `test_shipped_sources.py` import them from
there.

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
  - The `lint` script in `web/package.json` becomes `eslint src tests`.
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

## 4. Headless member journey on a fake provider

**One HTTP journey over the confirmed problem area, on the acceptance agent.**

- **Provider.** `AcceptanceAgentLauncher` (`agents/acceptance.py`) is already
  a provider mode with no CLI, wired through `create_app(acceptance_agent=True)`.
  Add one fixed scenario, `space_handoff`, with three roles:
  - **First Space turn**: selected the existing way, by the `scenario` key of
    the state file `_scenario` reads from the turn's working folder; the test
    writes it into the Space thread's scratch before the first message. The
    turn runs the staged `handoff` command by the command prefix in the
    contract (`_run_campaign_argv`), with the marker `space_handoff` in the
    handoff message, and sets `handed_off` in its state file. (The wording
    rule covers scenario selection only.)
  - **Delivered project turn**: selected by that marker in its prompt, which
    `_scenario` already checks; it answers and succeeds.
  - **Space wake turn**: the same Space state file, now with `handed_off`
    set; it answers and succeeds without a second handoff.
  - Every role writes `outcome.json` as `generic_watchers` does, so the report
    carries a `turn_outcome`. `space_handoff` joins `_scenario`'s persisted
    set and the `AcceptanceLaunchRecord.scenario` literal.
  - Failures and retries stay with the piece 1 explorer: `transport_lost` is
    only classified for a remote host, and acceptance runs locally.
- **Runner.** `tests/test_member_journeys_served.py`, cloned from
  `tests/test_agent_slice_served.py`: uvicorn with the lifespan on, `httpx`,
  sign-in via `/api/owner/redeem`. `seconds_until_automatic_launch` is
  patched to 0 where `rcp.background` imports it.
- **Journey**, passing or failing on HTTP state: the member asks the Space
  thread to hand work to a project, the handoff delivers, the project turn
  succeeds, and the Space thread wakes once with the report. This covers the
  prompts, staging and command path the explorer skips.
- Do not fake `launch_admitted` (`test_launch_fakes.py` forbids it).

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
- **Flaky served tests**: the journey runs threads; it waits on HTTP state
  with the shared wait helpers and never sleeps.
- **Baseline merge friction**: sorted, one-line-per-file baselines merge by
  line; a conflict is resolved by rerunning the script.
- **Explorer fidelity**: actions are atomic store writes plus dispatch, so
  worker-thread races are out of reach (see piece 1).

## Ownership, order, and checks

Shared contracts stay serial: `pyproject.toml`, `.pre-commit-config.yaml`,
`web/eslint.config.js`, and AGENTS.md.

| Step | Owner files | Check |
|---|---|---|
| 1 | `tests/lifecycle_explorer.py`, `tests/test_lifecycle_handoff.py`, fixes it finds | `uv run pytest -n0 tests/test_lifecycle_handoff.py` |
| 2 | `tests/architecture.py` (moved helpers), `tests/test_shipped_sources.py`, ruff per-file targets, the `with` fix, `remote_bundle_modules`, the `_paper_tree_remote_script` switch | `uv run ruff check src/rcp`, the pytest file |
| 3 | `tests/test_polled_routes.py`, `polled_routes.json`, `trace_requests` in `tests/helpers.py`, the Idempotency-Key rule, the web pause change | the pytest files, `node --experimental-strip-types --test web/tests/agentWindow.test.mjs` |
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
