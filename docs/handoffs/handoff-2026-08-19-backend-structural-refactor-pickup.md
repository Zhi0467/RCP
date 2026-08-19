# Handoff: backend structural refactor pickup after durable launch

- **Status:** Ready for discussion and continuation on `main`; the overall
  backend structural refactor is not complete.
- **Audited starting point:** `f6085b0` (`Land graph transitions and architecture
  contracts`).
- **Current code checkpoint:** `ed4c019` (`Launch only durably admitted tasks`).
- **Implementation span through `ed4c019`:** 58 commits after `f6085b0`,
  covering the corrected audit, Phases 0–6, and the first two Phase 7 durability
  slices. The documentation commit containing this new file follows that code
  checkpoint.
- **Intentional stopping point:** every task admission is durable and every
  production launch crosses the ID-only launch boundary; constructor/startup and
  episode-policy extraction have deliberately not started in this final slice.
- **The three documents and what each is for.** They are read together, and none
  of them supersedes the others:
  - [`handoff-2026-08-18-backend-structural-refactor.md`](handoff-2026-08-18-backend-structural-refactor.md)
    — **the work order.** The human-confirmed phase order and target architecture
    for Phases 0–7. Still the authority for *why* each phase exists. Archive it
    only when every definition-of-done item here is true.
  - [`rcp_architecture_audit.md`](rcp_architecture_audit.md) — **the evidence.**
    The measurements the work order was built from, with its `f6085b0` figures
    separated from the current tree, and its remediation items marked done,
    partial, or open. Appendix A explains each finding from scratch. It is *not* a
    work order and must never be implemented from directly.
  - **This file** — **the current execution state and the plan for what remains.**
    It records where the code actually stopped, every decision settled with the
    human, and a slice-by-slice implementation plan. Where this file and the work
    order disagree, this file is later and wins; where this file and the audit
    disagree, this file was measured against the current tree and wins.

  Behavioral truth lives in [`docs/design.md`](../design.md) and
  [`docs/specs/`](../specs/), never in any of these three. A handoff may refine an
  open implementation detail; it may not silently change a specification.

## Read this in order

1. [`AGENTS.md`](../../AGENTS.md), especially the authority invariants, explicit
   policy-boundary rule, fan-out guidance, and verification requirements.
2. [`docs/design.md`](../design.md), the applicable files under
   [`docs/specs/`](../specs/), and [`docs/open-questions.md`](../open-questions.md).
3. The [original work order](handoff-2026-08-18-backend-structural-refactor.md),
   especially the Phases 5–7 re-review ledger and Phase 7.
4. This pickup document.
5. The current source and tests. Do not implement from old line numbers in the
   audit.

## Two things this plan deliberately does not do

Both look like obvious cleanup. Both were measured, and the measurement said no.
An agent applying this plan's own rules mechanically will try to "finish" them,
so they are stated here, at the two code sites, and again inside the slices that
touch them.

1. **`_create_and_spawn` is not refactored.** Four `isinstance` branches in one
   211-line function is the most inviting target in the file. It is 5.3% of the
   class, 82% of it is universal row assembly, and its whole edit history shows
   no case of two owners colliding in it — which is the failure a split exists to
   prevent. Its Auto-research branch leaves as a *consequence* of C5, never as
   its own task. See decision 3.
2. **One engine call into Experiment policy stays.** `_run` calls
   `_record_bound_experiment_session_limit` after a provider failure deep inside
   a running worker. There is no caller to invert. Removing it means the engine
   emits a generic failure event that Experiment policy subscribes to — a
   dispatch registry under another name, forbidden by the settled decisions. The
   method still moves to its Experiment module in C4; the call follows it there
   as a plain import and stays a plain call. See the wrong-way-call table.

Neither is a loose end, and neither is deferred work. If either starts to look
necessary, that is a stop-and-ask, not a judgement call.

## Suggested skills

- Use `grill-me` first to discuss the bounded decisions in this document with
  the human. The purpose is to close recovery presentation and slice order, not
  to reopen the settled architecture.
- No implementation skill is required. Follow the repository's normal
  contract-first, file/module-scoped subagent workflow after the discussion.

## Executive state

| Workstream | State at `ed4c019` | What that means |
| --- | --- | --- |
| Audit correction | Complete | The bad extracted-copy findings were corrected against the real checkout; the audit is historical evidence, not a work order. |
| Phase 0 safety net | Complete | Route inventory, lifecycle transition matrix, and one-time coverage inventory are committed. |
| Phase 1 task lifecycle | Complete | Legal status changes and guarded side effects share one private transactional seam; refusals are truthful. |
| Phase 2 project snapshots | Complete | An incomplete graph-only snapshot cannot reach an API response without the single completion boundary. |
| Phase 3 transaction audit | Complete | Four proven harmful partial-write windows were fixed; broad `AppStore` splitting was rejected. |
| Phase 4 | Complete as scoped | The byte-identical result-view ID validator moved; the watcher-prose abstraction was rejected. |
| Phase 5 API routes | Complete | `api/app.py` has no route handler body; focused routers use narrow dependencies through `ApiServices`. |
| Phase 6 task ownership | Complete | One-invocation executors live under `runs/tasks/`; ordinary Work, Auto-research child Work, and Experiment-loop policy are explicit. |
| Phase 7 durable admission | Complete | Every task insertion atomically records one strict `operation_admitted` launch intent. |
| Phase 7 durable launch | Complete | Every production post-admission launch calls `launch_admitted(operation_id)`; malformed or ambiguous evidence fails closed. |
| Remaining Phase 7 | Not implemented | Constructor/startup reconciliation, common episode ownership, ID-only settlement/recovery routing, authority relocation, and remaining policy extraction. |
| Frontend/final product verification | Not done after this backend refactor | The web baseline passed during audit fact-checking, but no final web build/test or browser drive was performed at `ed4c019`. |

## What this goal session accomplished

### Audit correction and safe baseline

The session began by fact-checking the architecture audit against the real
repository rather than trusting the incomplete extracted copy. It corrected the
false missing-file claims, reproduced the route, closure, lifecycle, store,
dependency, and file-size measurements that could be retained, and demoted
unreproducible classifier totals to historical evidence.

The safety net then froze the actual route entries, built the full lifecycle
operation/source-status matrix, and recorded coverage before structural moves.
The route inventory distinguishes route entries from expanded method/path pairs,
and the lifecycle tests cover status, events, receipts, retained Patch output,
and lifecycle notices rather than checking only the status column.

Key commits:

- `d934b76` — fact-check the architecture audit;
- `09a563c` — freeze the backend route inventory;
- `3673c04` — centralize lifecycle transitions;
- `372b294` — record the structural-refactor coverage baseline; and
- `2d89390` — make the shared project membership gate extractable.

### Phases 1–4 correctness work

