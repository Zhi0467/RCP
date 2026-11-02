# Dev-flow hardening (temporary design)

This file is deleted before merge. Its rules move to AGENTS.md (Verification),
`docs/install.md` (commands), and the docstrings of the new checks.

Goal: make each recurring failure a mechanical check. Every check runs in the
existing CI jobs (`lint`, `python-shard`, `web`), on PRs and on main alike.
Every check counts work or reachable states, not wall time.

Six pieces, one PR. Each piece extends an existing mechanism.

Decided with the human on 2026-11-02: the explorer over real code (not TLA+ or
Hypothesis), per-file comment counts, tests under the ban, the shipped set
derived by scanning its readers, small violations fixed here and the rest on a
shrinking allowlist, HTTP-only member journeys, and one pre-commit ratchet
script for per-file rules.

| Piece | Extends | Runs in |
|---|---|---|
| 1. Lifecycle model checks | `background_tasks.stream` test seam, store fixtures | `python-shard` |
| 2. Lint rules | `test_module_boundaries.py`, `test_route_inventory.py`, ruff, eslint | `lint`, `python-shard` |
| 3. Comment ban + ratchet | basedpyright baseline, `test_project_types.py` ratchet, eslint suppressions | `lint` |
| 4. Member journeys | `AcceptanceAgentLauncher`, `test_agent_slice_served.py` | `python-shard` |
| 5. Budgets | `graphProjectionPerformance.test.mjs`, `_TracingAppStore`, `real_use_corpus.py` | `web`, `python-shard` |
| 6. Real-data regressions | installed-upgrade seed (`tests/server_upgrade_state.py`), release-upgrade browser data | `installed upgrade`, `web` |

## 1. Lifecycle model checks

**Extend the store tests to a bounded exhaustive search over the real code.**

### Options

| Option | Checks real code? | Exhaustive? | Cost |
|---|---|---|---|
| TLA+/PlusCal + TLC | No. It is a parallel model that drifts from the SQL. | Yes, within bounds | Needs Java. The local `/usr/bin/java` is the macOS stub. brew has only the GUI cask (and `openjdk`). uv cannot install it. CI needs `setup-java` plus a pinned `tla2tools.jar`. A new language for reviewers. |
| Hypothesis `RuleBasedStateMachine` | Yes | No. Random sequences, with shrinking. | A new dev dependency. Liveness needs a drain step at the end of each run. |
| Explicit-state explorer over the real store | Yes | Yes, within bounds | About 150 lines of test helper. Each step costs real SQLite and engine time. |

**Recommendation: the explorer.** The machines live in SQL statements spread
over several owners: handoff rows, `graph_runs`, receipts, timers. Separate
predicates must agree. For example, `_handoff_turn_settled` must match the
engine's own view of whether a retry is still owed (`owed_transport_retry_operation_ids`,
`_rearm_owed_transport_retries`, `_transport_retry_superseded`). Only a check
that runs those exact functions catches drift between them. Keep TLA+ for a
later cross-process protocol (StateWorkspace locking, invariant 8). There, the
design is the thing under test.

### Mechanism

- `tests/lifecycle_explorer.py` holds the helper:
  - `World` is a real `AppStore` on a temporary SQLite file plus a real
    `BackgroundAgentTasks`.
  - `Action(name, enabled(view), apply(world))`.
  - `view(world)` is a hashable abstraction read from the real rows.
  - The search is BFS over views. A snapshot is the SQLite backup plus the list
    of pending timers. Restoring injects those timers without running startup
    reconciliation, so "restart" stays its own action.
- It checks three kinds of property:
  - **Safety** in every reachable state.
  - **Agreement** between predicates that must match.
  - **No stuck state**: from every reachable state, environment actions alone
    reach a quiescent state, or a state that shows a human control (AG EF).
- A failure prints the shortest trace of actions.
- Seams, all in tests:
  - `_schedule_transport_retry` appends to `world.timers` instead of starting a
    thread.
  - `store.now` is a fixed clock.
  - Turn outcomes come through the existing `background_tasks.stream` seam, so
    `_task_settled` and retry run for real.
  - Use the shared wait helpers to wait for settlement.
- Bounds are declared per model: one Space thread, one project thread, one
  handoff, depth ≤ 8. Budget: under 30 s per model.

### Models, in build order

