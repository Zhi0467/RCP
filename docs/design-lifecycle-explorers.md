# Lifecycle explorers (temporary design)

This file is deleted before merge. Its rules move to the explorer modules'
docstrings, `docs/install.md` (commands) and AGENTS.md where noted.

Goal: every lifecycle that has shipped a bug is checked over every action order
to a bound, against real code, and the checks themselves are checked: every
real writer and every status value is reached, and every rule can fail.

Decided with the human on 2026-11-02:
- One PR, together with the test comment shrink.
- Explorers for the six machines with bug history: handoff (exists), agent
  task, Experiment episode with watchers, agent schedules, nightly
  consolidation, Proposals with approval cards.
- Actions and rules live in code only, as named objects with docstrings. Specs
  link to the module instead of restating them.
- Every other stored state machine is written down in code too, without an
  explorer.
- A defect an explorer confirms is fixed in this PR at its root, with its trace
  as a regression case.
- Deeper search comes from cheaper states; everything runs in PR CI. No new job.

Extend `tests/lifecycle_explorer.py` (the handoff explorer) into a shared engine
plus one module per machine.

## 1. Shared engine

`tests/lifecycles/engine.py` keeps the handoff explorer's search and adds the
pieces every machine needs. A machine module supplies one `Machine`:

- `start(root) -> World`: the seeded start state.
- `actions(view) -> list[Action]`: every action enabled in a state. An action
  is a named object: name, arguments, and whether it is an environment action
  (settle, timer, restart) or a member action (Pause, Retry, Keep).
- `view(world) -> View`: the hashable state, with positional names, as today.
- `rules`: a tuple of named `Rule` objects. Each has a docstring that states
  the rule in one sentence and a `source` (spec heading, invariant number, or
  "code guard at <function>"). Three kinds:
  - `Safety(holds(world) -> bool, applies(world) -> bool)`
  - `Step(holds(before_view, world) -> bool, applies(before_view, world) -> bool)`
  - `Goal(reached(view) -> bool)` for liveness: from every state at depth
    ≤ bound − horizon, some run of at most `horizon` environment actions
    reaches the goal.
- `bound` and `states`: the PR bound and the exact state count it yields.

`explore(machine)` returns an `Exploration`: states, edges, failures,
per-rule `applied` counts, per-action `fired` counts, and the set of status
values seen.

**Cheaper states.** Today a state is a copied file tree plus a SQLite backup,
and a step boots a backend over it (~0.15 s). The engine saves the database
with `sqlite3.Connection.backup` into an in-memory connection, and keeps only
the files a machine declares in `files` (for example a stage directory) as
bytes in memory. A step restores into a fresh temporary directory reused per
worker. Target: ≤ 0.02 s per state, measured on the handoff machine before
the other machines are written. Each machine's bound is the deepest that keeps
its test ≤ 20 s on a CI runner; the test asserts its state count.

## 2. Checking the checks

Four tests in `tests/lifecycles/test_completeness.py`, parametrized over the
six machines:

1. **Every status value is reached.** A machine declares `covers`, a mapping
   from a `Literal` type to the field it reads (for example
   `AgentTaskStatus -> graph_runs.status`). The test asserts that every value
   of each `Literal` appears in some explored state, or sits in the machine's
   `unreached` mapping with a one-line reason. Adding a status to the type
   fails CI until an explorer reaches it.
2. **Every writer runs.** A machine declares `writes`: the tables and columns
   it owns (for example `graph_runs.status`, `graph_runs.failure_kind`). An AST
   scan of `src/rcp` finds every function whose SQL text updates or inserts
   into those columns, or that calls the single gate for them
   (`_transition_agent_task` for task status). During exploration a profiler
   records which functions ran. Every scanned writer must have run, or sit in
   `unreached` with a reason. A new code path that changes the machine's state
   without an action fails CI.
3. **Every rule can fail.** Each rule names a `witness`: a short action trace
   plus a patch that restores a known-bad behavior (a reverted fix, or a
   one-line mutant). The test replays the trace with the patch and asserts the
   rule reports. A rule whose condition can never be false fails CI.