Phase 1 replaced per-method guarded status updates with one private
status-observe-and-update path. A refused single-task transition now preserves
all guarded state and writes only its truthful refusal event; missing IDs fail
loudly and write nothing. The existing S10 acceptance document and current
project/operations specification were updated.

Phase 2 replaced serializable half-built project snapshots with an opaque
graph-only draft plus one completion boundary for fresh and saved snapshots.
This concerns backend project reads, not cache-management UI.

Phase 3 inspected multi-call mutating sequences after the lifecycle fix. Four,
and only four, met the named crash-window gate:

1. ordinary Auto-research ending;
2. Auto-research Stop ending;
3. initial graph-repair admission; and
4. Experiment-loop Patch/watcher/session handoff.

Those cases received narrow compound store operations and failure-injection
coverage. Watcher admissions/claims already used compound methods; broad store
repository splitting was not justified.

Phase 4 moved only the proven byte-identical result-view ID validator. No helper
sweep or watcher-renderer abstraction was added.

Key commits:

- `c89ad5e` — centralize result-view ID validation;
- `8dd3ce6` — make project snapshot completion unavoidable;
- `3b7d976` — make Auto-research ending fences atomic;
- `a310205` — make graph-repair admission atomic;
- `15824c5`, `849731e`, `fc30941` — make the Experiment handoff atomic and
  retry-safe.

### Phase 5: route extraction

The session introduced one typed `ApiServices` composition container and narrow
dependency accessors, then moved route groups by cohesive API ownership. No
business-service wrapper, duplicate lock, optional fallback, or dynamic state
mirror was added. `src/rcp/api/app.py` is now 1,221 lines and contains
composition, lifespan, middleware, shared streaming dispatch, and callback
wiring—not route handler bodies.

Implemented router commits:

- `5c4e512` — paper;
- `603c18d` — chats;
- `15aa985` — history;
- `f8ae6fe` — ordinary watcher API;
- `305ec5e` — result views;
- `f87a18e` — coherent transition projection completion;
- `62ee427` — graph Sync and preview;
- `e58a9d2` — task API;
- `61a8575` — Experiment API;
- `a1f9f2f` — episode API;
- `b262a38` — identity/team API;
- `02187b3` — project index/global API;
- `8831e14` — remaining project-state API; and
- `e777417` — health, completing the zero-route-body target.

The original handoff contains each route group's exact contracts, moved tests,
dependency seam, and checkpoint verification. Do not duplicate those routes or
reintroduce closure access from an extracted module.

### Phase 6: one-invocation task ownership

The old `runs/work.py` path is gone. Ordinary Work now lives at
`runs/tasks/work.py`; result-view, Auto-research-child, and Experiment-loop task
policy have explicit owners. Discuss, Coach, Seed/Refresh Graph, branch-merge,
episode-report, and Auto-research-stream executors were mechanically rehomed.

The important semantic result is not reduced line count. Auto-research child
Work and Experiment-loop each keep their own prompt, continuation, watcher,
Patch, repair, and handoff algorithms instead of hiding policy behind a
`patch_kind`, `surface`, `kind`, or callback registry. Some duplication is the
deliberate cost of visible policy.

Key commits:

- `5e8d991` — result-view task policy;
- `258b9de` — Experiment watcher maintenance;
- `e8b9b74` — mechanical Work package move;
- `c0f6f68`, `1106f8a`, `b1c52f3`, `612aa76`, `ae32db2`, `e744da5` — Discuss,
  Coach, Graph, branch-merge, report, and Auto-research-stream task rehomes;
- `32bcda8` — explicit Auto-research child Work owner; and
- `ffe7c5c` — explicit Experiment-loop invocation owner.

Two reviews materially improved these splits. Child Work now closes its
validator mailbox on pre-launch prompt failure. The Experiment/Work split no
longer forwards Experiment policy arguments through nominally shared validator
or Apply plumbing.

## Phase 7 work completed in this session

### Durable admission contract — `e84b461`

Every task insertion now commits a reserved `operation_admitted` summary receipt
in the same transaction as the task. It records only launch data not already in
the row:

- exact task kind;
- exact attempt number;
- nullable exact parent operation ID;
- exact continuation cause; and
- `admission_committed=true`.

It does not add a table, a `graph_runs` column, a second request copy, or a new
`AgentTaskRecord` field. The request, episode, graph target, authorizer,
authority, session, and stage remain on the existing row.

`agent_task_admission_intent` strictly validates the payload's exact keys and
types, uniqueness, allowed continuation, and equality with the task row. It
retains one narrow reader fallback for the old Experiment
`operation_created + admission_committed=true` shape and rejects duplicate
legacy evidence.

Admission, dispatch-attempt, pre-start-failure, and dispatch-start receipts are
permanently retained. Retention therefore cannot erase the evidence needed to
distinguish a safe launch from an unknown prior start.

### ID-only launch boundary — `ed4c019`

`BackgroundAgentTasks.launch_admitted(operation_id)` is the only production
caller of `_spawn_record`. All previous post-admission paths now pass only the
durable operation ID.

For a queued task it reloads and validates:

1. the persisted record;
2. the request parsed by exact task kind and an exact JSON roundtrip;
3. the durable admission intent and continuation cause;
4. exact parent ID/presence and the persisted parent record;
5. the human authorizer snapshot;
6. dispatch authority—required for every non-report task and forbidden for a
   hidden episode report;
7. request/row episode identity, persisted episode project, and graph target;
8. native session and execution-stage internal consistency;
9. the absence of an impossible prelaunch write-scope fingerprint; and
10. durable proof that no earlier dispatch started.

It deliberately does not infer continuation-specific session/stage rules,
parent kind, parent episode relationships, or mode policy. Those belong to the
admission owner. The neutral cross-record invariants are exact identity,
presence, project, and graph target.

Outcomes are fail-closed and idempotent:

- a missing ID raises `KeyError` and writes nothing;
- an in-process duplicate returns freshly loaded durable state;
- a task already beyond `queued` returns its current state;
- a valid newly admitted queued task with no attempt is safe to launch;
- a legacy admission with no retained attempt proof is ambiguous and refuses;
- a retained matching `operation_dispatch_failed_before_start` makes that exact
  attempt retryable; and
- malformed, mismatched, missing, unknown, or already-started evidence refuses
  before another worker or receipt is created.

`_spawn_record` claims the operation in `_workers` while holding
`_controls_lock`, then reloads and compares all launch-critical immutable fields
inside that lock. This order prevents a concurrent duplicate from rejecting a
legitimate live task merely because the live worker already checkpointed its
native session or stage.

Dispatch evidence is ordered:

```text
in-process registry claim
  -> operation_dispatch_attempt
  -> operation_created
  -> Thread.start()
  -> operation_dispatch_started
```