1. **Handoff + retry** (`agent_thread_commands`, `background`, `provider_login`).
   - Actions:
     - the source turn settles;
     - the delivered turn settles as succeeded, `transport_lost`, `provider_auth`, other failure, or Stopped;
     - a timer fires and admission is either admitted or refused;
     - restart;
     - sign-in release;
     - human Retry or Resume;
     - an attachment batch expires (`can_retry` becomes false);
     - a dispatch pass.
   - Safety:
     - at most one delivered turn and one wake per handoff;
     - no wake while the Space thread is busy;
     - no notification before settlement.
   - Agreement: handoff "settled" means no retry is owed.
   - **Candidate defect to confirm first** (not yet reproduced). Take a
     `transport_lost` turn with a `transport_auto_retry` receipt, then make
     `can_retry` false (the batch expires) or set an episode recovery refusal.
     `_transport_retry_superseded` and `_rearm_owed_transport_retries` stand
     down without writing an `_exhausted` receipt. `_handoff_turn_settled` then
     returns False forever, so the Space thread is never woken.
2. **Agent schedules**: active, paused, disabled, done and cancelled. The
   actions are add, tick at `now`, pause, resume, cancel, claim wake, and
   limits reached. Invariants:
   - every pending note belongs to an active or done schedule;
   - a paused schedule never fires;
   - `fires_left` never rises;
   - the active count stays at or under `AGENT_SCHEDULES_MAX_ACTIVE`.
3. **Approval cards**: pending, running, settled and expired, including an
   interrupted run. The invariant: one action per card, and "approved" only
   when the action committed.
4. **Task status**: queued, running, pausing, succeeded, failed, interrupted
   and paused. Retry and Resume lineage has at most one continuation child per
   parent.
5. **Stop fence (invariant 10g)**: one graceful Stop per episode. A Stop
   settles exactly once. There is no fresh-session fallback.

Files: `tests/lifecycle_explorer.py`, `tests/test_lifecycle_<machine>.py`.
Product code changes only for defects the models find. Each fix ships with a
regression trace.

## 2. Lint rules

### (a) Host-shipped Python

About 50 files are shipped as source to hosts whose `python3` may be 3.9. Six
different readers ship them:
- `_remote_script`
- `remote_bundle`
- `importlib.resources`
- `Path(__file__)` reads
- `inspect.getsource`
- `isolated_python_argv`

Two tests guard a few of these files today: `test_staged_command_client.py`
and `test_hidden_read_wrapper.py`.

Extend them to the full set:
- **Syntax**: in pyproject, use ruff `per-file-target-version = "py39"` for the
  shipped set. Ruff 0.16 then reports 3.10+ syntax, and `UP` stops proposing
  3.10+ rewrites, which retires the `# noqa: UP041` pragmas. Today it would
  flag one case: the parenthesized `with` at `transport/remote_turn_supervisor.py:249`.
- **Imports**: `tests/test_shipped_sources.py` checks the following.
  - Top-level imports are in `sys.stdlib_module_names`, minus the modules that
    are newer than 3.9 (for example `tomllib`).
  - No unguarded top-level `rcp` import. Imports under `if`, `TYPE_CHECKING`
    or inside functions stay allowed, as today.
  - `ast.parse(feature_version=(3, 9))` succeeds.
- **The set**: the test derives it by scanning for the six reader calls with a
  literal `.py` target, with self-tests for the scanner. It also asserts that
  the ruff glob list equals the derived set.
- Out of scope: the ~12 inline `script = """…"""` strings in `run_stage.py`,
  `state.py` and `sources/preflight.py`.

### (b) Polled routes stay cheap

The web polls about 14 routes (`RefreshScheduler`, `useAgentThreads`,
`AgentThreadView` live poll, and others).

Extend the route inventory with `tests/test_polled_routes.py`:
- `tests/fixtures/polled_routes.json` lists each polled `(method, path)` with
  its web call site. The test asserts that each one is in
  `_FROZEN_ROUTE_INVENTORY`.
- It calls each route on a seeded app with tracers:
  - a SQLite trace callback for `BEGIN IMMEDIATE` and DML;
  - a patched `subprocess` that records `git`, `ssh` and `rsync`;
  - `refresh_if_stale` and `open_display` recorded as remote reads.

  A runtime check sees the real transitive path. An AST check cannot follow
  service calls.
- Today's violators go into a shrinking allowlist, each with a one-line reason:
  - team device pairing GET (`BEGIN IMMEDIATE`)
  - `GET /api/space/machines/{id}/browser` (SSH)
  - `GET /api/space/runs` (conditional write plus remote refresh)
  - `GET /api/agent-threads` (`ensure_agent_threads` writes)
  - `GET /api/update-notice` (HTTP plus `git rev-parse` when stale)
  - project chat transcript (`open_display`)
