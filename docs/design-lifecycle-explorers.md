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
- Deeper search comes from cheaper states; everything runs in PR CI. No new job.

Extend `tests/lifecycle_explorer.py` (the handoff explorer) into a shared engine
plus one module per machine.

## Alternatives

- **TLA+ models.** Explore deeper and split actions finer, but restate the
  logic, which drifts from the code, and need Java in CI. Rejected on
  2026-11-02 (D1 of the dev-flow PR).
- **Hypothesis `RuleBasedStateMachine`.** Random sequences with shrinking; not
  exhaustive, so a short bad order can be missed for many runs, and failures
  are not reproducible from a seed in another environment. Kept in mind as a
  later deep-walk tier.
- **A pure-Python model per machine.** Fast, but it is the TLA+ drift problem
  in Python.
- **Random long walks past the bound.** Cheap depth without guarantees.
  Deferred; the engine's `explore` can grow a walk mode later.

## Non-goals

Remote stages and SSH, races between threads, races inside one function, the
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
    covers: Mapping[object, str]     # Literal type -> view field it appears in
    writers: tuple[Callable, ...]    # product functions that change this machine's state
    unreached: Mapping[object, str]  # status value or writer -> reason it is not reached
    rules: tuple[Rule, ...]

    def seams(self) -> ContextManager[None]: ...   # patches for every engine it builds
    def start(self, root: Path) -> World: ...      # seeded start state in a fresh tree
    def actions(self, view: View) -> list[Action]: ...
    def apply(self, world: World, action: Action) -> None: ...
    def view(self, world: World) -> View: ...
    def save_memory(self, world: World) -> object: ...      # timers, workers, decided, …
    def restore_memory(self, world: World, saved: object) -> None: ...