An exception before `Thread.start()` records
`operation_dispatch_failed_before_start` for the same attempt and releases the
registry claim. A later launch is allowed only when that exact durable proof is
still coherent.

### Review corrections made before `ed4c019`

The main agent reviewed the worker implementation and corrected all of these
before committing:

- removed a silent Auto-research reconciliation `try/except`; launch integrity
  failures must remain loud;
- removed a generic attempt-number rule that rejected valid child message wakes;
- removed episode-authorizer equality, because a current human can legitimately
  authorize branch merge after a different human authorized the episode;
- allowed the existing empty-string local `stage_host` when an exact stage root
  exists;
- removed continuation-specific session/stage inference from the neutral engine
  after a Seed Resume regression proved that policy did not belong there;
- updated a stale Experiment-index fixture to persist its required dispatch
  authority rather than weakening production validation;
- hardened malformed/non-object/null/empty dispatch IDs and legacy no-attempt
  ambiguity in storage;
- moved the live-worker check ahead of immutable reload to close the
  native-session/stage time-of-check race; and
- withdrew a proposed generic parent-kind/episode rule after checking valid Auto
  child Work, child Experiment, and report lineages. Mode owners retain those
  contracts.

The final independent read-only review returned PASS with no Critical, High,
Medium, or Low finding. Its only non-blocking note was that no dedicated
two-thread barrier test calls public `launch_admitted` simultaneously; existing
concurrent reconciliation and live-duplicate tests cover the same registry claim
and checkpoint race indirectly.

## Verification evidence

### Final `ed4c019` launch slice

All of the following passed before the code commit:

- `uv run pytest -q tests/test_background.py`
- `uv run pytest -q tests/test_dispatch_authority.py`
- `uv run pytest -q tests/test_storage.py tests/test_background.py tests/test_experiment_index.py tests/test_dispatch_authority.py`
- the focused Auto-research, Experiment, watcher, result-view, branch-merge,
  episode-report, paper, ingestion, API, lifecycle, and recovery sets recorded in
  the original handoff ledger;
- `uv run pytest -q`
- `uv run ruff check src tests`
- exact-file pre-commit over the five launch-slice files;
- `uv run pre-commit run --all-files`
- `git diff --check`; and
- `git ls-files --others --exclude-standard`, with no unaccounted path.

The first full backend run exposed three real integration failures in the new
slice: an overstrict Seed Resume stage rule and two stale Experiment-index
fixtures without authority. They were corrected; the exact failures and the
confirmation full run passed. There was no suppression, fallback, or expected
failure marker.

### Earlier session evidence

Every logical implementation chunk ran its focused tests, full backend suite,
Ruff, exact-new-file hooks where applicable, and all-file hooks before its
checkpoint commit. The original handoff ledger is the detailed per-slice record.

At the audit baseline, `npm --prefix web run build` passed and
`npm --prefix web test` reported 419 passing tests. Those web checks were not
rerun at `ed4c019`. No browser or desktop interaction was driven after the
backend-only refactor; do not describe the final product journey as verified.

The final scenario-status sweep found four `pending` and two
`blocked-external` files. None became runnable or wrong because of this backend
slice:

- S41 is the only directly affected scenario. Its listed backend pytest coverage
  passed, but its real-provider, failure, watcher-wake, and served-browser drive
  remains outstanding; its `last_checked` note was updated and it stays pending.
- S32's native artifact window/download/isolation drive remains pending. This
  session touched only the result-view ID validator in `artifacts.py`, not the
  desktop preview boundary.
- S121's distinct terminal `refused` task state and explanation UI were not
  implemented; the Phase 1 lifecycle-refusal fix is a different contract.
- S90 live desktop dictation was untouched and remains pending.
- S35 still requires a signed bundle, clean macOS account, reachable SSH host,
  and installed provider; S36 still requires a built bundle and published update
  manifest. Both remain blocked externally.

Implemented scenarios were not rerun merely for the calendar, consistent with
`AGENTS.md`.

### Documentation wrap-up checks

The final focused documentation run passed 8/8. The exact-file hook's first pass
only added the required final newline to this new file; its rerun passed. The
all-file hooks and `git diff --check` passed. The untracked-path inventory named
only this pickup file, which is intentionally included in the documentation
commit.

## Documentation updated while closing the session

- The original work order now records `ed4c019`, distinguishes the old
  24-method ownership inventory from a final method-count target, lists the
  unimplemented remainder, and links here.
- Its stale paper-router hash was corrected from `a310205` (the graph-repair
  atomicity commit) to the actual paper extraction `5c4e512`.
- The architecture audit now separates its `f6085b0` measurements from the
  current `ed4c019` tree and marks the remediation items done, partial, or still
  open.
- `docs/handoffs/README.md` now exposes both the original work order and this
  current pickup.
- S41's `last_checked` note now records the affected backend coverage and the
  still-outstanding provider/browser drive; the other five pending or
  blocked-external scenarios were inspected and remain unchanged for explicit
  reasons in the verification section above.
- The current behavioral specifications were rechecked. Phase 1 had already
  updated `projects-spaces-and-operations.md`; the conversations/episodes and
  API projection specs describe behavior rather than Python file locations, so
  this structural checkpoint does not justify semantic edits to them.

## What was deliberately not done

No part of this list is implied complete by `ed4c019`. It is the record of that
checkpoint and is deliberately not rewritten as later slices land — read the
slice ledger below for what has since been done:

- `BackgroundAgentTasks.__init__` is not side-effect-free. It still proves
  committed/reserved Auto-research work, interrupts other active tasks, restarts
  stopping Experiment recoveries, settles ready Stops, and restarts interrupted
  reports during construction.
- Startup ownership has not moved. `api/app.py` still carries the long lifespan
  reconciliation sequence, and the constructor still mutates storage before
  lifespan starts.
- `EpisodeReconciler` still lives at `runs/episode_reconcile.py`; it has not moved
  to `runs/episodes/reconcile.py`, and its settlement entry point is not yet the
  single ID-only engine notification.
- `runs/episode_wrapup.py` has not moved to `runs/episodes/wrapup.py`.
  `start_episode_report` and `_restart_interrupted_episode_reports` remain on the
  background class.
- Task API Resume/Retry still calls `BackgroundAgentTasks.resume/retry` directly.
  Episode-linked recovery has not been routed through `EpisodeReconciler`.
- `_resolved_dispatch_authority` remains on `BackgroundAgentTasks` because
  admission still lives there. It has not moved to `runs/task_policy.py`.
- The separate generic and Auto-research settlement callbacks remain wired in
  `api/app.py`; one ID-only task-settled callback has not replaced them.
- Auto-research admission/recovery/Stop/children, Experiment parent recovery,
  ordinary and episode watcher admission, and branch-merge admission have not
  moved out of `background.py`.
