# Lifecycle explorers (temporary design)

This file is deleted before merge. Its rules move to the explorer modules'
docstrings and `docs/install.md` (commands).

Goal: every lifecycle that has shipped a bug is checked over every action order
to a bound, against real code, and the checks themselves are checked: every
declared writer runs, every status value is reached, and no rule is vacuous.

Decided with the human on 2026-11-02:
- One PR, together with the test comment shrink (done on this branch).
- Explorers for the six machines with bug history: handoff (exists), agent
  task, Experiment episode with watchers, agent schedules, nightly
  consolidation, Proposals with approval cards.
- Actions and rules live in code only, as named objects with docstrings.
- Every other stored state machine is written down in code, without an
  explorer.
- A defect an explorer confirms is fixed in this PR at its root, with its trace
  as a regression case.
- Each machine's depth is set by its budget; everything runs in PR CI. No new
  job.
- A scan of the source keeps each machine's writer list complete. The human
  asked how CI can guarantee every action is listed; the scan is that
  guarantee, with its blind spots stated in §2.

Extend `tests/lifecycle_explorer.py` (the handoff explorer) into a shared engine
plus one module per machine.

## Alternatives

- **TLA+ models.** Explore deeper and split actions finer, but restate the
  logic, which drifts from the code, and need Java in CI. Rejected on
  2026-11-02 (D1 of the dev-flow PR).
- **Hypothesis `RuleBasedStateMachine`.** Random sequences with shrinking; not
  exhaustive, so a short bad order can be missed for many runs. Kept in mind
  as a later deep-walk tier.
- **A pure-Python model per machine.** Fast, but it is the TLA+ drift problem
  in Python.
- **Random long walks past the bound.** Cheap depth without guarantees.
  Deferred; the engine's `explore` can grow a walk mode later.

## Non-goals

Remote stages and SSH (beyond one patched outage in the episode explorer),
races between threads, races inside one function, the
UI, and provider behavior. Each action is atomic. The approval-card tap is not
split, so its suspected check-then-sync race stays out of reach and is reported
in the PR body.

## 1. Shared engine

`tests/lifecycles/engine.py`. A machine module defines one `Machine`:

```python
class Machine(Protocol):
    name: str
    bound: int                       # PR bound
    states: int                      # exact state count at that bound
    horizon: int                     # liveness look-ahead, default 3
    covers: Mapping[object, tuple[str, int | None]]  # Literal type -> (View field, column in its row tuples)
    tables: frozenset[str]           # tables this machine owns (see §2)
    writers: tuple[Callable, ...]    # product functions that change this machine's state
    unreached_values: Mapping[str, str]       # status value -> why no state reaches it
    unreached_writers: Mapping[Callable, str] # writer -> why no action calls it
    rules: tuple[Rule, ...]

    def seams(self) -> ContextManager[None]: ...   # class-level patches, entered once
    def start(self, root: Path, clock: Clock) -> None: ...  # seed the start state into a fresh tree
    def boot(self, root: Path, clock: Clock) -> World: ...  # build store and engine over a tree, with instance patches
    def actions(self, view: View) -> list[Action]: ...
    def apply(self, world: World, action: Action) -> None: ...
    def view(self, world: World) -> View: ...
    def save_memory(self, world: World) -> Memory: ...      # see "Memory" below
    def restore_memory(self, world: World, saved: Memory) -> None: ...
```

`boot` reapplies the module's instance patches and binds `store.now` to the
clock on every call; each module's docstring lists its patches. `start` runs
under the same `seams` and binds `store.now` to the same clock, so seeded
times (a target "due three days ago") are relative to the fixed instant.
`start` receives a fresh tree; step 1 pulls a plain manifest builder out
of the `manifest` fixture in `tests/conftest.py` so `start` can build one.

- `World` holds the live store, engine and clock for one machine run.
- An `Action` is a frozen dataclass: `name`, `args`, `environment: bool`.
- A `View` is a `NamedTuple` per machine. A field holding many rows is a sorted
  tuple of per-row tuples; coverage reads a `Literal`'s values from the field
  and column `covers` names (column `None` for a scalar field), so a
  matching string in another column never counts.