4. **No rule is vacuous.** Every rule's `applied` count is above zero in the
   PR-bound exploration.

Witness patches name real fixes where one exists (for example `3cb576199`'s
startup re-arm for "a restart never drops an owed retry"). The patch is a
monkeypatch in the test, never a product flag.

## 3. The six machines

Each module lists its actions and rules in code; the lists below are the
starting set from the code maps. Seams are the ones the named existing tests
already patch.

### Handoff (`tests/lifecycles/handoff.py`)
Moved from `tests/lifecycle_explorer.py` onto the engine. Adds a
clock-expiry action (attachment expiry), which makes the stood-down-retry
defect of `3ef7773ec` reachable; it becomes that rule's witness.

### Agent task (`tests/lifecycles/agent_task.py`)
- **Start:** the `_store` fixture of `tests/test_background.py` with one
  admitted queued task; no app boot.
- **Actions:** claim, settle(success | transport_lost | provider_auth |
  session_refused | paused), finish (`_task_settled`), member Pause, Resume,
  Retry, retry timer fire, refused fire, restart, member removal, project
  deletion, sign-in release, clock past attachment expiry.
- **Rules:** status moves follow `AGENT_TASK_TRANSITIONS`; Resume and Retry
  never rewrite the parent's status; at most one recovery child per parent;
  at most one active task per chat; the transport ladder never exceeds its
  limit across restarts; no timer stays pending once a child exists; a
  `pausing` row always settles (Goal).
- **Known targets:** Pause on a queued row with no worker sticks in `pausing`
  until restart (suspected); `interrupted → queued` and
  `paused → interrupted` rewrites in restore, transfer and episode reports
  contradict the transition table (suspected).

### Experiment episode with watchers (`tests/lifecycles/episode.py`)
- **Start:** the `_Loop` fixture of `tests/test_experiment_stop.py`: one
  running episode with a bound session and one watcher; local stage only.
- **Actions:** Stop, settle the turn, exit-turn with an ending signal, finish,
  watcher check (active | completed | degraded), poll-reconcile, report
  outcome, retire watcher, restart. The clock ticks on every action, because
  Stop and interruption are ordered by timestamp.
- **Rules:** invariant 10g's one graceful Stop fence; a Stop settles once no
  leaf is live; Stop skips the report; at most three report attempts;
  `wrapping_up` always ends (Goal); a claim takes only completed or degraded
  watchers not yet notified; a lone degraded watcher wakes once; Stop retires
  only the current episode's observations.
- **Known targets:** startup leaves a Stop recovery pending on a transient
  remote outage, then `settle_ready_experiment_loop_stops` abandons the
  interrupted turn anyway (suspected; needs `directory_exists` patched to
  raise, as a restart variant).

### Agent schedules (`tests/lifecycles/schedules.py`)
- **Start:** the `schedule_store` fixture of `tests/test_agent_schedules.py`.
  Database only.
- **Actions:** add (member turn | wake), pause, resume, cancel, enable, tick
  (+1, +15, +60 minutes), settle the wake, member sends, close thread,
  restore, restart.
- **Rules:** the spec's private-schedule rules S1 to S11 (gap, pending notes,
  one atomic claim, deferral keeps notes, closed thread fails the wake, lost
  access disables, missed fires count, paused never fires, restore disables,
  20 per thread, wakes are captured Work turns).
- **Known targets:** `missed_fires` is not capped by `--count`; restore skips
  paused rows; thread close keeps `next_fire_at` while dispatch clears it; a
  claim `ValueError` retries silently every tick (all suspected).

### Nightly consolidation (`tests/lifecycles/consolidation.py`)
- **Start:** the `runtime` fixture of `tests/test_consolidation_runtime.py`:
  one target due three days ago.
- **Actions:** poll, next night, settle the task (success | fail | pause),
  report, commit, archive the chat (route and legacy row), Keep, Dismiss,
  member turn in the chat, restart, restore.