- No `runs/episodes/` package has been created yet.
- No new task runtime, policy registry, callback registry, mixin hierarchy,
  fallback launch path, second launch table, new durable status, or continuously
  running “episode manager task” was added.
- No frontend source was changed in this slice.
- No final web build/test, served-app check, browser drive, desktop check, or
  remote/SSH execution was performed at this checkpoint.
- The original handoff was not archived because the work is still active.

## Current code landmarks for the next agent

- `src/rcp/background.py:261` — constructor. Assigns fields only since Slice A;
  `recover_at_startup()` immediately below it owns what it used to do.
- `src/rcp/background.py:3446` — `launch_admitted`.
- `src/rcp/background.py:3582` — private `_spawn_record`, now reachable only
  through `launch_admitted` in production.
- `src/rcp/storage/agent_tasks.py:1580` — strict admission-intent reader.
- `src/rcp/storage/agent_tasks.py:2659` — fail-closed dispatch no-start proof.
- `src/rcp/api/app.py:833` — current lifespan reconciliation sequence.
- `src/rcp/api/app.py:679` — current task-settlement callback wiring.
- `src/rcp/runs/episodes/reconcile.py` — the coordinator, reused and moved in
  Slice B, never replaced.
- `src/rcp/runs/episodes/wrapup.py` — common wrap-up/report admission policy.
- `src/rcp/runs/episodes/report.py` — report admission, moved off the engine in
  Slice B. It reaches into the engine's private registry on purpose.
- `src/rcp/api/tasks.py:315` and `:410` — current Resume and Retry route entry
  points that still dispatch directly to the background object.

Line numbers are an `ed4c019` navigation aid, not a contract. Search by symbol
after any edit.

## Decisions settled with the human on 2026-08-19

Every decision below was taken against measurements of the real checkout and the
human's own app data, not against the prose in this file. The measurements are in
"Verification against real records" below. Where a measurement contradicted the
recommendation this file previously carried, the measurement won and the change
is noted.

### 1. Reading a request RCP itself persisted — settled: tolerate dropped fields

**Decided:** when RCP reads back a task request it wrote earlier, it drops keys
this build no longer declares, logs each drop, and validates everything else
exactly as strictly as before. Live callers stay strict: a request arriving from
outside still cannot smuggle an unknown field past validation.

**Why:** a stored request is not untrusted input; it is RCP's own record of what
it already did. Strictness exists to catch a bad agent, not to make RCP unable to
read its own past.

**Evidence:** every stored `auto_research` request in the human's database carried
an `ending` key that `AutoResearchRunRequest` dropped on 2026-08-15 in `885fa3a`,
which predates this refactor. Five failed Auto-research tasks offered a Retry
button whose only possible outcome was `HTTP 409` carrying a raw pydantic dump and
an `errors.pydantic.dev` URL. Landed with `load_stored_request` in
[task_policy.py](../../src/rcp/runs/task_policy.py).

### 2. A legacy Experiment turn with no recorded authorizer — settled: say what to do

**Decided:** refusing is correct and stays. The episode's own human is the
authority for every turn in it, so a current human pressing Retry cannot stand in.
The message now names the situation and the way out, matching the sibling message
already in that file.

**Evidence:** eight real failed Experiment turns answered "A patch-capable agent
task requires a human authorizer snapshot" — true and useless.

**Checked and dismissed — the Experiment deadlock the human raised.** Auto-research
can always start a new episode; an Experiment's control node looked like it might
be the only door. It is not. `run_experiment` always mints a fresh `episode_id`,
and readiness is blocked only by open graph gates or a genuinely live loop. All
13 real Experiment nodes were measured: 10 are runnable, including every node that
owns a task refusing Retry; the 3 blocked ones are blocked by undecided Decisions
and open Blockers. The one unrunnable case, `exp/theta0-four-reference-probes`,
no longer exists in any project graph, so it has no UI entry point to deadlock.

### 3. `_create_and_spawn` — settled: leave it bundled, do not refactor it

**Decided:** the four admission owners move out of `background.py` and keep
calling the shared creation function. It is not restructured. When Auto-research
admission moves out it inserts its own row, so its 27 lines and both
Auto-research-only parameters leave with it as a consequence of that move rather
than as separate work. Experiment's 9 lines and the `episode_id` ternary stay.
The function's own docstring now carries this reason, so it outlives this file.

**This reverses the recommendation originally made in this session.** That
recommendation was based on how the function reads — four `isinstance` branches —
not on what it costs. The measurements:

| | |
| --- | ---: |
| Moves out regardless of this decision | 2,245 lines, 39 of 72 methods (**56%**) |
| `_create_and_spawn` itself | 211 lines (**5.3%** of the class) |
| — of which universal row assembly | 173 lines (82%) |
| — of which Auto-research-specific | 27 lines (13%) |
| — of which Experiment-specific | 9 lines (4%) |
| Callers passing the two surface-specific parameters | 1 (`start_auto_research_turn`, itself moving out) |

Its edit history shows no maintenance problem: 12 edits since the repository
began, every one inside a large landmark commit touching 49–202 files. Not one
was a small surface-local change that had to reach into it. There is no record of
two owners colliding here, which is the failure the extraction exists to prevent.

### 4. Constructor purity and a `runs/episodes/` package — settled: both, in this order

**Decided:** two separate commits.

1. **Constructor purity.** `BackgroundAgentTasks.__init__` assigns fields only.
   Its four recovery side effects move to one explicit public method the lifespan
   calls. The methods stay on the class; only the call becomes explicit. Two files.
2. **The episodes package.** Relocate `episode_reconcile.py` (323 lines) and
   `episode_wrapup.py` (259) under `runs/episodes/`, move `start_episode_report`
   and `_restart_interrupted_episode_reports` (33) off the class, collapse the two
   settlement callbacks into one that passes only an operation id, and route
   episode-linked Resume/Retry through `EpisodeReconciler`.

**Why separate:** landing them together produces one diff in which a behavior
change and a ~615-line file move are indistinguishable in review, which is exactly
where a real change hides inside a rename.

**Correcting this file's earlier claim.** Step 2 of the plan previously recorded
here asserted the constructor's code needs a destination before `__init__` can be
made pure. It does not. The four calls stay where they are and are simply called
explicitly. The two changes are independent in both directions.

**A caution about the package move.** `start_episode_report` reaches into the
engine's in-process worker registry (`self._workers`, `self._controls_lock`) and
its launch gate (`self.launch_admitted`). Relocating it does not decouple it — it
would still need the background object handed in, exactly as `EpisodeReconciler`
already does. The gain is a legible address symmetric with `runs/tasks/`, not
reduced coupling. Worth doing, worth not overselling.

**What does not motivate this work:** startup is **0.50s** on the human's real
data (0.21s construction, 0.29s lifespan) and ran clean over 252 tasks, 25
episodes, and 4 projects with zero warnings. The motivation is solely that
constructing an object writes to a database, which **358 sites** currently do —
130 direct constructions in tests and 228 through `create_app`.