- The browser fetch-budget tests (piece 5) read the same JSON. Every URL a
  screen fetches more than once in its window must be on the list. That keeps
  the list tied to the web.

### (c) Recurring review findings

| Finding | Rule | Where | Existing hits |
|---|---|---|---|
| Re-implementing `read_local_regular_file` | A function that uses `O_NOFOLLOW` and `S_ISREG` together, or `lstat`/`is_symlink` followed by `read_bytes`/`read_text`, outside `regular_file_reader.py` and the shipped set. | Python ratchet (piece 3 mechanism) | About 55 + 6 |
| Detecting a wake or handoff by wording | `startsWith`/`includes`/`endsWith`/`match`/`split` on `.text`/`.content`/`.message`/`.body`. Python: `startswith`/`in` on the same attributes. | eslint `no-restricted-syntax` scoped to `web/src/{agents,chat}`. Python ratchet scoped to `runs/agent_thread*`, `background.py` and `api/agent_threads.py`. | `agentThreadModel.ts:297`, `background.py:3355` |
| Idempotency-Key skipping `client_request_admission` | A route whose dependant declares an `idempotency-key` header calls `client_request_admission`. Every `(method, path)` the web sends the header to (8 call sites) declares it. | `test_route_inventory.py` (reads `route.dependant`). The web call sites are listed in the same test. | No route skips admission. **Four routes ignore the header the web sends**: project pause (`tasks.py:996`) and Space thread pause, resume and retry (`agent_threads.py:485/503/524`). Fix them in this PR. |

Shared AST helpers for the new tests go in `tests/architecture.py`
(`python_files`, `call_name`, ratchet compare and write). Old tests keep their
own helpers. No refactor.

## 3. Comment ban with allowlist and ratchet

**Rule.** No comments, with three exceptions:
- **Invariant citations**: the comment matches `invariant \d+[a-z]?`.
- **Lint pragmas**: `noqa`, `type: ignore`, `pyright:`, `basedpyright:`, `fmt:`,
  `eslint-disable*`, `@ts-*`, `prettier-ignore`.
- **Docstrings on public owners**: module docstrings, public classes and public
  functions, plus any docstring that cites an invariant. JSDoc `/** */` on an
  exported TS symbol counts as a docstring.

Everything else is either deleted, or recorded in the baseline when it already
exists.

**Current counts (this branch).**

| Area | Inline `#` / `//` | Private docstrings | Public docstrings (allowed) | Pragmas |
|---|---|---|---|---|
| `src/rcp` | 4,888 | 765 | 2,710 | 62 |
| `tests` | 2,512 | 129 | 610 | 236 |
| `supervisor`, `packaging`, `scripts` | 146 | 15 | 84 | 0 |
| `web/src` | 534 line + 8 block | — | 715 JSDoc (exported share unmeasured) | 11 |
| `web/tests` | 436 line + 2 block | — | 27 JSDoc | 0 |

Only 6 Python comments cite an invariant today. Several invariant owners named
in AGENTS.md hold their promise in an uncited comment or docstring. Examples:
- the 10f watermark comment in `materialize._apply_patch`;
- invariant 7 in the private `_atomic_write`.

Before the ratchet lands, add the number to each such owner, so a later
comment sweep cannot delete one.

**Mechanism: mirror the basedpyright baseline.**
- **Python**: add a `comment-ratchet` pre-commit hook (`scripts/comment_ratchet.py`).
  - It uses `tokenize` and `ast` over every tracked `.py` file
    (`pass_filenames: false`, like basedpyright).
  - Its baseline is `.ratchet/baseline.json`: `{file: {rule: count}}`.
  - A run fails when any count rises. It rewrites the file when a count falls,
    and drops entries for deleted files.
  - CI's `pre-commit run --all-files --show-diff-on-failure` fails when the
    shrunk baseline is not committed. That is the same enforcement basedpyright
    gets.
  - The piece-2 Python ratchets (regular-file reader, wording) use the same
    script and baseline.
- **TypeScript**: add a local eslint rule `rcp/no-comments` inline in
  `eslint.config.js`, using `sourceCode.getAllComments()`.
  - Its baseline is ESLint 10's own bulk suppressions file
    (`web/eslint-suppressions.json`, created with `--suppress-all`). It is also
    counted per file and per rule, and unpruned suppressions fail by default,
    so it is a native shrink-only ratchet.
  - Extend `lint` from `eslint src` to `eslint src tests`.
