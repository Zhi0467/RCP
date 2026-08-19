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
- **Authority:** the
  [original structural-refactor handoff](handoff-2026-08-18-backend-structural-refactor.md)
  remains the confirmed work order. The
  [architecture audit](rcp_architecture_audit.md) remains evidence. This file is
  the current execution checkpoint and pickup guide.

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

No part of this list is implied complete by `ed4c019`:

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

- `src/rcp/background.py:261` — constructor with current storage/recovery side
  effects.
- `src/rcp/background.py:3446` — `launch_admitted`.
- `src/rcp/background.py:3582` — private `_spawn_record`, now reachable only
  through `launch_admitted` in production.
- `src/rcp/storage/agent_tasks.py:1580` — strict admission-intent reader.
- `src/rcp/storage/agent_tasks.py:2659` — fail-closed dispatch no-start proof.
- `src/rcp/api/app.py:833` — current lifespan reconciliation sequence.
- `src/rcp/api/app.py:679` — current task-settlement callback wiring.
- `src/rcp/runs/episode_reconcile.py:34` — existing coordinator to reuse and
  move, not replace.
- `src/rcp/runs/episode_wrapup.py` — existing common wrap-up/report admission
  policy.
- `src/rcp/api/tasks.py:315` and `:410` — current Resume and Retry route entry
  points that still dispatch directly to the background object.

Line numbers are an `ed4c019` navigation aid, not a contract. Search by symbol
after any edit.

## Decisions to discuss before the next code slice

These are intentionally bounded. The architecture and product policy below them
are already settled.

### 1. What should startup do with a legacy ambiguous queued admission?

Current fact: a strict new admission with no dispatch attempt proves the worker
never started. An old legacy admission with no retained attempt evidence does
not. `launch_admitted` refuses it loudly and will never guess.

Recommended decision: let the existing generic startup interruption path mark
that row interrupted with a durable diagnostic and preserve its task, receipts,
Patch text, and retry history. Do not add a new status and do not leave server
readiness blocked by a permanently queued row. Recovery can then require an
explicit human Resume/Retry through the appropriate owner.

Alternative: leave it queued in an explicit quarantine and surface a human
action. This retains the misleading queued state and needs new projection/UI
semantics, so it is larger debt and not recommended.

What is not an option: silently launch, reconstruct intent from request shape,
or treat missing evidence as a pre-start failure.

### 2. How should the constructor/startup change be sliced?

Recommended decision: keep the behavior change serial at the shared contract,
but assign implementation at coherent file/module granularity:

1. define the exact preserve-set and ID-only startup/reconciliation API;
2. move common report launch/restart and wrap-up ownership so constructor code
   has a real destination;
3. make `BackgroundAgentTasks.__init__` pure and expose only the neutral generic
   startup interruption primitive;
4. wire the explicit preserve → generic interruption → episode reconciliation
   order in `api/app.py`; and
5. only then remove old constructor/callback paths.

The next agent should not commit “constructor purity” alone while silently
dropping report or Stop recovery. If the slices cannot each preserve behavior,
integrate them as one reviewed checkpoint while still delegating file-level
ownership.

### 3. Should a dedicated simultaneous-launch barrier test be added now?

Recommended decision: do not add it as an isolated cleanup before touching
startup. Current public duplicate, concurrent reconciliation, receipt ordering,
and live native-session tests already cover the claim. Add one deterministic
two-thread barrier test in the startup slice only if the new reconciliation path
creates another concurrent caller or changes lock ordering.

### 4. When should dispatch-authority resolution move?

Recommended decision: move `_resolved_dispatch_authority` mechanically to
`runs/task_policy.py` as a small serial contract slice immediately before the
first admission owner leaves `background.py`. Then each extracted owner can
resolve and persist authority at admission; `launch_admitted` continues only to
validate the stored binding. Do not combine this with a generic registry or
infer authority at launch.

### 5. Is the old “24 engine methods” count a target?

No. This documentation now settles that it was a classification of the old
70-method baseline. The neutral launch boundary introduced named launch
validation, and policy-neutral code should not be merged merely to hit 24. The
definition of done is absence of surface policy, not a method count.

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