### 5. Proving recovery was not silently dropped — settled: explicit calls, measured first

**Decided:** every test that needs recovery calls the new method itself. No test
helper bundles construction with recovery, because a helper that always recovers
rebuilds the same implicit coupling one layer down. No `recover=` flag, which
would be a behavior selector on a shared constructor.

**Before changing anything,** instrument the four side effects to record when they
actually do work and run the full suite. That yields the exact list of tests whose
constructor really performed recovery, as distinct from the ones that merely
construct the object. A green suite after the change is not evidence on its own —
a test that silently stopped exercising recovery still passes. The instrumented
list is the checklist.

**Measurement taken 2026-08-19, corrected on re-measurement.** Of the 358
construction sites, exactly **14 tests** have a constructor that changes the
store. The first pass reported 10 and under-counted; the corrected run hashes the
sqlite file around the real `__init__` for every test in the suite, and the four
it had missed are marked **(missed)** below. Every other site constructs the
object over a store with nothing to recover, so moving recovery out is invisible to
them. This is the complete checklist for the constructor slice; each name must end
up either calling the new method explicitly or demonstrably not needing it:

| File | Tests |
| --- | ---: |
| `tests/test_experiment_stop.py` | 4 (1 missed) |
| `tests/test_auto_research_stream.py` | 3 |
| `tests/test_background.py` | 3 (2 missed) |
| `tests/test_auto_research_experiments.py` | 1 |
| `tests/test_episode_lifecycle_acceptance.py` | 1 |
| `tests/test_acceptance_experiment_watchers.py` | 1 (missed) |

The measurement makes the explicit-call decision clearly correct — a test helper
was proposed to avoid churn concentrated in `test_background.py`, and that churn
turns out to be three tests, one of which needs no call at all.

### 6. The legacy ambiguous queued admission — settled: the case is currently empty

**Decided:** the recommendation this file previously carried stands — the generic
startup interruption path marks such a row interrupted with a durable diagnostic
and preserves its task, receipts, Patch text, and retry history. No new status, no
quarantine, no guessing.

**But its urgency was overstated.** The human's database has **0 queued tasks**.
All 252 of their tasks predate the durable admission receipt, so any of them would
be ambiguous *if* queued — none is, and the set only shrinks as new tasks carry the
receipt. This does not need to be settled before the constructor slice.

### 7. A dedicated simultaneous-launch barrier test — unchanged: not now

The recommendation already in this file stands. Add one deterministic two-thread
barrier test only if the startup slice creates another concurrent caller or changes
lock ordering.

### 8. When dispatch-authority resolution moves — unchanged: before its consumers

`_resolved_dispatch_authority` (144 lines) moves mechanically to
`runs/task_policy.py` as a small serial contract slice immediately before the first
admission owner leaves `background.py`.

### 9. The old "24 engine methods" count — unchanged: not a target

Settled previously and not reopened. The definition of done is absence of surface
policy, not a method count.

## Implementation plan for the remaining work

Written to be executed without reopening any decision above. Every method name,
file path, and test name below was read from the tree on 2026-08-19; verify by
symbol search rather than by line number, which will drift.

### The shape of what is left

`BackgroundAgentTasks` is 3,989 lines across 72 methods. Classified by whether the
code serves every job type or one particular kind of job:

| | methods | lines | |
| --- | ---: | ---: | --- |
| **General engine** | 31 | 1,711 | 43% — stays |
| Auto-research | 32 | 1,705 | 43% |
| Experiment loop | 4 | 326 | 8% |
| Watcher | 2 | 141 | 4% |
| Branch merge | 1 | 73 | 2% |
| Episode report | 2 | 33 | 1% |
| **Moves out** | **41** | **2,278** | **57%** |

**The five job-specific groups make zero calls to each other.** Each is a
self-contained move and they may be done in any order. The only thing coupling
them to anything is the engine.

Note the classification correction: the four `_validate_existing_child_experiment_*`
methods (185 lines) read `store.auto_research_child_experiment(...)` and are called
only from Auto-research code. They belong to **Auto-research**, not to the
Experiment loop, despite their names. A name-based grouping gets this wrong.

### The twelve wrong-way calls

Twelve places have general engine code calling into job-specific code. These are
the only real obstacles; everything else points the safe direction (job-specific
code calling the engine, 69 calls, which become ordinary imports).

| Engine method | calls into | count | Resolved by |
| --- | --- | ---: | --- |
| `__init__` | the four recovery helpers | 4 | Slice A |
| `_create_and_spawn` | `_auto_research_for_request`, `ensure_auto_research_wake_spawned`, `_auto_research_admission_exhausted` | 3 | Slice C5 — Auto-research inserts its own row |
| `retry` | `_retry_auto_research_task`, `_retry_experiment_loop`, `_preflight_experiment_episode_recovery` | 3 | Slice D — invert |
| `resume` | `_preflight_experiment_episode_recovery` | 1 | Slice D — invert |
| `_run` | `_record_bound_experiment_session_limit` | 1 | **Kept.** Documented exception |

**The kept exception, and why.** `_run` is the worker-thread body. Its one call
records that a session-bound Experiment episode hit a provider session limit, and
it fires after a provider failure deep inside a running job. There is no caller to
invert. Removing it would require the engine to emit a generic failure event that
Experiment policy subscribes to — a dispatch registry under another name, which
the settled decisions forbid and which makes the behaviour harder to follow. One
honest, named, documented call is the better trade. The reason is now recorded
at the call site in [background.py](../../src/rcp/background.py) as well as
here, so it survives this file being archived. Do not "fix" this later without
reopening the decision.

### Slice A — make construction stop writing to the database

**Change.** `BackgroundAgentTasks.__init__` assigns fields only. Its four recovery
side effects move verbatim into one new public method on the same class:

```python
def recover_at_startup(self) -> None:
    """Reconcile work the previous process left behind. Called once, by the lifespan.

    Kept on this class as a waypoint: when the job-specific owners move out
    (Slice C) this becomes startup orchestration that calls each owner, and the
    four wrong-way calls disappear with it.
    """
    preserved_dispatches = self._proven_committed_auto_research_dispatches()
    reserved_roots = self._proven_reserved_auto_research_roots()
    self.store.interrupt_active_agent_tasks(
        preserve_operation_ids={
            *[item.operation_id for item in preserved_dispatches],
            *[task.operation_id for _episode, task, _request in reserved_roots],
        }
    )
    self._restart_stopping_experiment_recoveries()
    self.store.settle_ready_experiment_loop_stops()
    self._restart_interrupted_episode_reports()
```

**Keep `self._accepting_watcher_deliveries = True` in `__init__`.** It is in-process
state, not a storage write, and it is already True during recovery today. Moving it
would change behaviour.