- **Rules:** one occurrence per target per local date; a failure advances the
  occurrence, never coverage; settled outcomes are final; only Keep or
  Dismiss closes a row; an unsettled run's task never stays queued (Goal);
  a due target advances or holds an unsettled run (Goal); invariant 10g's
  target match.
- **Witnesses:** `b12897489` (0.4.35 `graph_target` refusal) and `09f7519f6`
  (0.4.38 archived chat).

### Proposals with approval cards (`tests/lifecycles/proposals.py`)
- **Start:** the `filed` fixture of `tests/test_proposal_refresh.py` plus one
  card from `real_card`.
- **Actions:** agent files, agent withdraws, a dependency moves, member
  approves or rejects through Sync, refresh, ask card, tap (approve | decline
  | old hash), outage tap, claim then restart, expire, close thread.
- **Rules:** invariant 3 (only humans resolve, agents only withdraw); status
  changes only from pending; Sync withdraws a stale Proposal without effects;
  a card is single-use and settles stale only when its pins change; an
  outage releases it; restart never reruns it; every pending card ends
  (Goal).
- **Known targets:** a tap's pin check outside admission can settle `failed`
  where the spec says `stale` (a race; reachable only if the tap is split
  into check and sync actions, which this module does).

## 4. Every other machine, written down

`tests/lifecycles/catalog.py` declares the 21 other stored machines from the
inventory (project provisioning, transfer, provider login, member removal,
invitations, device pairing, project invitation, Auto-research recovery and
children, episode isolation and merge, conversation worktree, branch merge,
compute job, paper changes, paper live, mirrored terminal, artifact, server
step journal, restore, project deletion). Each entry names its owner module,
its status field or `Literal`, its actions as dotted paths to the real writer
functions, and its rules as docstrings with sources. `explored=False`.

`test_catalog.py` checks the catalog against the code: every dotted path
imports, and every declared `Literal` matches its type's values. The catalog
is the place an explorer for one of them starts later.

## 5. Defects

When an explorer reports a failure, the builder:
1. Confirms it with a plain regression test built from the trace.
2. Fixes the root cause. Where code and spec disagree (the forbidden status
   rewrites), the fix follows the spec unless the spec is wrong; the commit
   says which and why, and the spec changes in the same commit if needed.
3. Keeps the trace as the rule's witness when no better one exists.

A suspected defect that the explorer cannot reach is written in the PR body
with why, not fixed blind.

## 6. Comment shrink

Already running: the test tree's comments, proven code-identical per file by
AST comparison. It lands first on this branch.

## Ownership, order, and checks

Shared and serial: `tests/lifecycles/engine.py`, `tests/conftest.py`,
`pyproject.toml`.

| Step | Owner files | Check |
|---|---|---|
| 1 | engine + handoff move + cheaper states (measured) | `uv run pytest -n0 tests/lifecycles/test_handoff.py` |
| 2 | `test_completeness.py` against handoff only | same plus the new file |
| 3a–3e | one machine module and its test each, plus its defect fixes | that machine's test plus the product tests its fixes touch |
| 4 | `catalog.py`, `test_catalog.py` | the catalog test |
| 5 | docstring links from specs, `docs/install.md` command, delete this file | `test_agent_instructions.py` |

Steps 1 and 2 are serial. Steps 3a–3e run in parallel, each in its own
worktree, after step 2. Step 4 runs in parallel with step 3. Defect fixes
that touch the same product module are serialized by the integrator.

## Risks

- **CI time.** Six machines at ≤ 20 s each adds about two minutes to one
  python shard. The shard planner balances files by measured time.
- **Coupling.** Explorers patch private engine seams; an engine refactor
  breaks them. That is intended: the refactor must keep the explorers green.
- **Atomic actions.** Races inside one function stay out of reach except
  where a module splits an action on purpose (the card tap).
- **Fix scope.** Seven suspected defects may grow the PR. Each fix is its own
  commit with its trace.