- **Why count per file.** Counts survive line moves and match eslint's
  suppression format. The cost: inside a file in the baseline, deleting one old
  comment lets a new one in unnoticed. Review catches that, and the count never
  rises.

**AGENTS.md diff.** AGENTS.md is at 223 lines. The ceiling is 230.

```diff
@@ Verification
 - Pre-commit runs `basedpyright` against `.basedpyright/baseline.json`. Fix new
   errors. Never use `--writebaseline`. The baseline only shrinks.
+- Write no comments. Name things, test them, or put the why in the owning spec.
+  Allowed: invariant citations, lint pragmas, and docstrings on public modules,
+  classes, and functions. `.ratchet/baseline.json` and
+  `web/eslint-suppressions.json` hold older comments and lint debt and only
+  shrink. Commit the shrunk files. Never regrow them.
+- Turn each recurring review finding into a check here or in an architecture test.
+- A bug a live drive or team rehearsal finds also adds its data shape to the
+  installed-upgrade or release-upgrade seed in the same PR.
```

The "Keep brief rationale beside the rule it protects" line (Documentation
lifecycle) is about docs and stays. "Module-local promises belong in their
owner's docstring" (Stable invariants) stays, and the allowlist covers it. A
harness instruction to match the surrounding comment density loses to
AGENTS.md, because these are project instructions. The new bullet states the
rule outright, so no harness default applies.

## 4. Headless member journeys on a fake provider

**Extend `AcceptanceAgentLauncher` with a scripted journey scenario, and clone
the served test.**

- **Provider.** `AcceptanceAgentLauncher` (`agents/acceptance.py`) is already a
  provider mode with no CLI, wired through `create_app(acceptance_agent=True)`
  and `rcp serve --acceptance-agent`. It runs the real staged command client.
  - Add a `journey` scenario. It is selected by a structured script file in the
    chat scratch folder, not by prompt wording.
  - The script is a list of turns. Each turn has answer text, command-client
    calls (handoff, schedule, ask with card) and an outcome
    (`succeed | fail:<failure_kind> | hang`).
- **Runner.** Clone `tests/test_agent_slice_served.py` as
  `tests/test_member_journeys_served.py`.
  - It runs uvicorn with the lifespan on, drives it with `httpx`, and signs in
    via `/api/owner/redeem`.
  - Time is controlled: an instance override of `store.now`, plus
    `app.state.consolidation_poller.clock`, with `poll_once()` called directly.
    `seconds_until_automatic_launch` is patched to 0.
- **Journeys.** Each is one parametrized case and passes or fails on HTTP
  state:
  1. A handoff delivers, the project turn succeeds, and the Space thread wakes
     once with the report.
  2. A schedule fires, the wake turn runs, and `fires_left` drops.
  3. An approval card is tapped, the action runs once, and a second tap gets
     409.
  4. A turn fails with `transport_lost`, the auto-retry succeeds, and a handoff
     waiting on it settles.
  5. A schedule is paused, the tick passes, it is resumed, and it skips the
     missed fires.
- The cases reuse the piece-1 scenarios, so a model-checker trace can become a
  journey.
- The member is HTTP only. The approval-card tap is covered at HTTP level; no
  browser journey is added.
- **CI.** It runs inside the existing python shards. No new job is needed, and
  PR and main stay identical.
- Do not fake `launch_admitted` (`test_launch_fakes.py` forbids it). Do not use
  `background_tasks.stream` here: journeys must cover prompts and staging.

## 5. Budgets in CI

All of these count work. Wall time appears only as one loose ceiling.

- **Bundle**: `web/tests/bundleBudget.test.mjs`, in the `web` job after
  `npm run build`.
  - Set `build.manifest: true` and read `dist/.vite/manifest.json`.
    `emptyOutDir: false` leaves stale chunks in `dist/assets` (263 files,
    25 MB locally), so scanning the folder is wrong.
  - Budget gzip bytes per named chunk and for the initial-load total, in
    `web/bundle-budget.json`. Ceilings sit about 5% above today's sizes:

    | Chunk | gzip KB |
    |---|---|
    | entry JS | 291 |
    | PaperWorkspace | 273 |
    | chatMarkdown | 133 |
    | entry CSS | 88 |
    | Terminals | 85 |
    | GraphViews | 48 |

  - Raising a ceiling is a reviewed edit to that file.