**Placement.** `recover_at_startup()` is the **first statement** in the `lifespan`
body in [api/app.py](../../src/rcp/api/app.py), before `accept_watcher_notifications()`
and before `prune_operational_storage()`. This reproduces today's order exactly:
construction currently runs before every lifespan statement. Two orderings matter
and both are preserved by putting it first —

- interruption must precede `reconcile_committed_auto_research_dispatches`, or
  tasks that were just relaunched get interrupted;
- interruption must precede `prune_operational_storage`, because pruning skips
  tasks still marked active, and today interruption has already happened.

**Errors stay loud.** If `recover_at_startup()` raises, startup fails, exactly as a
constructor exception fails `create_app` today. Do not wrap it in `try/except`.

**The test checklist.** Of 358 construction sites, exactly these **14** have a
constructor that changes the store. Each must end up calling `recover_at_startup()`
explicitly, or be shown not to need it. No other site is affected.

```text
explicit recover_at_startup() call added
  test_auto_research_experiments.py::test_restart_recovers_the_stopped_predecessor_before_starting_its_replacement
  test_auto_research_stream.py::test_orchestrator_clean_retry_binds_replacement_session_in_production_stream[pre-stage]
  test_auto_research_stream.py::test_orchestrator_clean_retry_binds_replacement_session_in_production_stream[saved-stage]
  test_auto_research_stream.py::test_orchestrator_clean_retry_binds_replacement_session_in_production_stream[session-limit]
  test_background.py::test_interrupted_hidden_report_restarts_once_and_runner_owns_success        (missed by the first measurement)
  test_background.py::test_report_runner_terminal_error_is_not_generically_retried_or_resettled   (missed by the first measurement)
  test_experiment_stop.py::test_restart_keeps_stop_recovery_pending_when_remote_stage_probe_is_uncertain
  test_experiment_stop.py::test_restart_recovers_a_healthy_authorized_turn_behind_the_stop_fence[failed-retry]
  test_experiment_stop.py::test_restart_recovers_a_healthy_authorized_turn_behind_the_stop_fence[paused-resume]
  test_experiment_stop.py::test_restart_recovers_a_healthy_authorized_turn_behind_the_stop_fence[running-resume]
  test_experiment_stop.py::test_restart_settles_an_already_stuck_legacy_recovery_and_enables_fresh_run  (missed by the first measurement)

assertion moved inside the lifespan instead
  test_episode_lifecycle_acceptance.py::test_acceptance_episode_restart_retry_reuses_the_successful_spawn

recovers through create_app's lifespan, unchanged
  test_acceptance_experiment_watchers.py::test_s41_ceiling_pauses_then_human_run_starts_a_new_episode_and_exits  (missed by the first measurement)

shown not to need recovery, unchanged
  test_background.py::test_validated_spawn_record_rejects_both_parent_presence_directions
```

The one "shown not to need it" case monkeypatches `store.agent_task` before
calling `_validated_spawn_record`, so the record it validates never comes from
the database the constructor used to interrupt.

**Do not use a test helper that constructs and recovers together.** It would rebuild
the same implicit coupling one layer down. **Do not add a `recover=` constructor
flag** — a behaviour selector on a shared constructor is forbidden by the invariants.

**Add one test:** constructing `BackgroundAgentTasks` over a store holding an active
task leaves that task active until `recover_at_startup()` is called.

**Verification.** `uv run pytest -q` (a green suite alone is not evidence here — the
10 names above are), then Ruff, exact-file hooks, `pre-commit run --all-files`.

### Slice B — the `runs/episodes/` package

Pure relocation. Land it only after Slice A is committed, so a reviewer can see a
file move as a file move.

| From | To | lines |
| --- | --- | ---: |
| `src/rcp/runs/episode_reconcile.py` | `src/rcp/runs/episodes/reconcile.py` | 323 |
| `src/rcp/runs/episode_wrapup.py` | `src/rcp/runs/episodes/wrapup.py` | 259 |
| `BackgroundAgentTasks.start_episode_report` | `src/rcp/runs/episodes/report.py` | 29 |
| `BackgroundAgentTasks._restart_interrupted_episode_reports` | same | 4 |

Then collapse the two settlement callbacks wired at
[api/app.py](../../src/rcp/api/app.py) (`on_task_settled` and
`on_auto_research_task_settled`) into one that passes only an operation id and
reloads durable state, and route episode-linked Resume/Retry through
`EpisodeReconciler`.

**Known and accepted:** `start_episode_report` reads `self._workers`,
`self._controls_lock`, `self._require_operation`, and `self.launch_admitted`.
Relocating it does not reduce that coupling — it will take the background object
as an argument, exactly as `EpisodeReconciler` already does. The gain is an address
symmetric with `runs/tasks/`. Do not attempt to sever the coupling as part of this.

### Slice C — move the five job-specific groups out

Any order is safe (no calls between them). This order is cheapest-first, so the
pattern is set on a one-method move before the 1,705-line one:

| # | Group | Methods | Target | lines |
| --- | --- | --- | --- | ---: |
| C1 | Branch merge | `start_branch_merge` | `src/rcp/runs/branch_merge_admission.py` | 73 |
| C2 | Watcher | `start_watcher_notification`, `accept_watcher_notifications` | `src/rcp/runs/watcher_admission.py` | 141 |
| C3 | Episode report | folded into Slice B | `runs/episodes/report.py` | 33 |
| C4 | Experiment loop | `_retry_experiment_loop`, `_restart_stopping_experiment_recoveries`, `_preflight_experiment_episode_recovery`, `_record_bound_experiment_session_limit` | `src/rcp/runs/experiment_recovery.py` | 326 |
| C5 | Auto-research | the 32 listed below | `src/rcp/runs/auto_research_admission.py` | 1,705 |

**C4 moves the method that `_run` calls, and keeps the call.**
`_record_bound_experiment_session_limit` relocates with the other three
Experiment methods. `_run` then calls it at its new address through a plain
import — the method moving out is the point, the call surviving is the
documented exception. Do not leave the method behind on the class to avoid the
import, and do not replace the call with an event or a callback.

**C5 also removes `_create_and_spawn`'s three wrong-way calls.** Auto-research
admission inserts its own task row rather than passing
`auto_research_mail_delivery` and `auto_research_wake_admission` down into the
shared creation function. Both parameters and their 27 lines leave with it.
`start_auto_research_turn` is the only caller that passes them.
**`_create_and_spawn` is not otherwise restructured** — see decision 3.