- **Memory** is `Memory(engine, environment)`. `engine` is what the product
  holds in process (timers, workers, decided retries); `environment` is what
  the explorer holds (the clock, the episode's outage flag). Both name tasks
  by the view's lineage names, never by operation id, because ids are random
  and two paths to one view create different ids.
- **Restart** is an ordinary action. It advances the clock by 1 s (so strict
  `claimed_before`-style comparisons see the earlier claim), drops `engine`
  memory, keeps `environment` memory, builds the world through `boot`, and
  runs exactly the startup reconciliation `api/app.py` runs for the
  machine's tables, in the app's order. Each module's docstring lists those
  calls: handoff and agent task `recover_at_startup`; Proposals
  `settle_interrupted_approval_cards(claimed_before=<clock>)`; episode,
  schedules and consolidation the calls the same startup sequence makes.
- **Clock:** one `Clock` holder per exploration, starting at the fixed instant
  2026-01-15T12:00:00Z, so state counts never depend on when CI runs. `boot`
  binds `store.now` and the module's clock seams to it; local dates are read
  in UTC. Every view holds clock-relative fields (seconds to `next_fire_at`,
  to `expires_at`, to a timer); two states merge only when those match.
- **Shared clock seam:** `seams` replaces `datetime` in `rcp.storage.rows`
  with a `datetime` subclass whose `now()` reads the holder, so
  `fromisoformat` and every other use keep working. Handoff and agent task
  use it for attachment expiry. The episode module also patches
  `rcp.watchers._now` to read the holder.

**Rules** are named objects with a docstring stating the rule in one sentence
and a `source` (spec heading, invariant number, or "code guard at
<function>"):
- `Safety(holds(world), applies(world))`
- `Step(holds(before, world), applies(before, world))`
- `Goal(reached(view), allowed: frozenset[str] | None = None)`: from every
  state at depth ≤ bound − horizon, some run of at most `horizon` actions
  from `allowed` reaches it. The default is every environment action except
  restart, so neither a restart nor a member action can hide a stuck state.
  Each module lists any widened `allowed` set with its reason. Handoff widens
  its goal to include restart, as today (crash recovery is restart's job).

**State save and restore.** One mode for every machine, as the handoff
explorer does today: a state's save copies the tree except the SQLite files,
takes each SQLite database through `sqlite3.Connection.backup` (the store
runs in WAL mode, so a file copy of an open database can tear), and keeps
`save_memory`. A step replaces the tree
(`rmtree` then `copytree`), calls `boot`, then `restore_memory`. Whole trees
mean no file list to keep and no file a sibling branch wrote survives; a
fresh boot means no stale history cache (`HistoryManager` revisions and
engines).
- **Restore check:** the first explored edge of each distinct action (name
  and arguments, so each settle outcome counts) is also replayed from `start`
  on a fresh tree, so every action variant that touches memory is replayed at
  least once; the test asserts the view equals the restored
  view. A memory field `save_memory` misses then fails CI.
- **Merge check:** when an edge reaches a view already explored, the engine
  asserts its saved memory (lineage-named, apart from the absolute clock)
  equals the stored one. Memory that matters but is left out of the view then fails CI instead
  of silently merging two different states.
- **Measured in step 1:** a handoff edge costs about 0.1 s, almost all of it
  booting the app: each store open spends 22 ms re-entering WAL mode (a
  file-system cost, the same with a read-only `PRAGMA journal_mode`) and
  project registration about 35 ms. Handoff runs 150 edges in about 16 s
  locally. A live mode (database backup into a running store) would skip the
  boot but reintroduce the stale-cache risk the fresh boot removes, so the
  budget below is raised instead; live mode stays the lever if depth is ever
  worth that risk.

**Bounds.** Step 1 measures the cost per edge on handoff and records each
machine's state count as it is built. **Minimum bound:** at least
horizon + 1, at least the length of every Safety or Step trace a suspected
defect or witness needs, and at least the depth of every suspected stuck
state plus the horizon (a Goal is checked only from depth ≤ bound − horizon).
**Budget:** ≤ 20 s locally per machine, counting its exploration and its
witness runs, and a hard ceiling of 120 s per test enforced by the
autouse `hang_guard` in `tests/lifecycles/conftest.py`: a timer thread that
dumps every stack and exits (faulthandler's own timer spun forever in a frame
walk on a loaded host and hung the shard). When the minimum does not fit the budget,
the machine first drops member actions that no rule or suspected trace
needs, then lowers its horizon to 2. If it still does not fit, the bound
drops below the minimum, each suspected trace it no longer reaches becomes a
plain regression test built from that trace (§5), and the docstring records
both. The CI ceiling always wins.

## 2. Checking the checks

Each machine's test runs its exploration once and asserts all of these from
that one `Exploration`:

1. **No rule breaks** within the bound.
2. **Every status value is reached.** For each `Literal` in `covers`, every
   value appears in some explored view, or is in `unreached_values` with a
   reason.
3. **Every declared writer runs.** Inside `apply` only (never during `start`,
   `boot` or rule checks, so seeding never counts as coverage), the engine
   counts calls
   to each function in `writers` with `sys.monitoring` `PY_START` events on
   `inspect.unwrap(f).__code__`, so every call counts however it was imported
   or bound, and a shared decorator's wrapper code is never counted. Each must
   run at least once, or be in `unreached_writers` with a reason.
   `sys.monitoring` exists only from Python 3.12; on the 3.11 shards this one
   assertion is skipped with that reason. The 3.12 shards of the same PR CI
   run enforce it, so no change merges unchecked.
4. **No rule is vacuous.** Each `Safety` and `Step` rule's `applies` was true
   in at least one explored state or edge; each `Goal` was checked from at
   least one state where it was not already reached.

A fifth test, `tests/lifecycles/test_writers.py`, keeps the `writers` lists
complete.
- **Ownership.** Every table the scan finds a write to has exactly one owner
  (a machine or catalog entry listing it in `tables`, no two listing the same
  table), or sits in a `NOT_LIFECYCLE` list with a reason (receipts, events,
  `digest_events`, and other append-only or cache tables). A new table that
  nobody declares fails the test. `graph_runs` belongs to the agent task machine; handoff, episode and
  consolidation read it but do not own it. Declared gates map to their table
  (`_transition_agent_task` → `graph_runs`).
- **Scan.** It scans `src/rcp` string literals and f-strings with a
  case-insensitive regular expression for `UPDATE [OR <word>] <t>`,
  `INSERT [OR <word>] INTO <t>` and `DELETE FROM <t>`, with `\s+` (newlines
  included) between keywords and `<t>` optionally quoted with `"` or a
  backtick, plus calls to declared gates. `ON CONFLICT … DO UPDATE SET` names
  no table and is skipped. The outermost enclosing function or method (by AST
  line range) is the writer, since a nested `def` cannot be imported or
  counted. A literal `<t>` counts only if it is a table in the
  `sqlite_master` of a freshly created `AppStore`, so prose in docstrings
  ("update the row") is never read as a table; an interpolated `<t>` always
  counts.
- **Rule.** Every writer of an owned table appears in its owner's `writers` or
  in its owner's `unreached_writers` with a reason (for example "driven by
  the episode explorer"). So the owner accounts for every writer of its
  table, whichever explorer drives it.
- An interpolated `<t>` (`{table}`) is an unknown table: its function must be
  declared by some owner or allowlisted with a reason (project deletion and
  transfer loop over tables this way). Migrations and backup-API restores sit
  in the same short allowlist.
- Blind spots, stated in the test's docstring: a helper that receives the
  table name as an argument, and SQL assembled outside one literal.
- Its self-test runs the scan on a small sample with an f-string `SET`, an
  `INSERT OR REPLACE`, `UPDATE OR IGNORE {table}`, `DELETE FROM "{table}"`, a
  `ON CONFLICT (id) DO UPDATE SET`, a gate call, a `WHERE`-only mention and
  a docstring saying "update the row", and asserts the exact set found.

**Witnesses.** A rule gets a witness only where one is free: a real fix that a
seam can revert, or a trace this PR confirms. The witness is a seam patch (the
same seams existing tests patch) or a seeded bad state, never a copied old
function body. For `Safety` and `Step` rules the witness replays its trace
with the patch and asserts that named rule reports. For `Goal` rules it runs
the machine's own exploration under the patch and asserts that named goal
reports. Rules read engine state directly (`_workers`, `_retry_decided`),
never through a function a witness stubs. Starting witnesses, each with its
seam:
- handoff: stub `BackgroundAgentTasks._rearm_owed_transport_retries` to a
  no-op (reverts `3cb576199`); stub `retry_undecided` to return False, which
  changes only the product call in `runs/agent_thread_commands.py` (reverts
  the #368 race fix);
- consolidation: seed the 0.4.35 queued request with `graph_target`
  (`b12897489`) and a legacy archived consolidation chat row (`09f7519f6`);
  both are seeded bad states, not code patches;
- episode: wrap the `_experiment_stop_unresolved_tasks` staticmethod (as a
  `staticmethod`) so its failed and paused rows report as running, which
  makes `_settle_experiment_loop_stop` treat them as live (reverts
  `e87b06f58`); the builder confirms the witness trace leads through a
  failed or paused leaf, not an interrupted one.
Each builder confirms its seam reverts the fix before keeping the witness;
one that cannot be reverted by a seam is dropped.

## 3. The six machines

Each module's start state reuses its existing test builder by moving the
builder into the module and importing it from both places.

### Handoff (`tests/lifecycles/handoff.py`)
Moved from `tests/lifecycle_explorer.py`; `tests/test_lifecycle_handoff.py`
becomes `tests/lifecycles/test_handoff.py`, and both old files are deleted.
Step 1 is a pure move: same actions, same 106 states at bound 4, and the
"unsettled with no retry coming" rule now reads `_workers` and
`_retry_decided` directly instead of calling `retry_undecided`.

A later commit in the same step adds attachment expiry, aiming at the
stood-down-retry defect of `3ef7773ec`. The handoff builder has no attachment
path, so after dispatch the start state writes one attachment descriptor into
the delivered turn's `request_json`, standing in for a member's uploaded file
(a seeded state the product makes through upload, not through this builder),
with `expires_at` 30 minutes after the start instant. A new action advances
the clock past it. That commit records the new bound and count, and follows
the budget rule above if the trace does not fit.

### Agent task (`tests/lifecycles/agent_task.py`)
- **Start:** `BackgroundAgentTasks(store, _done_stream)` over the `_store`
  helper and `_admitted_launch_task` of `tests/test_background.py`, plus one
  provider login that `resolved_task_login` finds; no app.
- **Actions** and the product call each makes: claim (`launch_admitted`,
  with `_spawn_record` stubbed as handoff does, so no thread starts);
  settle(success | transport_lost | provider_auth | session_refused | paused)
  runs the claimed worker's body inline in the explorer thread with a
  scripted stream per outcome, each taken from the existing test in
  `tests/test_background.py` that produces that outcome; finish
  (`_task_settled`); member Pause, Resume and Retry
  (`BackgroundAgentTasks.pause`, `.resume`, `.retry`); retry timer fire
  (`_run_transport_retry`); refused fire (`_run_transport_retry` after a
  child exists, so `_transport_retry_superseded` refuses it); restart
  (`recover_at_startup`); member removal (`request_member_removal_pause`);
  project deletion (`stop_for_project_deletion`); sign-in release
  (`resume_provider_account`, which claims with `_claim_auth_retry` and then
  calls `retry`); clock past attachment
  expiry (the start state carries one attachment, as in handoff).
- **Rules:** status moves follow `AGENT_TASK_TRANSITIONS`; Resume and Retry
  never rewrite the parent's status; at most one recovery child per parent;
  at most one active task per chat; the transport ladder never exceeds its
  limit across restarts; no timer stays pending once a child exists;
  `Goal`: a `pausing` row settles without a restart.
- **Suspected:** Pause on a queued row with no worker sticks in `pausing`
  until restart; `interrupted → queued` and `paused → interrupted` rewrites
  in restore, transfer and episode reports contradict the transition table.

### Experiment episode with watchers (`tests/lifecycles/episode.py`)
- **Start:** the `_Loop` class of `tests/test_experiment_stop.py` (its `loop`
  fixture), one running episode with a bound session and one watcher; local
  stage inside the run's tree. Covers episode wrap-up and report attempts
  too.
- **Actions:** Stop, settle the turn, exit-turn with an ending signal, finish,
  watcher check (active | completed | degraded), poll-reconcile, report
  outcome, retire watcher, restart, restart with a transient remote outage.
- **Rules:** invariant 10g's one graceful Stop fence; a Stop settles once no
  leaf is live; Stop skips the report; at most three report attempts;
  `Goal`: `wrapping_up` ends without a restart; a claim takes only completed
  or degraded watchers not yet notified; a lone degraded watcher wakes once;
  Stop retires only the current episode's observations.
- **Suspected:** startup leaves a Stop recovery pending on a transient remote
  outage, then `settle_ready_experiment_loop_stops` abandons the interrupted
  turn anyway. The outage action seeds the leaf's `stage_host` and turns
  an `outage` flag on; `seams` patches `RemoteRunStage.directory_exists` for
  the whole exploration to raise while the flag is on and return True once it
  is off. The flag lives in `save_memory` and the view, and an "outage ends"
  action clears it. No call reaches SSH.

### Agent schedules (`tests/lifecycles/schedules.py`)
- **Start:** the `schedule_store` fixture of `tests/test_agent_schedules.py`,
  moved to a plain builder. Database only.
- **Actions:** add (member turn | wake), pause, resume, cancel, enable, tick
  (+1, +15, +60 minutes), settle the wake, member sends, close thread,
  restore, restart.
- **Rules:** the spec's private-schedule rules (gap, pending notes, one atomic
  claim, deferral keeps notes, closed thread fails the wake, lost access
  disables, missed fires count, paused never fires, restore disables, 20 per
  thread, wakes are captured Work turns).
- **Suspected:** `missed_fires` is not capped by `--count`; restore skips
  paused rows; thread close keeps `next_fire_at` while dispatch clears it; a
  claim `ValueError` retries silently every tick.

### Nightly consolidation (`tests/lifecycles/consolidation.py`)
- **Start:** the `runtime` fixture of `tests/test_consolidation_runtime.py`,
  moved to a plain builder: one target due three days ago.
- **Actions:** poll, next night, settle the task (success | fail | pause),
  report, commit, archive the chat (route and legacy row), Keep, Dismiss,
  member turn in the chat, restart, restore.
- **Rules:** one occurrence per target per local date; a failure advances the
  occurrence, never coverage; settled outcomes are final; only Keep or
  Dismiss closes a row; invariant 10g's target match; `Goal`: an unsettled
  run's task never stays queued; `Goal`: a due target advances or holds an
  unsettled run.

### Proposals with approval cards (`tests/lifecycles/proposals.py`)
- **Start:** the `filed` fixture of `tests/test_proposal_refresh.py`, moved to
  a plain builder, plus one card built as `real_card(api, action)` in
  `tests/test_approval_cards.py` does. `api` is the tuple
  `(app, signed_in_client(app), store, project_id)` over `filed`'s app with
  `reconcile_question_answers = Mock()`, rebuilt on each `boot`; `action =
  {"kind": "proposal", "project_id": …, "proposal_id": PROPOSAL, "decision":
  "approved"}`.
- **Actions:** agent files, agent withdraws, a dependency moves, member
  approves or rejects through Sync, refresh, ask card, tap (approve | decline
  | old hash), outage tap (`rcp.api.approval_cards.approval_pins` raises
  `StateUnavailable`, as `tests/test_approval_cards.py` does), claim then
  restart, expire, close thread.
- **Rules:** invariant 3 (only humans resolve, agents only withdraw); status
  changes only from pending; Sync withdraws a stale Proposal without effects;
  a card is single-use and settles stale only when its pins change; an outage
  releases it; restart never reruns it; `Goal`: every pending card ends.

## 4. Every other machine, written down

`tests/lifecycles/catalog.py` declares the 20 other stored machines from the
code inventory (project provisioning, transfer, provider login, member
removal, enrollment codes, device pairing, project invitation, Auto-research
recovery, Auto-research children, episode isolation and merge, conversation
worktree, branch merge, compute job, paper changes, paper live, mirrored
terminal, artifact, server step journal, restore, project deletion; episode
wrap-up belongs to the episode explorer). Each entry is a frozen dataclass
`Entry(name, status, tables, writers, rules)`: `status` is the status
`Literal` type or the column name where none exists, `tables` the tables it
owns, `writers` imported callables, and `rules` named objects with docstrings
and sources. Import and basedpyright check the references; the writer scan of
§2 treats an entry as the owner of its tables.

## 5. Defects

When an explorer reports a failure, the builder:
1. Confirms it with a plain regression test built from the trace.
2. Fixes the root cause. Where code and spec disagree (the forbidden status
   rewrites), the fix follows the spec unless the spec is wrong; the commit
   says which and why, and changes the spec in the same commit if needed.
3. Makes the trace that rule's witness when it has none.

A suspected defect the explorer cannot reach goes into the PR body with why.

## Ownership, order, and checks

Shared and serial: `tests/lifecycles/engine.py`, `tests/conftest.py`,
`tests/shards.py`, `pyproject.toml`, and `tests/lifecycles/test_writers.py`.
Fixture moves touch each machine's existing test file; that file belongs to
the machine's step.

| Step | Owner files | Check |
|---|---|---|
| 1 | `tests/lifecycles/{__init__,engine,handoff,test_handoff}.py`, delete the two old files; manifest builder; explorer weights in `tests/shards.py`; measured cost per edge; then the attachment-expiry commit | `uv run pytest -n0 tests/lifecycles/test_handoff.py` (106 states after the move; the recorded count after expiry) |
| 2 | completeness assertions and `test_writers.py`, against handoff; writers of tables owned by later steps sit in a temporary allowlist section | `uv run pytest -n0 tests/lifecycles/test_handoff.py tests/lifecycles/test_writers.py` |
| 3a | `agent_task.py`, its test, `tests/test_background.py` builder move, fixes | its test, `tests/test_background.py`, tests for touched product modules |
| 3b | `episode.py`, its test, `tests/test_experiment_stop.py` builder move, fixes | likewise |
| 3c | `schedules.py`, its test, `tests/test_agent_schedules.py` builder move, fixes | likewise |
| 3d | `consolidation.py`, its test, `tests/test_consolidation_runtime.py` builder move, fixes | likewise |
| 3e | `proposals.py`, its test, `tests/test_proposal_refresh.py` and `tests/test_approval_cards.py` builder moves, fixes | likewise |
| 4 | `catalog.py` | `test_writers.py` and basedpyright |
| 5 | integrator: removes each merged step's entries from the temporary allowlist, then deletes the empty section | `test_writers.py` |
| 6 | `docs/install.md` command, spec links, delete this file | `test_agent_instructions.py` |

Steps 1 and 2 are serial. Steps 3a–3e run in parallel, each in its own
worktree, after step 2; step 4 runs alongside them. Those steps never edit
`test_writers.py`: each reports which of its machine's writers it now owns,
and the integrator (step 5) removes those entries when merging it. The
integrator also serializes defect fixes that touch the same product module.

## Risks

- **CI time.** Each machine is ≤ 20 s locally and capped at 120 s per test, and
  the suite runs once per Python version (3.11 and 3.12). `tests/shards.py`
  weighs files by size, which underweights these small slow files, so step 1
  counts each `tests/lifecycles/test_*.py` file as the size of the largest
  test file; `--dist loadfile` then
  spreads them across workers.
- **Exact state counts** break on view changes; that is deliberate, and the
  failure names the new count.
- **Timing on loaded runners.** Bounds are set against the local budget, well
  under the CI ceiling.
- **Coupling.** Explorers patch private engine seams; an engine refactor must
  keep them green.
- **Fix scope.** Eight suspected defects may grow the PR; each fix is its own
  commit with its trace.