```

- `World` holds the live store, engine and clock for one machine run.
- An `Action` is a frozen dataclass: `name`, `args`, `environment: bool`.
- **Restart** is an ordinary action whose `apply` builds a new engine over the
  same store, drops in-memory state, and runs the machine's startup recovery.
- **Clock:** every machine's view includes clock-relative fields (seconds to
  `next_fire_at`, to `expires_at`, to a timer). Two states merge only when
  their time-relative fields match.

**Rules** are named objects with a docstring stating the rule in one sentence
and a `source` (spec heading, invariant number, or "code guard at
<function>"):
- `Safety(holds(world), applies(world))`
- `Step(holds(before, world), applies(before, world))`
- `Goal(reached(view), allowed: frozenset[str])`: from every state at depth
  ≤ bound − horizon, some run of at most `horizon` actions from `allowed`
  reaches it. `allowed` excludes restart where a stuck state must not need one
  (for example "a `pausing` row settles": restart is not allowed).

**State save and restore.** The engine keeps one live `World` per machine run.
A state's save is `sqlite3.Connection.backup` into an in-memory database, the
bytes of the files the machine declares, and `save_memory`. A step restores by
backing up the saved database into the live store's file (after closing the
store's cached connections), rewriting the declared files, and calling
`restore_memory`. No app boot per step.

Step 1 measures this on the handoff machine, per edge. Target: ≤ 0.02 s per
edge. Each machine's bound is the deepest that keeps its test ≤ 10 s locally
(CI runners are about 2× slower, so ≤ 20 s there). If step 1 misses the
target, bounds simply come out lower; the design does not depend on the
number.

## 2. Checking the checks

Each machine's test runs its exploration once and asserts all of these from
that one `Exploration`:

1. **No rule breaks** within the bound.
2. **Every status value is reached.** For each `Literal` in `covers`, every
   value appears in some explored view, or is in `unreached` with a reason.
3. **Every declared writer runs.** During exploration the engine wraps each
   function in `writers` with a call counter (a monkeypatch of that function
   only). Each must run at least once, or be in `unreached` with a reason.
4. **No rule is vacuous.** Each `Safety` and `Step` rule's `applies` was true
   in at least one explored state or edge; each `Goal` was checked from at
   least one state where it was not already reached.

A fifth test, `tests/lifecycles/test_writers.py`, keeps the `writers` lists
complete. It scans `src/rcp` with a regular expression for `UPDATE <table>`,
`INSERT [OR …] INTO <table>` and `DELETE FROM <table>` in string literals and
f-strings, plus calls to declared gates (`_transition_agent_task`), for the
tables the six machines and the catalog name. Each enclosing function (by AST
line range) must appear in some machine's `writers`, in the catalog, or in a
short allowlist with a reason (migrations, backup-API restores). A table may be
named by several machines; listing a writer once anywhere satisfies it. Its
self-test runs the scan on a small source sample with an f-string `SET`, an
`INSERT OR REPLACE`, a gate call and a `WHERE`-only mention, and asserts the
exact set found.

**Witnesses.** A rule gets a witness only where one is free: a real fix that a
seam can revert, or a trace this PR confirms. The witness is a seam patch (the
same seams existing tests patch) or a seeded bad state, never a copied old
function body. For `Safety` and `Step` rules the witness replays its trace
with the patch and asserts the rule reports. For `Goal` rules it runs a
bounded exploration (bound ≤ 3) under the patch and asserts the goal reports.
Starting witnesses: the handoff's `3cb576199` startup re-arm and
`retry_undecided`; consolidation's `b12897489` and `09f7519f6`; episode's
`e87b06f58`.

## 3. The six machines

Each module's start state reuses its existing test builder by moving the
builder into the module and importing it from both places.

### Handoff (`tests/lifecycles/handoff.py`)
Moved from `tests/lifecycle_explorer.py`; `tests/test_lifecycle_handoff.py`
becomes `tests/lifecycles/test_handoff.py`, and both old files are deleted.
Adds a clock-expiry action (attachment expiry), which makes the
stood-down-retry defect of `3ef7773ec` reachable.

### Agent task (`tests/lifecycles/agent_task.py`)
- **Start:** `BackgroundAgentTasks(store, _done_stream)` over the `_store`
  helper and `_admitted_launch_task` of `tests/test_background.py`; no app.
- **Actions:** claim, settle(success | transport_lost | provider_auth |
  session_refused | paused), finish (`_task_settled`), member Pause, Resume,
  Retry, retry timer fire, refused fire, restart, member removal, project
  deletion, sign-in release, clock past attachment expiry.
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
  turn anyway.

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
  a plain builder, plus one approve card built as `real_card(api, …,
  action="approve")` does.
- **Actions:** agent files, agent withdraws, a dependency moves, member
  approves or rejects through Sync, refresh, ask card, tap (approve | decline
  | old hash), outage tap, claim then restart, expire, close thread.
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
wrap-up belongs to the episode explorer). Each entry references the real
objects directly: its status `Literal` type (or the column name where none
exists), its writer functions as imported callables, and its rules as named
objects with docstrings and sources. Import and basedpyright check the
references; the writer scan of §2 counts the catalog's writers as declared.

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
`pyproject.toml`. Fixture moves touch each machine's existing test file; that
file belongs to the machine's step.

| Step | Owner files | Check |
|---|---|---|
| 1 | `tests/lifecycles/{__init__,engine,handoff,test_handoff}.py`, delete the two old files; measured cost per edge | `uv run pytest -n0 tests/lifecycles/test_handoff.py` |
| 2 | completeness assertions and `test_writers.py`, against handoff | the two files |
| 3a | `agent_task.py`, its test, `tests/test_background.py` builder move, fixes | its test, `tests/test_background.py`, tests for touched product modules |
| 3b | `episode.py`, its test, `tests/test_experiment_stop.py` builder move, fixes | likewise |
| 3c | `schedules.py`, its test, `tests/test_agent_schedules.py` builder move, fixes | likewise |
| 3d | `consolidation.py`, its test, `tests/test_consolidation_runtime.py` builder move, fixes | likewise |
| 3e | `proposals.py`, its test, `tests/test_proposal_refresh.py` and `tests/test_approval_cards.py` builder moves, fixes | likewise |
| 4 | `catalog.py` | `test_writers.py` and basedpyright |
| 5 | `docs/install.md` command, spec links, delete this file | `test_agent_instructions.py` |

Steps 1 and 2 are serial. Steps 3a–3e run in parallel, each in its own
worktree, after step 2; step 4 runs alongside them. The integrator serializes
defect fixes that touch the same product module.

## Risks

- **CI time.** Six machines at ≤ 20 s on CI is about two minutes. Each machine
  has its own test file, so `--dist loadfile` spreads them across workers.
- **Exact state counts** break on view changes; that is deliberate, and the
  failure names the new count.
- **Timing on loaded runners.** Bounds use half the CI budget locally.
- **Coupling.** Explorers patch private engine seams; an engine refactor must
  keep them green.
- **Fix scope.** Eight suspected defects may grow the PR; each fix is its own
  commit with its trace.