The 32 Auto-research methods: `start_auto_research`, `reserve_auto_research`,
`reconcile_reserved_auto_research_roots`, `_proven_reserved_auto_research_roots`,
`_fail_reserved_auto_research_root`, `start_auto_research_turn`,
`ensure_auto_research_wake_spawned`, `reconcile_committed_auto_research_dispatches`,
`_proven_committed_auto_research_dispatches`, `start_auto_research_child_work`,
`ensure_auto_research_child_work_spawned`, `auto_research_child_work_task`,
`start_auto_research_child_work_message_wake`, `pause_auto_research_child_work`,
`stop_auto_research_child_work`, `resume_auto_research_child_work`,
`start_auto_research_child_experiment`,
`ensure_auto_research_child_experiment_spawned`,
`resume_auto_research_child_experiment`, `stop_auto_research`,
`pending_auto_research_mail`, `_retry_auto_research_task`,
`pause_auto_research_worker`, `_auto_research_for_request`,
`_auto_research_parent_episode`, `_auto_research_parent`,
`_auto_research_admission_exhausted`, `_validate_existing_auto_research_wake`,
`_validate_existing_child_experiment_fresh`,
`_validate_existing_child_experiment_resume`,
`_validate_existing_child_experiment_graph_repair`, and
`_validate_existing_child_experiment_watcher_wake`.

Move `_resolved_dispatch_authority` (144 lines) to
[runs/task_policy.py](../../src/rcp/runs/task_policy.py) as a small serial commit
**before C1**, per decision 8.

**Verify each group separately** with its own focused tests before the next one:
`uv run pytest -q tests/test_branch_merge.py tests/test_branch_merge_api.py` (C1),
`tests/test_graph_condition_watchers.py tests/test_acceptance_experiment_watchers.py`
(C2), `tests/test_experiment_stop.py tests/test_experiment_episode_ending.py
tests/test_experiment_index.py` (C4), and the full `tests/test_auto_research_*.py`
set plus `tests/test_episode_lifecycle_acceptance.py` (C5). Then the full suite.

### Slice D — turn Resume and Retry the right way round

Land after C4 and C5, because it needs the extracted owners to exist.

Today `retry` and `resume` on the engine contain a list of every job type:

```python
if isinstance(original, AutoResearchRunRequest):
    return self._retry_auto_research_task(...)
self._preflight_experiment_episode_recovery(previous, request=original)
if isinstance(original, RunRequest) and original.patch_kind == "experiment_loop":
    if not graph_repair:
        return self._retry_experiment_loop(...)
```

The HTTP handlers at [api/tasks.py](../../src/rcp/api/tasks.py) already load the
record and know its kind before calling. Move the choice there: an Auto-research
task goes straight to the Auto-research module's retry, an Experiment task to the
Experiment module's, everything else to the engine's generic path. Each job-specific
module does its own part and then calls down into the engine for the shared work.

Afterwards `retry` and `resume` on the engine name no job type, and the engine's
only remaining reference to job-specific code is the single documented `_run` call.

### How to work each slice

**Commit at every slice boundary.** One slice, one commit, on `main` — this
repository does not use working branches. A slice is complete only when its own
focused tests, the full backend suite, Ruff, exact-file hooks, and
`pre-commit run --all-files` all pass. Enumerate
`git ls-files --others --exclude-standard` and account for every new path before
committing; a green `--all-files` run proves nothing about a file that is not yet
tracked.

**Stop and ask the human before starting the next slice** whenever any of these is
true. Do not decide them alone — every one of them was settled by measurement in
this document, and guessing re-opens a closed decision:

- a slice cannot preserve behaviour without changing a decision recorded above;
- the measured reality differs from what this plan states — a method is not where
  it is named, a call count is wrong, a group is not independent after all;
- a test on the Slice A checklist turns out not to need recovery, or a test *not*
  on it breaks;
- a move would require adding a `kind`/`surface`/`patch_kind` parameter, a
  dispatch registry, a fallback path, or a second launch or admission table;
- removing the documented `_run` exception starts to look necessary;
- the slice grows past its stated file scope.

**What is not a reason to stop:** a mechanical rename, an import cycle you can
break by moving a type, or a test that needs the new explicit `recover_at_startup()`
call. Those are the expected work.

**Write down what happened.** At each boundary, update this file's slice table with
the commit hash and anything the slice proved wrong. The next agent reads this file,
not the diff.

### Slice ledger

Updated at every slice boundary. The next agent reads this, not the diff.

| Slice | Commit | Notes |
| --- | --- | --- |
| Guards | `da50c3d` | The two deliberate non-refactors recorded at their code sites. |
| A | `68c0ba7` | Constructor purity. Re-measurement found **14** store-mutating constructors, not 10. A post-change probe over the whole suite reports none. |
| B1 | `a931f02` | `runs/episodes/` package: `reconcile.py`, `wrapup.py`, and report admission moved off the engine. Pure relocation. |
| B2 | `7899c2a` | One settlement callback, carrying the execution object. The engine no longer knows Auto-research settles differently. |
| 0 | `44bc556` | `resolved_dispatch_authority` moved to `runs/task_policy.py`, with the three type aliases it needs. |
| C1 + C2 | `08af3e7` | Branch-merge and watcher admission extracted. One commit: both edit `background.py`, and splitting one file's diff by hunk risks committing a state that does not build. |
| C4 | `ce4d5d2` | Experiment recovery extracted. `_skill_update` moved to `runs/task_policy.py` first — importing it back from `background` would have been circular. |
| C5a | — | Auto-research extracted: **36** methods, not the 32 the plan listed. `_create_and_spawn` untouched; its three wrong-way calls now point at the new module and are C5b's job. |

### Slice B2: settled by the human on 2026-08-19

**The ID-only settlement callback cannot preserve behaviour.** `after_task_settled`
in [api/app.py](../../src/rcp/api/app.py) forwards the `AgentTaskExecution` to
`evaluate_graph_conditions_after_task`, which reads two fields that exist only in
memory for the duration of the run and are never persisted:

- `applied_graph_state` — the materialised graph *this* task produced. Reloading
  by id gives the current graph, which is a different thing the moment another
  Sync lands.
- `armed_graph_watchers` — whether the turn armed watchers without applying a
  patch. Nothing durable records it, and it is exactly the case that decides
  whether ready wake groups are delivered.

So an id-only callback either drops the watcher boundary evaluation or requires
persisting graph state on every settlement. Both change behaviour, which puts
this on the stop-and-ask list rather than in an implementer's hands.

**Decided: keep the execution object.** "Operation ID only" now names the
*episode* settlement path, which genuinely can reload, and not this boundary. The
decision exists so settlement does not depend on a live worker's identity;
`applied_graph_state` is a value the run produced, not a handle to it.

**What B2 became.** The collapse still had a point without the id-only shape: the
engine knew that Auto-research settles differently. `_task_settled` now calls one
callback and nothing else. Loading the episode, settling a requested Stop, and
reconciling the Auto-research task moved to
`EpisodeReconciler.settle_auto_research_task`, which keeps its own
`auto_research_task_settled_callback_failed` diagnostic. The app-side handler
calls it from a `finally`, because the Auto-research half ran even when the
generic half raised while the engine owned both — dropping that would have been a
silent behaviour change.