- **Fetch counts**: `web/tests/fetchBudgets.browser.test.mjs`.
  - It reuses the `agentThreads.browser` mocked-API harness, with
    `page.clock` (Playwright 1.62) so no real time passes, and forces
    `visibilityState` explicitly.
  - Budgets:
    - An idle open thread makes 0 transcript fetches in 10 s, and at most 5
      version reads.
    - A running thread makes at most 1 transcript fetch per 2 s.
    - A hidden tab makes 0 transcript fetches. **This fails today**: the
      `AgentThreadView` live poll has no visibility gate. Fix it in this PR.
    - An idle project tab makes only `resource-watermarks` beats.
  - Repeated URLs must be in `polled_routes.json` (piece 2b).
- **Backend work**: `tests/test_work_budgets.py`.
  - Extend `real_use_corpus.py` with a large seed: a 2,000-message thread and
    50 projects, seeded once per module.
  - Pull a `count_queries(store)` helper into `tests/helpers.py` from
    `_TracingAppStore` (`test_storage.py:1140`).
  - Budgets:
    - SQL statements per request for the thread list and for
      `resource-watermarks` across 50 projects. Measure first, then record.
    - Response bytes and file bytes read for the transcript.
    - Zero subprocesses on polled routes.
  - The transcript route has no paging and reads the whole file on every 2 s
    live poll. Its byte budget therefore grows with the thread. Record it as a
    named exception with an issue; paging is a product change.
  - One end-to-end 2,000-message transcript request keeps a 10× wall-time
    ceiling.

## 6. Real-data regressions feed pre-promote CI

**Rule.** A bug that a live drive, a candidate drive, or a team rehearsal
exposes is fixed together with the data shape that triggered it, added to the
automatic pre-promote path in the same PR. Green CI must catch that class next
time without a human drive.

- **Where the shape goes.** The installed-upgrade seed
  (`tests/server_upgrade_state.py`) when the bug needs an old release's data;
  the release-upgrade browser data (`tests/release_upgrade_browser_data.py`)
  when it needs a served journey; an old-data migration fixture when it is a
  storage shape.
- **First entry.** Build 2966's rehearsal refused because members had archived
  a consolidation chat, which 0.4.38's archived-chat admission rule refused.
  The release fix PR seeds an archived consolidation chat into the
  installed-upgrade data. This PR states the rule; it does not move that seed.
- **Check.** None mechanical: a PR that fixes a drive-found bug says which seed
  now holds its shape. The AGENTS.md bullet below states it.

## Ownership, order, and checks

Shared contracts stay serial: `pyproject.toml` (ruff targets),
`.pre-commit-config.yaml`, `web/eslint.config.js` and AGENTS.md.

| Step | Owner files | Check |
|---|---|---|
| 1 | `tests/lifecycle_explorer.py`, `tests/test_lifecycle_handoff.py`, then fix the stuck-handoff defect if it is confirmed | `uv run pytest -n0 tests/test_lifecycle_handoff.py` |
| 2 | Remaining lifecycle models | Same, per file |
| 3 | `tests/architecture.py`, `tests/test_shipped_sources.py`, ruff per-file targets, the `remote_turn_supervisor` `with` fix | `uv run ruff check src/rcp`, the pytest file |
| 4 | `scripts/comment_ratchet.py`, `.ratchet/baseline.json`, the hook, invariant-number additions | `uv run pre-commit run --all-files` |
| 5 | `rcp/no-comments` eslint rule, the wording rule, suppressions, `lint` covers tests | `npm --prefix web run lint` |
| 6 | `tests/test_polled_routes.py`, `polled_routes.json`, the Idempotency-Key route rule and the 4 route fixes | the pytest files |
| 7 | The journey scenario in `agents/acceptance.py`, `tests/test_member_journeys_served.py` | the pytest file |
| 8 | Bundle and fetch budgets, the live-poll visibility fix | `node --experimental-strip-types --test web/tests/{bundleBudget,fetchBudgets.browser}.test.mjs` |
| 9 | `tests/test_work_budgets.py`, the large seed, `count_queries` | the pytest file |
| 10 | The AGENTS.md diff, the `docs/install.md` commands, deleting this file | `uv run pytest -n0 tests/test_agent_instructions.py` |

Steps 1–2, 3–6, 7 and 8–9 touch disjoint files and can run in parallel. Step 4
rewrites many files' baseline entries, so it lands after the code steps or
reruns last.

Keep the tests proportional: each rule gets a self-test on a small source
sample, not a corpus.