### Ordering summary

```text
0. move _resolved_dispatch_authority -> runs/task_policy.py   (serial, small)
A. constructor purity + the 10-test checklist                 (2 production files)
B. runs/episodes/ package + settlement callback collapse      (~615 lines relocated)
C1 branch merge -> C2 watcher -> C4 experiment -> C5 auto-research
D. invert Resume/Retry at the HTTP handlers
```

### What "done" means

- `BackgroundAgentTasks` contains no job-specific method, and its only reference to
  job-specific code is the one documented `_run` call.
- Constructing it performs no storage write.
- Startup order is explicit in the lifespan and preserves preserve → interrupt →
  reconcile.
- Full backend suite, Ruff, exact-file hooks, and `pre-commit run --all-files` pass.
- The frontend is re-verified if any response shape changed. The baseline drive
  was done on 2026-08-19 and is recorded below; repeat it only if a slice alters
  what a route returns, not merely where its handler lives.
- The scenario staleness sweep in `AGENTS.md` is run and the original work order is
  archived.

## Verification against real records — 2026-08-19

Performed before any of the decisions above, on byte-identical copies of the
human's data directory. The real database was never opened for writing; its
SHA-256 was confirmed unchanged afterwards.

Method: build the app with `create_app(data_dir=<copy>)`, enter the lifespan
through `TestClient` so the real constructor and the real startup sequence run,
then exercise real records. This is exhaustive over the human's data where driving
the UI would only sample it.

| Check | Result |
| --- | --- |
| Real startup over 4 projects, 252 tasks, 25 episodes | clean, **0 warnings**, 0 exceptions |
| Startup wall time | 0.50s |
| Read endpoints across every project | all 200 |
| Phase 1 lifecycle flags on real records | correct — failed→retry only, paused→resume+retry, pause never offered on a terminal task |
| Retry of all 44 recoverable records, provider stubbed | 23 launch; 21 refuse, every refusal classified |
| Refusals that were real bugs | 2, **both predating this refactor** — decisions 1 and 2 above |
| Experiment Run readiness, all 13 real nodes | 10 runnable, 3 blocked only by open graph gates |

Both bugs were fixed and reverified against the same real records, with four
focused regression tests. Full backend suite, Ruff, exact-file hooks, and
`pre-commit run --all-files` all pass.

### Frontend verified against this backend — 2026-08-19

This had not happened once across the whole refactor. It has now, and the result
is clean. Repeat it only when a slice changes what a route *returns*.

- `web/src` is **untouched** by the refactor — `git diff f6085b0..HEAD -- web/src`
  is empty.
- The frozen route inventory in [test_route_inventory.py](../../tests/test_route_inventory.py)
  passes, so every URL and method the frontend calls still exists. Note it freezes
  the route surface, **not response bodies**; those are covered only by the API
  tests and by the drive below.
- `npm --prefix web run build` passes.
- `npm --prefix web test` passes **419/419**.
- Served on a spare port against a copy of the real data directory and driven in a
  browser: project index (4 real projects), a project overview, **Runs**, Inbox,
  Research, Paper, Settings, and Chats. **28 requests, every one 2xx** apart from
  two cosmetic `/favicon.ico` 404s. **Zero console errors. No server traceback.**

**One frontend test was failing and is fixed.** `teamEnrollment.test.mjs` hardcoded
`expires_at: "2026-08-19T00:00:00Z"` and asserted the ledger reads "Available". On
2026-08-19 the ledger correctly reads "Expired", so the test began failing on a
calendar day rather than on a code change. Both of its dates are now relative to
`Date.now()`. This was not a refactor regression and would have failed with or
without any backend work.


## Settled decisions not to reopen

- No second launch/admission table. `operation_admitted` plus the existing task
  row is the durable contract.
- No silent fallback from a specialized child/Experiment/report path to ordinary
  Work or a fresh native session.
- No `kind`, `surface`, `is_chat`, `patch_kind`, filename, or equivalent policy
  selector inside shared execution plumbing.
- No generic run-handler registry, `TaskController`, callback registry, mixin
  split, or second episode coordinator.
- The episode coordinator is a server-owned control plane, one level above
  concrete background tasks; it is not itself a durable provider task.
- Task settlement crosses the final boundary by operation ID only and reloads
  durable state.
- Constructor startup work must happen before requests are accepted and in the
  confirmed preserve → generic recovery → episode reconciliation order.
- Missing or inconsistent durable launch evidence fails loudly.
- Main remains writable while an Auto-research branch runs; exact graph target
  and episode bindings must survive every move.
- Keep policy algorithms explicit even when that leaves some duplicated lines.

## Recommended next execution plan

1. Grill the five bounded decisions above with the human and record the answers
   in the original handoff decision ledger.
2. Re-read the current constructor, lifespan, `EpisodeReconciler`, report
   wrap-up, Resume/Retry routes, and their focused tests. Do not implement from
   this prose alone.
3. Write a short integration plan with exact file ownership and checks. For
   Luna-max workers, use coherent file/module tasks of roughly ten minutes of
   agent work or one hour of human work—not a whole Phase 7 slice and not
   two-line fragments. Shared contracts land serially before consumers fan out.
4. Establish the report/wrap-up and startup owners without changing recovery
   semantics. Preserve all constructor behaviors until their explicit lifespan
   replacement is tested.
5. Commit and document the side-effect-free constructor/startup checkpoint.
6. Add ID-only settlement and episode-linked recovery routing, then remove the
   old special callbacks.
7. Move dispatch authority before extracting its admission consumers.
8. Extract remaining Auto-research, Experiment, watcher, and branch-merge policy
   in small owner-level commits, updating the Phase 7 branch ledger each time.
9. Run the full backend/Ruff/hooks at every logical checkpoint. At final Phase 7
   closure, run web build/test and the applicable served UI/browser path, sweep
   acceptance scenarios, update current specs/instructions, and archive the
   original handoff only when every definition-of-done item is true.

## Definition of the next safe checkpoint

The next checkpoint is complete only when:

- `BackgroundAgentTasks` construction performs no store mutation, recovery, or
  launch;
- startup explicitly computes the episode preserve set, performs neutral
  interruption with that set, and reconciles episode work before `yield`;
- report restart and Experiment Stop recovery still occur exactly once through
  their named owner;
- malformed/legacy-ambiguous admission is never launched;
- focused constructor, startup, report, Auto-research recovery, Experiment Stop,
  and episode lifecycle tests pass;
- full backend, Ruff, exact-new-file hooks, all-files hooks, diff check, and
  untracked inventory pass; and
- the original handoff, this pickup, and the audit describe the same current
  boundary.
