# Handoff: backend structural refactor

- **Status:** Implementation is in progress on `main`. The audit correction,
  Phase 0 safety net, Phases 1–2, Phase 4a, and the Phase 5 membership seam are
  committed. Phase 3 found four qualifying harmful partial-write windows; two
  Auto-research endings, graph-repair admission, and the final cross-store
  Experiment-loop recovery are committed. Phase 5's paper and chat routers are
  committed. The detailed Phases 5–7 re-review remains the work order.
- **Originally confirmed:** 2026-08-18. **Phases 0–4 re-review opened and
  closed:** 2026-08-18. **Phases 5–7 re-review closed:** 2026-08-19.
- **Every grouping in this document was checked against the code**, not inferred
  from file listings. The first pass grouped by function name and produced three
  tables; two of them were wrong and one design would have failed on contact.
  Those corrections are marked in place, in Phases 5, 6, and 7.
- **Supersedes:** the recommendations in
  [`rcp_architecture_audit.md`](rcp_architecture_audit.md). That document remains
  the evidence and the explanation — its Appendix A teaches the findings from
  scratch. This one is the work order. Where they disagree, this one wins, and the
  disagreements are listed at the end.

## Implementation decision and ambiguity log

Keep this ledger current while the work is in flight. It records choices made
while translating the settled design into code, including choices that may need
human review. The phase text above and below remains the authority; this ledger
does not silently amend it.

| Date       | Slice        | Decision or ambiguity                                                                                                                                                                                                                                                                                                         | Review state                                                                                     |
| ---------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 2026-08-19 | 0b / 1       | A refused loud single-task transition writes exactly one truthful warning event. “No side effects” in the regression means no false success event, receipt, Patch cleanup, or lifecycle notice; it does not suppress that explicit refusal event.                                                                             | Settled by the Phase 1 contract and now stated explicitly in S10.                                |
| 2026-08-19 | 0c           | Run the one-time coverage inventory after Phase 2 stops changing `api/app.py`, but before Phase 5 moves any route. Measuring a file while another worker edits it would not produce reproducible line/function evidence; the later stable tree still guards 5–7.                                                              | Implementation timing only; no product or test-coverage contract changed.                        |
| 2026-08-19 | commits      | Concurrent workers share one worktree. Verify the combined stable tree, keep a focused check for each owned slice, and create exact-file commits so unfinished or unrelated paths are never swept into an earlier commit.                                                                                                     | User-requested slice discipline; final full-suite and all-files checks still run.                |
| 2026-08-19 | 4a           | Move only the byte-identical result-view id validator to `rcp.artifacts`; do not use the extraction as permission for a helper sweep.                                                                                                                                                                                         | Settled by the handoff; focused result-view tests preserve exact behavior.                       |
| 2026-08-19 | 2            | A `/cached` assertion run only after the current-project route has stored a completed snapshot is a false green: it never exercises completion of graph-only saved data. Keep independent evidence for saved-cache completion and the fresh-open path, plus existing post-stream/reconcile paths.                             | Review correction; no product behavior changed.                                                  |
| 2026-08-19 | 1            | No lifecycle-related `lost its …` guard became redundant: the private transition result intentionally does not escape the store, and the changed production callers consume no new atomic fact. Removed-guard count is zero.                                                                                                  | Settled after auditing the changed call sites; do not sweep the other literals.                  |
| 2026-08-19 | 1 checks     | The episode acceptance fixture's command broker is forbidden by the managed sandbox and produced six `Operation not permitted` failures there. The exact file passed 6/6 when rerun outside the sandbox; this is environmental evidence, not a product fallback.                                                              | Verification fact; retain the unsandboxed command in the slice record.                           |
| 2026-08-19 | 2            | Persisted display snapshots remain graph-only and may therefore omit `experiment_control`; `ProjectDisplayCache.complete_snapshot` is the single completion primitive for fresh drafts and saved dictionaries. The private draft stays non-serializable while retaining minimal item access for internal consumers.           | Settled implementation of the confirmed boundary; review if full immutability is later desired.  |
| 2026-08-19 | 3 / watchers | The watcher-only transaction audit found no qualifying harmful partial commit. Admission/claim paths use compound writers; per-watcher and per-boundary progress is deliberately durable and retryable. This closes only the watcher candidate set, not the rest of Phase 3.                                                  | Evidence reviewed; no watcher production change is justified.                                    |
| 2026-08-19 | delegation   | Luna-max implementation tasks should normally span a coherent file or module seam—about ten minutes of agent work or one hour of human work. Avoid both whole-phase ownership and two-line microtasks; the main agent retains cross-module synthesis, review, checks, and commits.                                            | Human-confirmed working rule for the remaining phases.                                           |
| 2026-08-19 | 5 / gate     | `ApiServices` begins with the three cohesive objects the membership dependency actually needs: `AppStore`, `ProjectCatalog`, and `IdentityAccess`. Its whole-container resolver stays private; exported dependency functions return narrow members. Existing dynamic `app.state` fields remain during incremental extraction. | Settled composition seam; later router slices may add cohesive runtime objects only when needed. |
| 2026-08-19 | 3 / audit    | An AST-assisted inventory found 425 store methods and 156 transitively mutating methods, then reviewed external functions with more than one mutating call. Four sequences met the actual gate: Auto-research non-Stop ending, Auto-research Stop, manual graph-repair admission, and Experiment-loop Patch/watcher/session handoff. Mutually exclusive branches, durable checkpoints, trailing observability, and already-compound writers were closed with no change. | Reproducible classifier plus semantic call-site review; counts are evidence, not future API contracts. |
| 2026-08-19 | 3 / Auto     | Both Auto-research endings now pair the generic episode fence and Auto watcher settlement inside one `BEGIN IMMEDIATE` compound method. Failure injection after the episode update proves both episode and watcher state roll back; a successful retry lands both. | Verified in the campaign and Auto-research focused suites.                                       |
| 2026-08-19 | 3 / repair   | Initial graph-repair admission now claims the rejected parent and inserts the ordinary or Experiment child in one transaction. An explicit orchestration flag distinguishes that first admission from Resume/Retry children that retain `graph_repair` policy but must not consume the parent again. The obsolete restore-on-exception fallback was removed; the direct claim remains only as the low-level eligibility seam used by focused tests. | Failure injection covers rollback for ordinary and Experiment admission; recovery-child routing is focused-tested. |
| 2026-08-19 | 3 / loop     | The Experiment-loop sequence crosses the canonical state repository and SQLite, so one transaction cannot include the canonical Patch. Watcher rows plus episode/session binding now commit in one SQLite transaction. Before that handoff, the current task records a bounded summary containing the exact episode, invocation root, Patch/watch SHA-256 values, graph outcome, watcher ids, and requested stop ids—never the potentially large raw watcher document. A real Retry may consume an unchanged retained `watch.json` only when an explicit, complete, acyclic parent walk finds that exact receipt for the same root, episode, invocation, and current Patch/watch digests. Normal Experiment Patch attribution is stable at the invocation root; graph-repair attribution remains the repair task. Ordinary Work and maintenance survivor rules are unchanged. | Committed as `fc30941`; a real different-operation Retry covers injected compound-handoff failure. The 24 focused Experiment tests, full backend suite, Ruff, and hooks pass. |
| 2026-08-19 | 5 / paper    | The paper group is the first extracted leaf router: four handlers plus `PaperSaveRequest`, the exact shared membership dependency, and a narrow catalog dependency. Route identity stays frozen; only the editable handler-module map moves. Paper API tests now cover snapshot, create, save, sessions, and the removed conflict route's 405. | Committed as `a310205`; focused and full backend checks passed at that checkpoint.                 |
| 2026-08-19 | 5 / chats    | The chat leaf moves four handlers—list, detail, attachment upload, and attachment removal—plus their three chat-history tests. `ApiServices` gains the existing `ChatAttachmentStore` instance and exposes it only through a narrow dependency; the extracted router imports nothing from `api.app`. The cross-module concurrent-open test remains in `test_api.py`, while attachment behavior stays in its dedicated test module. | Committed as `603c18d`; 56 focused tests, full backend suite, Ruff, and both tracked and exact-new-file hook passes are green. |

## Phases 0–4 re-review ledger

This ledger records the 2026-08-18 fact check against the live working tree.
It is authoritative over stale measurements and mechanisms later in this
document.

### Settled

1. **Phase 0a freezes 86 route entries, not 81 method/path pairs.** Each entry
   records one actual FastAPI route's complete method set, path, and handler
   module. The frozen route inventory and editable module map remain separate.
   The live tree has 82 application route entries plus four FastAPI-generated
   entries. Expanding every method would yield 90 pairs, but that is not the
   chosen representation.
2. **There is no S126.** The lifecycle regression extends the existing S10
   durable-agent-work promise and adds focused lifecycle tests. A new
   pytest-only acceptance scenario would duplicate that existing promise.
3. **Phase 1 uses one private status-check-and-update mechanism.** Its result
   distinguishes `applied`, `refused` with the actual status observed, and
   `missing`. Status-changing store operations use that mechanism rather than
   repeating their own guarded update. The result comes from the same database
   observation as the attempted update; callers do not perform a second,
   potentially racy read to explain a refusal.
4. **The structured transition result stays private.** Public lifecycle methods
   keep their existing return shapes: the human pause request returns the
   updated task or raises, while the other single-task operations and the bulk
   restart interrupt continue to return nothing. No production caller is forced
   to consume a result it does not need. A guarded progress-message update is
   not a status transition; when the task is no longer active it quietly omits
   both the message update and its optional event.
5. **A missing task fails loudly and writes nothing.** Every single-task
   lifecycle operation raises `KeyError` when its operation id does not exist.
   This is distinct from refusing a transition for an existing terminal task.
   RCP does not delete `graph_runs` rows, so absence is an internal consistency
   failure rather than a normal lifecycle race. No event, receipt, output
   cleanup, or lifecycle notice is written for the missing id.
6. **Phase 1 adds no second Resume/Retry table.** `can_resume` and `can_retry`
   are already calculated once in `rows.py`, and backend recovery consumes those
   projected values before applying its genuinely additional session, episode,
   and task-kind checks. Leave those explicit calculations in place. Only
   `can_pause` is derived from the status-transition rule.
7. **Phase 2 removes route-level completion as a step callers must remember.**
   The graph-only builder returns an opaque internal draft. One display/I/O
   boundary adds live experiment control and returns the serializable public
   snapshot; route handlers receive and return only that completed form. The
   draft is deliberately not serializable, so bypassing the boundary fails
   loudly instead of sending believable but false control state.
8. **The same Phase 2 boundary owns saved backend snapshot reads.** This is not
   the cache-management UI. The saved snapshot is an internal backend copy used
   when opening or refreshing projects. Routes never receive that unfinished
   dictionary; the display/I/O boundary reads it, adds current experiment state,
   and returns the same completed public form used for freshly built snapshots.
9. **Phase 3 is an audit gate, not a promised refactor.** Re-audit multi-call
   database sequences after Phase 1. A sequence qualifies only when a crash
   between commits would violate a named invariant and no recovery design relies
   on the earlier commit standing. Fix each verified case with one narrow
   compound store method and a failure-injection test. If the audit finds no
   qualifying sequence, Phase 3 closes with no code change.
10. **Phase 4a moves the one verified duplicate and does not start a sweep.**
    Move the byte-identical result-view id validator to `rcp.artifacts` beside
    `ResultViewDescriptor`, expose it under a descriptive public name, and import
    it from both `runs/result_views.py` and `transport/run_stage.py`. The existing
    dependency direction supports that home. Do not search-and-refactor unrelated
    similar helpers as part of this phase.
11. **Phase 4b is struck.** The watcher exit mapping is a tiny stable protocol,
    not a changing computed policy like repository write scope. Runtime tests
    already cover exit `0`, exit `1`, and an unexpected exit, and the Experiment
    prompt test pins the work-remains wording. A shared renderer would add
    indirection without addressing demonstrated drift.

### Corrections forced by the code and current invariants

1. Pytest currently collects **2,204** tests, not 2,201.
2. Phase 0b must test every lifecycle operation against every source status and
   must assert all guarded side effects: status, events, receipts, retained Patch
   output, and lifecycle notices. An illegal completion currently deletes a
   retained `graph_run_outputs` row from each of four terminal statuses even
   though the status transition did not apply.
3. The 22 production lifecycle-writer call sites remain exact. The 66 literal
   `lost its …` guards remain exact, but they now span 19 files, not 15, and many
   guard immutable bindings or other non-lifecycle facts. They are not evidence
   that all 66 exist because writers return no transition result.
4. Phase 2 has seven direct `attach_experiment_control` calls in `api/app.py`
   and two internal calls in `projects.py`, not eight plus two.
5. Phase 3's named watcher examples already call one compound atomic store
   operation. They are not evidence of a caller-level partial-write bug.
   `AppStore` currently has 242 public callable members across the same ten
   mixins and is referenced from 22 `src/rcp` files.

### Still to settle before implementation

None in Phases 0–4.

## Phases 5–7 re-review ledger

This ledger records the second 2026-08-18 fact check against the live working
tree plus the decisions closed with the human through 2026-08-19. It is
authoritative over stale measurements and mechanisms later in the document.

### Corrections forced by the code

1. **The Phase 5 split table is complete, but it counts handlers rather than
   route entries.** It assigns all 77 distinct application handlers. Five of
   those handlers each own an additional `HEAD` route, producing the already
   settled 82 application route entries. There are no five unassigned routes.
2. Phase 5's closure totals reproduce, but the frequency labels were wrong:
   `catalog` is captured by 57 distinct handlers and `store` by 49. The old
   59/54 values count route entries and therefore count stacked `GET`/`HEAD`
   handlers twice. The 31 distinct names, median transitive reach of 2, and
   maximum of 11 remain exact.
3. The membership test already creates a deliberately ungated project route and
   proves the structural check catches it. No temporary source edit is needed.
4. The two Sync handlers have 11 transitive closure dependencies in their union
   and share 9 of them; they do not share all 11.
5. `runs/work.py` contains 88 module-level class/function definitions plus one
   private alias, for the stated total of 89 named definitions. The ten-member
   result-view cluster and two-member watcher-maintenance cluster reproduce.
6. **Phase 6's claimed mutual recursion is false.** The internal call graph has
   no cyclic strongly connected component. `_validate_patch_deliverable` calls
   `_apply_work_patch` in one direction; `_reject_patch_deliverable` does not call
   it. `_validate_work_patch_live` and `_apply_work_patch` share
   `_prepare_work_patch_candidate`; neither calls the other.
7. The Phase 6 prompt, child-work, and approximately-26-definition patch cluster
   measurements have no retained membership lists, so their counts and outbound
   edges could not define a file move. The confirmed semantic owners and newly
   recorded exact move ledgers in Phase 6 replace those unsupported groupings.
8. `BackgroundAgentTasks` remains exactly 3,916 lines and 70 methods. The five
   named engine methods do make the 12 listed calls, but that list is not an
   exhaustive engine-to-policy boundary under the handoff's own labels:
   `_create_and_spawn` and `start_branch_merge` also call
   `_resolved_dispatch_authority`, which the cluster table calls policy.
9. The Phase 7 counts of 45 policy methods and 32 policy-to-engine callers have
   no retained classifier. Different reasonable classifications produce
   different totals, so neither number is implementation authority.
10. `ProjectService` has 43 methods including `__init__`, or 42 when
    construction is excluded. Its 1,520-line class and 362-line
    `_build_sync_patches` measurement remain exact.

### Settled during re-review

1. **Phase 5 uses one typed `ApiServices` composition container.** Startup
   stores that container at `app.state.services`; it holds the small set of
   cohesive runtime objects that extracted routes need. Module-level dependency
   functions read the container and return a narrow value such as the store,
   catalog, identity service, or background-task manager. A handler requests
   only those narrow dependencies, not the whole container. Pure helpers remain
   module-level functions rather than becoming fields. This replaces closure
   capture without copying all 31 captured names into unrelated dynamic
   `app.state` attributes or turning `ApiServices` into a business layer.
2. **Phase 6 separates by semantic ownership, not file length or graph shape.**
   `work.py` keeps ordinary Work-turn orchestration and genuinely shared Work
   execution mechanics. Result-view lifecycle, Experiment-loop turn policy, and
   Auto-research child-Work policy move to their corresponding owners. Those
   specialized paths may call shared Work plumbing, but shared plumbing must not
   become a mode-switching algorithm that repeatedly branches on
   `patch_kind == "experiment_loop"` or child status. The internal call graph
   determines a safe dependency direction and extraction order; it does not
   decide whether these semantic ownership boundaries exist. The task-dispatch
   boundary selects explicit ordinary-Work, Experiment-loop-invocation, and
   Auto-research-child-Work entry points; one `stream_work_run` no longer selects
   among those policies internally.
3. **Phases 6–7 make the episode/task hierarchy visible in the package tree.**
   `src/rcp/runs/tasks/` owns execution of one already-admitted task;
   `src/rcp/runs/episodes/` owns the long-lived parent, including admission,
   budget, Stop, graph/session binding, child and watcher coordination, ending,
   and wrap-up. An Experiment-loop therefore has separate episode policy and
   per-invocation task modules; Auto-research parent policy is likewise distinct
   from orchestrator, worker, and child-Work task execution. `background.py`
   remains the policy-neutral task scheduler and lifecycle engine.
4. **Do not build a new task-runtime framework to accomplish that split.** Keep
   the existing `WorkTurn` as the shared state for one Work-derived execution.
   Specialized task modules import the small set of Work mechanics they need in
   one direction; task dispatch selects their explicit entry points, so
   `tasks/work.py` never imports them back. Add no wrapper context hierarchy,
   strategy registry, or generic mode-switching adapter. The work is primarily
   directory restructuring plus relocating policy to its actual owner.
5. **The episode coordinator is server-owned control plane, not another agent
   task.** It reacts to human actions, task settlement, watcher or child events,
   and startup reconciliation. Each concrete provider invocation remains an
   `AgentTaskRecord`; no persisted manager task stays running while an episode
   sleeps. The policy-neutral background engine emits one generic task-settled
   notification, and restart reconciliation covers a crash before that
   notification is handled. This is a single lifecycle integration point, not
   the rejected registry of per-mode engine hooks.
6. **Task settlement crosses that boundary by durable identity only.** After the
   background engine commits a task verdict, it notifies the episode coordinator
   with the task `operation_id`. The coordinator reloads the committed task,
   episode, request, and receipts from storage; it does not depend on the
   callback's in-memory request or `AgentTaskExecution`. Normal settlement and
   startup repair therefore drive the same idempotent reconciliation logic, and
   a crash between the task commit and notification cannot lose episode work.
7. **Reuse `EpisodeReconciler` as the common episode coordinator.** Move the
   existing owner under `runs/episodes/reconcile.py`; do not add a parallel
   manager object. Its settlement entry point accepts the durable task id,
   reloads current task and episode state, and explicitly enters the
   Auto-research or Experiment reconciliation path. The background engine's
   separate Auto-research settlement callback and embedded episode-settlement
   work are removed.
8. **Keep that common coordinator thin.** `EpisodeReconciler` owns the durable
   settlement/startup entry points and genuinely common ending/report
   coordination. It explicitly calls Auto-research or Experiment functions;
   their admission, child/watcher, Stop, recovery, and settlement algorithms
   remain in named mode-specific modules. “Single coordinator” does not mean a
   new giant episode class, a profile registry, or a generic policy object.
9. **The package tree expresses ownership, not a demand to move every file.**
   Lifecycle-owned task and episode policy moves under `runs/tasks/` and
   `runs/episodes/`. Genuinely cross-cutting execution support such as `chat.py`,
   `patch_validator.py`, `shared.py`, and `task_policy.py` remains at the
   `runs/` root. Do not add a content-free `common/` package or force shared
   primitives under a false task or episode owner.
10. **`BackgroundAgentTasks` construction becomes side-effect-free.** Its
    constructor wires the generic task engine but performs no database mutation
    or episode recovery. During app lifespan startup, the already-constructed
    `EpisodeReconciler` first identifies episode-owned task ids whose committed
    admissions must survive; the engine performs its generic interruption pass
    with that preserve set; then the reconciler repairs and launches episode
    work before the app accepts requests. Auto-research dispatch recovery,
    Experiment Stop recovery, and episode-report restart no longer run from the
    engine constructor.
11. **Episode policy launches only a durably admitted task id.** The engine
    exposes `launch_admitted(operation_id)`. It reloads the queued task, persisted
    request, parent, authority, and continuation cause, validates them, and owns
    the in-process dispatch claim and worker start. Episode modules never pass a
    reconstructed request, parent record, or continuation enum across this
    boundary. Every episode admission persists its exact launch intent in the
    same transaction as the task so normal launch and crash recovery call the
    same operation.
12. **`launch_admitted` is idempotent but never reconstructive.** A missing id
    raises `KeyError`. A queued task with complete, consistent durable launch
    intent is claimed and launched; an in-process duplicate or a task already
    beyond `queued` returns its current record without another launch. Missing,
    malformed, or inconsistent durable request, authority, parent, or
    continuation data fails loudly before a worker starts.
13. **The task API explicitly dispatches recovery by durable lineage.** The
    extracted `api/tasks.py` loads the task once: episode-linked Resume/Retry goes
    to `EpisodeReconciler`, while ordinary Resume/Retry goes to the background
    engine. Mode-specific internal recovery calls its episode module directly.
    Do not add a `TaskController`, inject policy callbacks into the engine, or
    make `BackgroundAgentTasks.resume/retry` inspect episode modes. Branch merge
    and hidden report recovery keep their explicit refusals.
14. **Dispatch authority is resolved at durable admission, not launch.** Move
    the current resolver from `background.py` to the shared
    `runs/task_policy.py`. Each owning admission path resolves and validates the
    exact authority, including parent-continuation non-widening, before inserting
    the task. `launch_admitted` only validates and uses the persisted binding; it
    never infers authority from request shape or episode mode. Episode reports
    retain their explicit no-graph-authority contract.

### Remaining implementation bookkeeping — not open design

No design decision remains before implementation. Phase 6 still requires an
exact per-move definition/call ledger, and Phase 7 still requires a branch-level
ledger when a mixed engine method loses its surface-specific branch. Those are
review artifacts for applying the settled ownership rules below, not permission
to choose new modules, profiles, registries, callbacks, or context frameworks.

## What this is

Seven backend workstreams plus their safety-net phase, and the order to execute
them in.
Phase 1 contains a bug with two reproducible side effects today. Phase 2 is a
latent correctness hole; Phases 3–7 are structural work whose exact claims are
qualified in their own sections.

Every phase changes structure, not product behavior — with one deliberate
exception in Phase 1, where the current behavior is wrong and the fix is visible
in the UI.

## Goals

1. **Fewer bugs of this class.** A rule written in several places drifts apart.
   Each phase replaces several descriptions of one rule with one description.
2. **Cheaper agent edits.** Nearly all code here is written by agents fanned out
   across module boundaries. A boundary containing 82 application route entries,
   or one 3,900-line
   class, is not a boundary — it is a single lock.

**Not a goal: making the code pleasant for a new human reader.** There is one human
on this project and they are not reading these files line by line. Do not spend
effort on naming, comment coverage, or docstring polish justified only by
onboarding. If a change is justified only by "it reads better," skip it.

**One honesty note about goal 2.** The churn figures below are measured. The
inference from them — that these files serialize work that could have been
parallel — is reasoning, not measurement. No specific case where two changes
blocked each other has been demonstrated. The argument is sound; treat it as an
argument.

## Why these findings, and what the claim is worth

The audit that produced this list was run against a broken copy of the repository.
Several of its findings were false and have been struck. The ones below were
re-verified against the real tree on 2026-08-18 and are real.

They are **verified, not exhaustive**. The audit could not report problems in files
it never received. Do not conclude that anything absent from this list is fine.

## Two reproducible side effects of the Phase 1 bug

**One.** A task that already failed still writes pause and start entries into the
log the human reads. The status column refuses the transition correctly; the event
log and the receipt log are written outside that guard.

```
status after fail:                          failed
status after pause on a failed task:        failed      <- correct
status after mark_running on a failed task: failed      <- correct

event log:
    info    | Preparing agent task.
    error   | boom
    warning | Paused. Resume from the saved agent session, or retry from the beginning.
    info    | Preparing agent task.

receipt categories: ['operation_failed', 'operation_paused']
```

**Two.** An illegal completion of a terminal task also deletes its retained
`graph_run_outputs` row even though the status update does not apply. That is
durable Patch evidence, so the false cleanup is more than a misleading timeline
entry.

The correct guarded pattern already exists twenty lines away.
`request_agent_task_pause` in
[agent_tasks.py](../../src/rcp/storage/agent_tasks.py) checks whether its update
changed a row and raises if it did not:

```python
if cursor.rowcount == 0:
    raise ValueError("Only a queued or running operation can be paused.")
```

`pause_agent_task` runs the same kind of guarded update and then writes its event
and receipt regardless. `mark_agent_task_running` does not check at all. Same file,
same week. The concept is not missing — it is applied inconsistently, and nothing
makes the inconsistency visible.

That is the shape this whole handoff is about.

## The four files, and why each is big for a different reason

Measured 2026-08-18. Commit counts are out of the last 60 commits; churn is over
the last 40 commits that touched the file.

| File                | Lines | Commits | Churn            | Shape                                                                            |
| ------------------- | ----- | ------- | ---------------- | -------------------------------------------------------------------------------- |
| `runs/work.py`      | 5,207 | 19      | +8,675 / −3,501  | **Flat.** 89 module-level definitions, only 2 of them public.                    |
| `background.py`     | 4,226 | 18      | +4,698 / −775    | **One class.** `BackgroundAgentTasks` is 3,916 of those lines, 70 methods.       |
| `api/app.py`        | 3,910 | 31      | +10,721 / −6,642 | **One function.** `create_app` runs 333→3278 with 99 functions nested inside it. |
| `tests/test_api.py` | 9,051 | 27      | +10,939 / −1,551 | One test file spanning a broad API surface.                                      |

They need three different fixes, which is why they are three phases and not one.
`work.py` has no lexical-closure problem, but its Work, Experiment-loop, and
Auto-research-child policies are interleaved; Phase 6 separates those semantic
owners without inventing a runtime framework. `app.py` is the hardest because
undoing the route-handler nesting is the work.

## Order

| Phase | What                                                 | Fixes a live bug | Rough size             |
| ----- | ---------------------------------------------------- | ---------------- | ---------------------- |
| 0     | Safety net                                           | no               | half a day             |
| 1     | One agent-task lifecycle                             | **yes**          | 1–2 days               |
| 2     | A project snapshot cannot be half-built              | latent           | half a day             |
| 3     | Re-audit harmful transaction gaps; then fix narrowly | latent           | re-scope after Phase 1 |
| 4     | Move one duplicated result-view validator            | no               | small                  |
| 5     | No route body in `app.py`                            | no               | 3–5 days               |
| 6     | Work-derived task policy moves to its semantic owner | no               | 2 days                 |
| 7     | `background.py` keeps only the engine's job          | no               | 2–3 days               |

Sizes are estimates with no basis beyond judgement. Treat them as appetite, not
as commitments.

Each part of Phase 0 guards a different phase, and a phase may start as soon as
its own guard is in place: **0a** guards Phase 5, **0b** guards Phase 1, **0c**
guards Phases 5–7. Phase 1 does not wait on the route inventory.

Phases 1–4 are independent of each other and of everything below.

Phase 5 is independent of 6 and 7. **Phases 6 and 7 are sequential, in that
order** — both create new modules under `src/rcp/runs/`, and both touch
auto-research, so running them in parallel would invent two different homes for
the same subject. Phase 6 is the cheaper and cleaner split, so it establishes the
naming that Phase 7 joins.

---

## Phase 0 — the safety net

Everything after this moves code without changing behavior. That only works if
behavior change is detectable. The existing 2,204 tests are a good net: they drive
the real app and assert behavior, not structure, so they survive code moving. But
they do not assert the two things a structural change breaks first: **which routes
exist**, and **which state transitions are legal**.

### 0a. Route inventory

New `tests/test_route_inventory.py`. Assert the full set of routes against a
committed literal: **86 route entries today** — 82 defined in `app.py`, 4
generated by FastAPI (`/openapi.json`, `/docs`, `/docs/oauth2-redirect`,
`/redoc`). One entry is one actual FastAPI route with its complete frozen method
set, path, and handler. Expanding every method would yield 90 method/path pairs;
do not use that representation.

Record two things, and keep them **separate**:

1. A **frozen list** of 86 route entries. Phase 5 must never edit it. A route that
   disappears or changes path or method set fails here, and no amount of editing
   the other half can hide it.
2. A **module map** saying which file each handler is defined in. Phase 5 edits
   this on every extraction commit, which is exactly what makes the move
   reviewable — the diff shows which routes changed home.

Keeping them in one literal would mean rewriting the guard in the same commit as
the change it guards, which guards nothing.

**The trap.** `include_router` on this FastAPI leaves an opaque `_IncludedRouter`
in `app.routes` rather than merging routes into it. A flat walk finds **zero**
project-scoped routes and passes vacuously. Descend through `original_router`:

```python
def walk(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from walk(inner.routes)
        elif hasattr(route, "methods"):
            yield route
```

The OpenAPI schema is not a substitute: it currently exposes the 82 application
operations but omits the four generated route entries.

### 0b. Lifecycle transition table test

New `tests/test_agent_task_lifecycle.py`. Table-driven over every lifecycle
operation against every source status in `AgentTaskStatus`. The operation rows
are `mark_running`, `request_pause`, `pause`, `complete`, `fail`, the
`interrupted` form of `fail`, and the bulk restart interrupt. For each case,
drive a task into the source status, attempt the operation, and assert five
things:

1. the resulting status,
2. how many events were appended,
3. how many receipts were appended,
4. whether retained Patch output stayed present, and
5. which lifecycle notices were appended.

Assertions two through five are the point. Assertion one already passes today.

Land it asserting **current** behavior, with the wrong cases marked
`pytest.mark.xfail(strict=True)` and a comment naming Phase 1. `strict=True` means
the suite fails if one of them starts passing by accident. Phase 1 deletes the
marks. `main` stays green and the bug is visible in the suite meanwhile.

**The count, measured 2026-08-18: 22 of the 49 possible (status, operation)
pairs write a log entry for a transition that did not happen.** Not two. Nearly
half.

The event/receipt pattern is systematic, not scattered:

- `mark_running` against any non-`queued` status writes one event (6 pairs).
- `pause`, `complete`, `fail`, and `interrupt` against any terminal status —
  `paused`, `succeeded`, `failed`, `interrupted` — each write one event **and** one
  receipt (16 pairs).
- `request_pause` correctly writes nothing, because it raises. It is the only one
  of the direct one-task operations that gets this right.

There is a second side effect the original count missed: `complete` against any
of those four terminal statuses deletes the retained Patch output even though
the guarded status update changed no row. That is forbidden by the durable task
evidence contract and must have its own assertions; an event-count xfail must
not hide it.

The live-tree reproduction is 22 of the 49 operation/status cases above. The
bulk `interrupt_active_agent_tasks` path changed only active tasks and emitted
nothing for terminal tasks in the single-task matrix. Its multi-task behavior
still belongs in the table because it has a different bulk contract.

### 0c. Coverage, measured once

`pytest-cov` is now present in the `dev` dependency group in
[pyproject.toml](../../pyproject.toml). Run coverage over `src/rcp/api`,
`src/rcp/background.py`, and `src/rcp/runs/work.py` once.

The deliverable is **a list of routes and functions no test exercises**, appended
to this document. Those are where Phases 5–7 have no safety net and need a test
written before the move, not after.

One-time measurement. Do not add a coverage threshold or a CI gate.

#### Phase 0c result — 2026-08-19

The authoritative run used the existing checkout environment and emitted its
JSON outside the repository:

```text
.venv/bin/python -m pytest --cov=src/rcp \
  --cov-report=json:/private/tmp/rcp-phase0c-20260819-elevated.json \
  --cov-report=term-missing
```

The first sandbox execution of that command could not start the acceptance
command broker (`[Errno 1] Operation not permitted`), so its 39 failures were
not treated as code failures or used for the inventory. The same command was
rerun with the required sandbox escalation. It passed **2267 tests**, with one
existing `StarletteDeprecationWarning`, in 436.29 seconds. Coverage.py 7.15.4
reported 82.85% overall (36,955 statements; 30,616 covered); branch coverage
was not enabled. The JSON used below is
`/private/tmp/rcp-phase0c-20260819-elevated.json`.

Tree state captured before and after the run was the same: `HEAD` was
`f6085b0d08f4779f9a38707342f6d2567b5ab53c`, with these concurrent paths dirty
or untracked:

```text
M docs/acceptance/S10-pause-resume-retry.md
M docs/handoffs/handoff-2026-08-18-backend-structural-refactor.md
M docs/handoffs/rcp_architecture_audit.md
M docs/specs/projects-spaces-and-operations.md
M pyproject.toml
M src/rcp/api/app.py
M src/rcp/artifacts.py
M src/rcp/projects.py
M src/rcp/runs/result_views.py
M src/rcp/service.py
M src/rcp/storage/agent_tasks.py
M src/rcp/storage/models.py
M src/rcp/storage/rows.py
M src/rcp/transport/run_stage.py
M tests/test_api.py
M uv.lock
?? tests/test_agent_task_lifecycle.py
?? tests/test_route_inventory.py
```

The target-file summaries from that run were:

| File                      | Statements | Covered | Coverage |
| ------------------------- | ---------: | ------: | -------: |
| `src/rcp/api/__init__.py` |          2 |       2 |     100% |
| `src/rcp/api/app.py`      |      1,699 |   1,481 |      87% |
| `src/rcp/api/episodes.py` |        199 |     188 |      94% |
| `src/rcp/api/identity.py` |         60 |      58 |      97% |
| `src/rcp/background.py`   |      1,554 |   1,306 |      84% |
| `src/rcp/runs/work.py`    |      1,717 |   1,354 |      79% |

#### Route handlers with zero executed statements

The live route inventory was walked recursively through `original_router`; the
82 `APIRoute` entries were grouped by their runtime endpoint `__name__`, then
joined to coverage.py's `create_app.<endpoint>` function summaries. These seven
application handlers had zero covered statements:

| Endpoint              | Method and path                                 | Statements |
| --------------------- | ----------------------------------------------- | ---------: |
| `logout_team_session` | `POST /api/team/session/logout`                 |          5 |
| `space_users`         | `GET /api/space/users`                          |          2 |
| `providers`           | `GET /api/providers`                            |          1 |
| `register_project`    | `POST /api/projects`                            |          5 |
| `project_watchers`    | `GET /api/projects/{project_id}/watchers`       |          2 |
| `save_paper`          | `PUT /api/projects/{project_id}/paper`          |          2 |
| `paper_sessions`      | `GET /api/projects/{project_id}/paper/sessions` |          2 |

#### Other zero-covered functions and methods

These are the remaining target-file functions whose coverage.py function
summary had `covered_lines == 0` and at least one statement, grouped by file.
The line is the function summary's start line; the number in parentheses is
its statement count.

- `src/rcp/api/app.py`: `create_app.background_task_stream.validate_auto_research_patch` (line 693, 6); `create_app.background_task_stream.apply_auto_research_patch` (line 720, 4); `default_data_dir` (line 3903, 4).
- `src/rcp/api/episodes.py`: none.
- `src/rcp/api/identity.py`: none.
- `src/rcp/api/__init__.py`: none.
- `src/rcp/background.py`: `BackgroundAgentTasks.pause_auto_research_worker` (line 2564, 6).
- `src/rcp/runs/work.py`: `_experiment_maintenance_binding` (line 977, 10); `_process_experiment_watcher_maintenance.reject_maintenance` (line 1084, 2); `_record_work_lock_lost` (line 4979, 4).

This is a one-time statement/function inventory, not a claim that covered
functions are fully tested. It does not measure branch coverage, does not infer
untested code from missing-line lists, and does not add a threshold or CI gate.
FastAPI-generated routes are intentionally excluded from the application
handler list; the frozen route test still guards all 86 route entries.

**Phase 0 is done when:** both tests are committed and green, the xfail count is
recorded here, and the untested list is written here.

---

## Phase 1 — one agent-task lifecycle

### The problem

The lifecycle rule is written in three unrelated kinds of place.

**One — SQL guards.** Seven `UPDATE graph_runs` statements in
[agent_tasks.py](../../src/rcp/storage/agent_tasks.py), each with its own
hand-typed `WHERE … status IN (…)`. Three handle the refused case differently from
each other.

**Two — capability flags.** [rows.py](../../src/rcp/storage/rows.py) computes what
the UI may offer. Only `can_pause` duplicates a status-transition rule;
`can_resume` and `can_retry` govern creation of a new recovery task and carry
different requirements:

```python
data["can_pause"]  = visible and status in {"queued", "running"}
data["can_resume"] = visible and status in {"paused", "interrupted"} and ...
data["can_retry"]  = visible and status in {"paused", "interrupted", "failed"} and ...
```

**Three — guarded caller assumptions.** There are 66 literal `lost its …`
messages across 19 files: `background.py` (15), `runs/work.py` (9),
`runs/auto_research_child_reconcile.py` (6), `api/app.py` (6), and fifteen other
files. The count is real; the original causal claim was not. Many guard immutable
bindings, retained snapshots, episode ancestry, or other facts unrelated to a
status transition. Audit the lifecycle-related subset without treating the
literal phrase as proof of why a guard exists.

### The change

**One transition table, and no recovery table.** `can_pause` maps onto a real
status transition and can be derived from the table. `can_resume` and `can_retry`
cannot: recovery creates a new task rather than moving the existing row, and the
two actions have different requirements involving `native_session_id`,
`stage_ready`, `recovery_abandoned`, and the `branch_merge` exception. Those
calculations are already written once in `rows.py` and consumed by the backend;
leave them explicit there rather than introducing a second abstraction.

1. **`AGENT_TASK_TRANSITIONS`** in [models.py](../../src/rcp/storage/models.py),
   beside `AgentTaskStatus` and `ACTIVE_AGENT_TASK_STATUSES`. A mapping from each
   target status to the frozen set of statuses it may be entered from. Derive it
   from the seven existing `WHERE` clauses. **This phase does not change which
   transitions are legal.**

2. **One private transition seam in `agent_tasks.py`.** It returns a structured
   result distinguishing `applied`, `refused` with the actual status observed,
   and `missing`; a blanket boolean is insufficient. It must read the table and
   run the guarded update on the same connection as
   every transition-owned side effect. Events, receipts, lifecycle notices, and
   successful-completion cleanup occur only for the outcome they describe.
   In particular, an illegal completion must not delete retained Patch output.

   The result retains the prior status needed for a truthful refusal. Callers do
   not issue a second read, which could observe a different state.
   A `missing` result becomes `KeyError` before any side effect is written.

3. **Keep the structured result private and preserve public return shapes.** There
   are exactly **22 call sites in `src/`** (`update_agent_task_message` 11,
   `pause_agent_task` 3, `fail_agent_task` 3, `request_agent_task_pause` 2, and one
   each for `mark_agent_task_running`, `complete_agent_task`, and
   `interrupt_active_agent_tasks`). That count does not make one blanket return
   type correct: `update_agent_task_message` is a message-only guarded update,
   `interrupt_active_agent_tasks` is bulk, and `request_agent_task_pause` is the
   human record-or-raise API. Only that pause-request return is consumed by a
   production caller; the remaining calls ignore returns. The pause request
   therefore still returns the updated record or raises, and the other methods
   continue returning nothing. A guarded progress-message update is not a status
   transition and quietly omits both its update and optional event once the task
   is no longer active.

4. **Only `can_pause` reads the transition rule.** `can_resume` and `can_retry`
   keep their current explicit calculations in `rows.py`; backend recovery keeps
   consuming those projected values and applying its additional checks.

   This reaches the frontend. Those three fields are read by the web app, and this
   phase must not change their values. The route inventory test does not cover
   that; verify it by driving the UI.

5. **A refused transition writes one explicit event.** Confirmed decision: loud,
   not silent — in the shape of `"Pause refused: this task already failed."`, at
   `warning`.

   The reason, in the human's words: **it does not change control, and it stays
   faithful.** The refusal changes nothing about what the system does — the
   transition was already being refused — and the timeline ends up saying what
   actually happened instead of what did not.

   This costs no extra lines. Today a refused pause already writes one line, and
   that line is false; loud replaces it with a true one. Silent would have written
   none.

   **If refusal lines start appearing often, that is a finding, not a nuisance.**
   It would mean code is routinely trying to pause or complete tasks that are
   already dead — a second bug that has been invisible until now. Do not quietly
   soften these into silence; investigate what is emitting them.

6. **`request_agent_task_pause` keeps raising.** It is a human action; the API
   should return a 4xx, not log a refusal and report success.

7. **Audit the lifecycle-related guards; do not sweep all 66 literals.** Start
   from the callers changed by this phase. Delete a guard only when its protected
   fact is now returned atomically by the lifecycle operation. The other
   `lost its …` guards are outside this phase merely because they share wording.
   Record the count removed.

### Invariant at risk

**Invariant 10g** — Stop loop is idempotent, durable, and restart-safe, and a
graph-level rejection is still recorded as that turn's accepted operational result.
Any change to what a refused transition writes must leave that intact. Run
`tests/test_experiment_stop.py` and `tests/test_episode_lifecycle_acceptance.py`
specifically.

### Existing acceptance scenario S10, plus focused regression

Do not create S126. Extend
[`S10-pause-resume-retry.md`](../acceptance/S10-pause-resume-retry.md) with the
durable task-history truth this bug exposed, and add the focused
`tests/test_agent_task_lifecycle.py` matrix. The regression asserts that a task
that reached a terminal status records no later false start, pause, completion,
failure, or interruption event or receipt; an attempted illegal transition
records exactly one truthful refusal event when its contract is loud; retained
Patch output is not deleted; and lifecycle notices match transitions that
actually applied.

### Verification

- `tests/test_agent_task_lifecycle.py` with the xfail marks removed.
- The amended S10 pytest assertions and applicable S10 browser path.
- `uv run pytest`, `uv run ruff check src tests`.
- **UI drive required.** Serve the app, run a task, pause it, let one fail. Confirm
  the buttons behave as before and the history reads correctly. Check
  `read_console_messages` and `read_network_requests`.

---

## Phase 2 — a project snapshot cannot be half-built

### The problem

`ProjectService` builds a project snapshot carrying a **default, empty** experiment
control block, because it has no task store. Any route returning that snapshot must
overwrite it by calling `attach_experiment_control`. Its own docstring in
[projects.py](../../src/rcp/projects.py) says what happens when a route forgets:

> a Settings save would blank the Experiment lifecycle the human is watching in Runs

There are **seven direct call sites in `app.py` and two internal calls in
`projects.py`**, and nothing checks the response boundary. A route added tomorrow
that forgets produces a snapshot that is structurally valid, passes every schema
check, and is wrong.

### The change

The incomplete thing must not look like the complete thing, and completing it
must not remain a step each route remembers independently.

1. The graph-only builder returns an opaque internal snapshot draft rather than a
   serializable dictionary. It omits live experiment control entirely.
2. One display/I/O boundary accepts that draft, adds live experiment control,
   and returns the serializable public snapshot. Route handlers call APIs that
   already return this completed form; they do not call
   `attach_experiment_control` themselves.
3. The display/I/O boundary owns saved backend snapshot reads and never hands a
   route a dictionary that still needs completion. This internal saved copy is
   unrelated to the cache-management UI.
4. The internal draft is deliberately not serializable. A future route that
   bypasses the public boundary therefore fails loudly rather than returning a
   valid-looking partial snapshot.
5. Tests assert that every current project-snapshot route returns populated live
   control and that attempting to return or encode a draft fails.

The original marker-on-the-successful-return proposal does not solve this: a
future route that skips the composition function also skips its marker and is
invisible to the test. The non-serializable property belongs to the incomplete
draft itself. The normal I/O path makes completion unavoidable; serialization
failure is the backstop for code that bypasses that path.

### Verification

`uv run pytest`, plus route checks for current, saved, and Settings-update project
snapshots, and a UI drive of Settings save → Runs, which is the exact path the
docstring warns about.

---

## Phase 3 — audit for harmful partial commits, then fix only proven cases

### What is actually true

`AppStoreBase.connection()` commits when its block exits, so **two store calls are
two commits** unless they deliberately share a connection through one compound
store method. The original narrowing measurements are not reliable enough to
implement:

- Writing store methods do call other public writers today. The Phase 1 event
  and receipt calls are examples; Experiment Stop also intentionally persists
  its fence before later settlement.
- The claimed count of 19 external functions had no retained classifier or
  reproduction script, so it cannot define the work order.
- The named watcher example is already atomic. `claim_and_spawn` chooses one of
  `create_experiment_episode_with_invocation`,
  `create_experiment_watcher_invocation`, or
  `create_watcher_notification_task`; each compound method owns the related
  SQLite writes in one transaction before spawning.

Re-audit this phase after Phase 1. Distinguish intentionally separate durable
checkpoints from a genuinely harmful partial state. A sequence is a candidate
only when a crash between its commits violates a named invariant and no recovery
path deliberately relies on the earlier commit standing.

### The conditional change

Do **not** add a general transaction context manager merely because this phase
exists. A context manager that yields a connection and then wraps existing store
calls would not make them atomic: those methods open their own connections.

For each verified harmful sequence, add one narrow compound store method
that accepts the complete typed intent and performs its existing private
connection-aware writes together. Use a reusable ambient transaction only if
several independently justified cases require it and its nested commit,
rollback-only, exception, and thread-local behavior are specified first. Every
converted sequence gets a failure-injection test proving that either the complete
state lands or none of it does. If the audit finds no qualifying sequence, record
that result and make no production-code change in this phase.

### Explicit non-goal

**Do not split `AppStore`.** It currently has 242 public callable members across
10 mixin modules and
is held by 22 modules in `src/rcp`. Splitting it into narrow per-topic interfaces
is the most finding-shaped response available and buys nothing: it is one database,
it stays one database, and every holder gains new imports for no change in what can
reach what. If a later phase wants to constrain reach, the lever is which object a
module is handed, not how many classes the store is cut into.

---

## Phase 4 — move one verified duplicated validator

### 4a. Literal duplication

`_result_view_id` is byte-identical in
[run_stage.py](../../src/rcp/transport/run_stage.py) and
[result_views.py](../../src/rcp/runs/result_views.py). Move it to
`rcp.artifacts` beside `ResultViewDescriptor`, name it publicly for what it
validates, and import it from both callers. `runs/result_views.py` already imports
artifact validation from that module, and `transport/run_stage.py` can depend on
the same neutral contract without importing `runs/`.

Do not turn this into a sweep for other similar bodies. This phase moves the one
verified duplicate only.

### 4b. Watcher prose — struck after fact check

The watcher exit contract — exit 1 means work remains, exit 0 means it is gone,
anything else means the check could not answer — is stated in five prose places
and enforced in [watchers.py](../../src/rcp/watchers.py). Leave it as written.

Unlike repository write scope, this is a fixed three-way protocol rather than a
computed policy whose permitted roots change per run. The runtime test already
proves exit `0` is complete, exit `1` is active, and exit `9` is an error;
`tests/test_prompts.py` pins the Experiment prompt's exit-1 meaning. No drift has
been demonstrated, and extracting a renderer would make the prompt harder to
read for no behavioral gain.

---

## Phase 5 — no route body in `app.py`

### The target

`src/rcp/api/app.py` ends as composition only: middleware, lifespan, `app.state`
wiring, and router includes. **No route handler body remains in it.**

Confirmed decision: the whole thing, not the top three groups and not
opportunistic extraction. A half-extracted file is worse than either end state —
two conventions and no rule about which to follow.

### Step zero, before any route moves: the membership gate

**This is the highest-risk step in the handoff. Do it alone, first, and verify it
before touching anything else.**

Every project route is gated by one router-level dependency built inside
`create_app`:

```python
projects_router = APIRouter(dependencies=[Depends(require_project_membership)])
# Exposed so the route-enumeration test can prove membership is attached,
# rather than trusting that every project route was declared in one place.
app.state.project_membership_dependency = require_project_membership
```

Every route added to that router inherits the check. The check answers **"no such
project"** rather than "not allowed," because a refusal that says "not allowed"
would confirm the project exists — the one thing a non-member must not learn.

Routers moving into modules cannot capture that closure. The check must become a
module-level function resolving what it needs from `request.app.state`, and the
include-time wiring must keep it attached to every project router.

**Get this wrong and nothing looks broken.** No error, no crash, no failing test
unless a test is watching. The routes simply start answering everyone, and a
non-member receives project data.

A test is watching: `tests/test_project_membership.py` reads
`app.state.project_membership_dependency` and proves every project route carries
the gate. It must keep passing, and it must keep _seeing_ every project route,
through every later extraction. Its
`test_a_project_scoped_route_declared_outside_the_router_fails_the_route_test`
case already adds a deliberately ungated route and proves the test catches it;
run that test rather than editing application source temporarily.

### The seam

Route handlers currently reach their dependencies as **closure variables** — names
captured from the enclosing `create_app`. That is why they cannot leave the
function. Replace that with module-level `Depends` functions backed by one typed
`ApiServices` container at `app.state.services`.

Measured 2026-08-18 by introspecting the built app:

- Handlers reach **31 distinct closure names**, not the 157 `create_app` defines.
- Five names account for most of it: `catalog` (57 handlers), `store` (49),
  `acting_user` (17), `require_patch_capable_identity` (13), `background_tasks` (12).
- **Transitive** reach per handler — following helper closures they call — has a
  **median of 2 and a maximum of 11**.

No individual handler reaches deeply into the closure graph. This corrects the
audit's broad-entanglement claim, but it does **not** make the extraction purely
mechanical: the shared helper closures still need explicit homes, and extracted
routers may not import them back from `app.py`.

`app.state` already carries `catalog`, `background_tasks`, `setup`, `space_id`,
`space_kind`, and `agent_mode`. The original proposal to add `store`, three
identity callables, and the operation locks is incomplete: the 31 captured names
also include identity/session helpers, attachment and display services, graph
branch projection helpers, the watcher poller, and other composition state.
Do not mirror every closure as another dynamic `app.state` field. Startup instead
constructs one typed `ApiServices` container from a small set of cohesive runtime
objects and stores it at `app.state.services`. Dependency functions expose only
the individual member a handler needs; handlers do not receive the whole
container. Pure helpers move to an owning or shared module and do not become
service fields. `ApiServices` is composition wiring, not a new business layer.

**The rule that makes this worth doing:** an extracted module imports nothing from
`app.py`. If it needs something from there, that something moves too, or moves to a
shared module both import. A router that still imports `app.py` has changed where
code sits without changing what depends on what.

### The tests move with the routes

Confirmed decision. `tests/test_api.py` is 9,051 lines and changed in 27 of the
last 60 commits. Leaving it whole would move the bottleneck rather than remove it:
every route change would still edit one enormous file, and goal 2 would be half
delivered.

Each extraction commit moves a route group **and its tests** into a matching test
module. It is the same mechanical move — you are already reading both sides — and
splitting them into separate commits means reading the same code twice.

### Confirmed baseline module split

These assignments cover the complete current route surface. Placement of
`usage`, `caches`, and `skills` is the least semantically strong part of the
table, but it is not license to change paths or methods: a later sub-split may
change only the editable Phase 0a module map, never the frozen route inventory.

| Module                  | Handlers | Route entries | Contents                                                                                                                                                         |
| ----------------------- | -------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/health.py`         | 1        | 1             | health                                                                                                                                                           |
| `api/team.py`           | 10       | 10            | identity, team enroll/session/invitations/credential/space                                                                                                       |
| `api/index.py`          | 12       | 12            | project list/create/delete, episodes index, space users, project invitations, providers, project setup, global caches, skills                                    |
| `api/project_state.py`  | 15       | 16            | project snapshot, cached, readiness, graph, revision, members, invitations, leave, settings, machine resolve, repository preview, sources, project caches, usage |
| `api/history.py`        | 3        | 3             | history, history summaries, transition manifest                                                                                                                  |
| `api/sync.py`           | 2        | 2             | sync, sync preview                                                                                                                                               |
| `api/tasks.py`          | 9        | 11            | dispatch, list, detail, pause, resume, retry, repair, artifact preview/download                                                                                  |
| `api/chats.py`          | 4        | 4             | chats list/detail, attachments                                                                                                                                   |
| `api/experiments.py`    | 3        | 3             | run, stop, watchers stop                                                                                                                                         |
| `api/watchers.py`       | 3        | 3             | list, check, stop                                                                                                                                                |
| `api/episode_routes.py` | 8        | 9             | episodes CRUD, stop, merge, reauthorize, messages, report preview                                                                                                |
| `api/result_views.py`   | 3        | 4             | list, preview, keep                                                                                                                                              |
| `api/paper.py`          | 4        | 4             | read, create, update, sessions                                                                                                                                   |

This table assigns all 77 distinct handlers and all 82 application route
entries. The five-entry difference is the five stacked `HEAD` routes sharing a
handler with `GET`; nothing is unassigned. Reconcile it mechanically with the
settled Phase 0a inventory before moving code.
`api/episodes.py` already exists holding episode models and serialization — keep
it and put the routes beside it rather than merging.

### Order within the phase

1. **The membership gate**, alone, verified.
2. **`api/paper.py`.** Four routes sharing exactly **one** dependency (`catalog`).
   The cleanest group in the file, and therefore the right place to establish the
   `Depends`-on-`app.state` pattern in a commit reviewable as a pattern rather
   than as a diff.

   **Not `health`.** It looks trivial at one route, but measured it reaches seven
   closure names — `agent_mode`, `catalog`, `default_project_name`, `identity`,
   `space_id`, `space_kind`, `store` — more than most whole groups. It is a bad
   first move.

3. **Leaf groups** — `chats` (2 deps), `history` (2), `watchers` (3),
   `result_views` (3).
4. **Cohesive but deep** — `sync`. Its two handlers share 9 transitive closure
   dependencies and have 11 in their union, so the group is tight even though it
   reaches far.
5. **Large groups** — `tasks`, `episode_routes`.
6. **`team`, `index`, and `project_state` last.** Measured, these three share
   **nothing** across all their routes — they are grab-bags rather than modules.
   `project_state` is defensibly one (it is "everything about one project"); the
   other two may want splitting further once someone has read them. Decide that
   when you get there, not now.

One group per commit, tests included.

### Verification per commit

- Route inventory test: same 86 route entries, only the module column moved.
- `tests/test_project_membership.py` still sees every project route.
- `uv run pytest`, `uv run ruff check src tests`.
- After the groups a view depends on: drive that view in the browser.

---

## Phase 6 — Work-derived task policy moves to its semantic owner

### The problem

`runs/work.py` is 5,207 lines with **88 module-level class/function definitions
plus one private alias**. Only two class/function definitions are public: the
`WorkTurn` dataclass and `stream_work_run`. The other 87 named definitions are
private.

Nothing is trapped inside an enclosing scope, so Phase 6 has no Phase 5-style
closure conversion. It is still not a blind text move: Experiment-loop and
Auto-research-child branches are embedded in otherwise shared Work functions and
must become explicit task entry points without creating a mode-switching runtime.

### The clusters, measured

These were rechecked on 2026-08-18 by rebuilding the file's internal call graph.
Only two original cluster measurements have an unambiguous retained membership
that reproduces directly:

| Cluster                        | Definitions | Reaches out to   | Fact-check result                     |
| ------------------------------ | ----------- | ---------------- | ------------------------------------- |
| Result views                   | 10          | **nothing**      | Reproduced exactly.                   |
| Experiment watcher maintenance | 2           | 1                | Reproduced exactly.                   |
| Prompt composition             | claimed 7   | claimed 2        | Membership list was not retained.     |
| Auto-research child work       | claimed 14  | claimed 1        | Membership list was not retained.     |
| Patch machinery                | claimed ~26 | claimed internal | Membership and rationale invalidated. |

**The former patch-cluster correction is struck.** There is no mutual recursion
in the live internal call graph and no cyclic strongly connected component at
all. `_validate_patch_deliverable` calls `_apply_work_patch` in one direction;
`_reject_patch_deliverable` does not call it. `_validate_work_patch_live` and
`_apply_work_patch` both depend on `_prepare_work_patch_candidate`, but neither
calls the other. A three-file split might still be a poor design, but a circular
import ring is not evidence against it. The exact semantic move ledgers below,
not the invalidated graph cluster, now govern the split.

**The second correction: `_apply_experiment_loop_turn` and
`_stream_work_graph_repair` are orchestration functions, not one-function
modules.** The first moves as part of the broader explicit
`tasks/experiment_loop.py` task path. The ordinary Work repair orchestration
stays with `tasks/work.py`; any Experiment-specific repair branch moves with the
Experiment task path. Do not create files named after either long function.

### The change — confirmed

Do not implement the original five-module order. The confirmed target is
semantic ownership:

- `work.py` keeps ordinary Work-turn orchestration and genuinely shared Work
  execution mechanics.
- The verified result-view lifecycle cluster moves to its existing owning
  module.
- Experiment-loop-specific turn policy moves to an explicit Experiment-loop
  owner.
- Auto-research child-Work mail, contract, command, and restriction policy moves
  to an explicit Auto-research child-Work owner.
- Task dispatch calls distinct ordinary-Work, Experiment-loop-invocation, and
  Auto-research-child-Work entry points instead of asking one
  `stream_work_run` to select the policy.

At the package level, one already-admitted provider execution belongs under
`runs/tasks/`; long-lived episode admission, budget, Stop, binding, child/watcher
coordination, ending, and wrap-up belong under `runs/episodes/`. An
Experiment-loop has both an episode owner and a per-invocation task owner.
Auto-research likewise separates its parent policy from orchestrator, worker,
and child-Work task execution. `background.py` keeps only policy-neutral task
scheduling and lifecycle mechanics.

Shared Work plumbing may be called by those specialized paths, but it must not
become a generic algorithm that selects policy through a `kind`, `surface`, or
equivalent discriminator. Keep the existing `WorkTurn`; do not wrap it in new
runtime and policy context classes. Specialized task modules import only the
small explicit set of Work mechanics they need, while task dispatch selects the
entry point above them, so `tasks/work.py` never imports the specialized modules
back. Generic patch machinery stays in `tasks/work.py` unless extracting an
explicit Experiment branch requires moving a narrower leaf operation.

### Confirmed task package map

| Destination                                    | Source / responsibility                                                                                               |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `runs/tasks/work.py`                           | Current `work.py`; retain `WorkTurn`, ordinary Work entry, graph repair, and shared Work mechanics after policy moves |
| `runs/tasks/experiment_loop.py`                | One admitted Experiment-loop invocation: its prompt, deliverable, correction, Apply, binding, and repair branches     |
| `runs/tasks/experiment_watcher_maintenance.py` | The verified maintenance pair used by an ordinary authorized Work task                                                |
| `runs/tasks/auto_research_child_work.py`       | Child mail staging, child contract, reply command broker, and child-only restrictions                                 |
| `runs/tasks/result_views.py`                   | Existing result-view primitives plus the verified ten-definition Work lifecycle cluster                               |
| `runs/tasks/auto_research_stream.py`           | Mechanical rehome of the current Auto-research orchestrator/worker task executor                                      |
| `runs/tasks/discuss.py`                        | Mechanical rehome of the Discuss task executor                                                                        |
| `runs/tasks/graph.py`                          | Mechanical rehome of Seed/Refresh graph task execution                                                                |
| `runs/tasks/coach.py`                          | Mechanical rehome of the paper-coach task executor                                                                    |
| `runs/tasks/branch_merge.py`                   | Mechanical rehome of `branch_merge_task.py`; the shared semantic merge algorithm stays at `runs/branch_merge.py`      |
| `runs/tasks/episode_report.py`                 | Mechanical rehome of one hidden report task; report admission remains episode-level                                   |

`runs/chat.py`, `runs/patch_validator.py`, `runs/shared.py`, and
`runs/task_policy.py` remain shared roots. Neutral contracts or algorithms such
as `branch_merge.py`, `branch_merge_request.py`, and
`transition_event_reconciliation.py` also remain at the root unless the move
proves they have exactly one lifecycle owner.

### Exact Phase 6 move ledgers

The result-view move is the verified ten-definition cluster:

`_PreparedResultView`, `_result_view_expiry`, `_result_view_task`,
`_preflight_result_view_revision`, `_roll_result_view_retention`,
`_result_view_action_was_settled_by_ancestor`,
`_prepare_result_view_create_slot`, `_prepare_result_view_turn`,
`_record_result_view_rejection`, and `_finalize_result_view_turn`.

The Auto-research child core is these fourteen definitions. This independently
recorded ledger replaces the unsupported original 14-definition cluster claim;
the matching number does not retroactively validate the old grouping:

`_prepare_auto_research_child_work_handoffs`,
`_stage_auto_research_child_work_mail`,
`_auto_research_child_mail_allocation_id`,
`_auto_research_child_work_contract`,
`_compose_auto_research_child_message_wake_prompt`,
`_child_reply_message_id`, `_unused_child_command_id`, `_child_reply_result`,
`_finish_child_reply_command`, `_recorded_child_reply_response`,
`_finish_child_reply_with_retry_attempt`, `_child_reply_matches`,
`_dispatch_auto_research_child_reply`, and
`_serve_auto_research_child_work_mailbox`.

Removing that policy also removes child-specific branches from `WorkTurn`,
`_stage_work_turn`, `_compose_resume_prompt`, `_compose_fresh_prompt`,
`_compose_retry_prompt`, `_apply_work_turn`, `_finalize_work_turn`,
`stream_work_run`, `_start_work_validator_mailbox`, and
`_required_work_continuation_session_id`. The explicit child entry point calls
the shared Work mechanics; do not replace those branches with a child-mode flag.

The Experiment task owner receives `_apply_experiment_loop_turn` and
`_compose_wake_prompt`, plus the Experiment-only branches currently inside
`_prepare_work_prompt_context`, `_compose_resume_prompt`,
`_compose_fresh_prompt`, `_compose_retry_prompt`,
`_read_initial_patch_deliverable`, `_read_initial_watch_deliverable`,
`_validate_watch_deliverable`, `_watch_correction_contract`,
`_reject_watch_deliverable`, `_settle_watch_deliverable`,
`_stream_work_graph_repair`, `_required_work_continuation_session_id`,
`_prepare_work_patch_candidate`, `_apply_work_patch`, and `stream_work_run`.
The ordinary implementations remain explicit; no extracted helper takes
`patch_kind` to choose between them.

Before each move, record the moved definitions' inbound callers and outbound
internal calls in the commit description. That ledger determines the small set
of package-private Work helpers imported by a specialized module; it may not
change the confirmed owners above.

### The trap that would break the product

**Invariants 10 and 10b** forbid a shared helper that knows which conversation
surface it serves:

> no shared helper may take a `kind`, `is_chat`, `surface`, or equivalent
> parameter, because anything that must know which surface it serves is policy and
> belongs in the caller. Leaving some lines duplicated is the correct outcome, not
> a missed cleanup.

A naive split produces exactly the forbidden thing — a `deliverables.py` with a
`mode` parameter serving both Discuss and Work. If a cluster cannot be extracted
without adding such a parameter, **leave it where it is and say so**. Duplication
across the Discuss and Work paths is deliberate.

Also at risk: **10c** (a chat's scratch folder belongs to the conversation, and
clearing the previous turn's `patch.json` fails closed), **10d** (the master
context and per-turn deltas), **10e** (the answer and preview artifacts are
independent, and a result view lives at one stable path in the reused workspace).

Run `tests/test_chat_prompt_protocol.py`, the eight `tests/test_result_view_*.py`
files, and `tests/test_api.py` on every commit here.

---

## Phase 7 — `background.py` keeps only the engine's job

### The problem

`BackgroundAgentTasks` is one class of **3,916 lines and 70 methods**. It is
`app.py`'s problem in class form: you cannot enter it partway, and any two changes
to it collide.

But only part of it is the background engine. Reading the method list, roughly:

| Cluster                | Methods | What it is                                                                                                                                       |
| ---------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Engine                 | ~20     | `start`, `_create_and_spawn`, `_spawn_record`, `_run`, `_consume`, `pause`, `resume`, `retry`, `shutdown`, `repair_graph_update`                 |
| Auto-research dispatch | ~16     | `start_auto_research`, `reserve_auto_research`, `start_auto_research_turn` (175 lines), `_proven_committed_auto_research_dispatches` (155 lines) |
| Auto-research children | ~9      | `start_auto_research_child_work`, `…_child_experiment`, their resume/pause/stop                                                                  |
| Child validation       | 8       | the `_validate_existing_*` block, roughly 450 contiguous lines                                                                                   |
| Experiment loop        | 4       | `_retry_experiment_loop`, `_restart_stopping_experiment_recoveries`                                                                              |
| Watcher notification   | 2       | `start_watcher_notification` (136 lines)                                                                                                         |
| Episode report         | 2       | `start_episode_report`                                                                                                                           |
| Dispatch authority     | 1       | `_resolved_dispatch_authority` (144 lines)                                                                                                       |

Everything below the first row looks like **policy about a particular surface**
that happens to need the engine.

### The correction: exact disposition replaces the unsupported totals

The 2026-08-18 recheck confirmed that surface-specific methods call generic
spawn, run, and recovery plumbing, but the claimed totals of 45 policy methods
and 32 policy-to-engine callers were not reproducible: the classifier had not
been retained. The five-method/12-call sample was also incomplete because
`_create_and_spawn` and `start_branch_merge` call
`_resolved_dispatch_authority`. Those old totals and that sample are evidence
of the original concern, not implementation authority.

The re-review classified every one of the 70 live methods. This is the exact
move ledger:

| Destination/role                   |  Count |
| ---------------------------------- | -----: |
| Policy-neutral background engine   |     24 |
| Auto-research episode policy       |     36 |
| Experiment episode policy          |      4 |
| Common episode/report coordination |      2 |
| Branch-merge task admission        |      1 |
| Watcher lifecycle                  |      2 |
| Shared admission-time task policy  |      1 |
| **Total**                          | **70** |

The exact inventories are:

- **Policy-neutral background engine (24):** `__init__`, `start`, `resume`,
  `retry`, `repair_graph_update`, `pause`, `_signal_agent_task_pause`,
  `shutdown`, `_create_and_spawn`, `_spawn_record`, `_validated_spawn_record`,
  `_record_spawn_dispatch`, `_run`, `_stream_closed`, `_task_settled`,
  `_consume`, `_require_operation`, `_session_is_rcp_owned`, `_retry_feedback`,
  `_failure_is_session_limit`, `_continuation_context_is_unavailable`,
  `_request_from_record`, `_validate_request_type`, and `_forget_control`.
- **Auto-research episode policy (36):** `start_auto_research`,
  `reserve_auto_research`, `reconcile_reserved_auto_research_roots`,
  `_proven_reserved_auto_research_roots`, `_fail_reserved_auto_research_root`,
  `start_auto_research_turn`, `ensure_auto_research_wake_spawned`,
  `reconcile_committed_auto_research_dispatches`,
  `_proven_committed_auto_research_dispatches`,
  `start_auto_research_child_work`,
  `ensure_auto_research_child_work_spawned`, `auto_research_child_work_task`,
  `start_auto_research_child_work_message_wake`,
  `pause_auto_research_child_work`, `stop_auto_research_child_work`,
  `resume_auto_research_child_work`, `start_auto_research_child_experiment`,
  `ensure_auto_research_child_experiment_spawned`,
  `resume_auto_research_child_experiment`, `stop_auto_research`,
  `pending_auto_research_mail`, `_retry_auto_research_task`,
  `pause_auto_research_worker`, `_auto_research_for_request`,
  `_auto_research_parent_episode`, `_exact_child_resume_problem`,
  `_validate_existing_child_work_fresh`,
  `_validate_existing_auto_research_wake`,
  `_validate_existing_child_experiment_fresh`,
  `_validate_existing_child_work_resume`,
  `_validate_existing_child_work_message_wake`,
  `_validate_existing_child_experiment_resume`,
  `_validate_existing_child_experiment_graph_repair`,
  `_validate_existing_child_experiment_watcher_wake`,
  `_auto_research_parent`, and `_auto_research_admission_exhausted`.
- **Experiment episode policy (4):**
  `_restart_stopping_experiment_recoveries`, `_retry_experiment_loop`,
  `_record_bound_experiment_session_limit`, and
  `_preflight_experiment_episode_recovery`.
- **Common episode/report coordination (2):** `start_episode_report` and
  `_restart_interrupted_episode_reports`.
- **Branch-merge task admission (1):** `start_branch_merge`.
- **Watcher lifecycle (2):** `start_watcher_notification` and
  `accept_watcher_notifications`.
- **Shared admission-time task policy (1):**
  `_resolved_dispatch_authority`.

The 24 engine names describe the retained shell, not permission to keep their
current surface branches. In particular:

- `__init__` becomes side-effect-free. It constructs no recovery work and
  launches nothing.
- `start`, `resume`, and `retry` lose episode- and task-kind dispatch.
- `_create_and_spawn` loses Auto-research admission branches.
- `_record_spawn_dispatch` loses surface-specific event rendering; the
  admission owner records the appropriate durable intent before launch.
- `_run` loses Experiment and Auto-research terminal policy.
- `_task_settled` becomes the one generic, ID-only notification after the task
  verdict is committed.

The two watcher methods are themselves mixed. Ordinary Work watcher admission
moves to the existing watcher lifecycle owner; Experiment watcher admission
and recovery move to the Experiment episode owner. Their common queue/storage
mechanics may remain shared, but no helper selects policy by watcher kind.

### The change — confirmed

`BackgroundAgentTasks` becomes a policy-neutral executor for already-admitted
tasks. Do not introduce the rejected five-profile registry, a callback registry,
or mixins. The closed set of product policies has explicit owners and ordinary
imports:

- `src/rcp/runs/episodes/reconcile.py` contains the moved `EpisodeReconciler`,
  the thin server-owned coordinator for settlement and startup reconciliation.
  It accepts an `operation_id`, reloads durable state, and explicitly calls the
  Auto-research or Experiment policy.
- `runs/episodes/auto_research/` owns Auto-research admission, recovery, Stop,
  session/branch binding, children, mail, and child-Experiment coordination.
  Existing Auto-research files move mechanically into that package; the
  per-invocation orchestrator and worker executor move to
  `runs/tasks/auto_research_stream.py`.
- `runs/episodes/experiment_loop.py` and its admission helper own the bounded
  Experiment parent, recovery, retry, Stop, session binding, and watcher
  coordination. `runs/tasks/experiment_loop.py` owns one admitted invocation.
- `runs/episodes/wrapup.py` owns common ending and report admission;
  `runs/tasks/episode_report.py` executes one admitted report task.
- `runs/episodes/membership_fence.py` remains episode-level policy.
- `runs/tasks/branch_merge.py` owns branch-merge task admission/execution while
  the neutral merge algorithm remains `runs/branch_merge.py`.
- The ordinary watcher owner handles ordinary watcher admission and calls the
  engine only after admission. Episode-specific watcher paths delegate to their
  episode owner.
- `runs/task_policy.py` resolves dispatch authority at admission. Admission
  persists the resolved authority with the exact launch intent; launch merely
  validates that binding. An episode report therefore reaches the engine with
  no graph authority rather than relying on the engine to rediscover that rule.

The engine exposes `launch_admitted(operation_id)`. Admission must atomically
store enough information to launch without reconstructing policy from the
request:

- a missing id raises `KeyError` and writes nothing;
- a valid queued record launches;
- a duplicate launch for an in-process or post-queued record returns its
  current durable state without starting a second worker; and
- malformed or inconsistent durable launch data fails loudly before spawning.

Extracted task API routes call the semantic owner before the engine. In
particular, Resume and Retry route episode-linked tasks through
`EpisodeReconciler`; ordinary tasks go directly to `BackgroundAgentTasks`.
Do not add a `TaskController` whose only job is to recreate the same kind switch.

Startup ordering is explicit and occurs before the server accepts requests:

1. construct the side-effect-free engine and episode coordinator;
2. let the coordinator identify operation ids whose episode recovery must be
   preserved;
3. run the engine's generic interrupt/recovery pass with those ids protected;
4. run episode reconciliation and launch any durably admitted work.

Task completion commits its generic verdict first, then notifies the coordinator
with only `operation_id`. A crash before notification is repaired by the same
idempotent startup reconciliation. There is no second continuously running or
persisted "episode manager" task.

Extract behavior unchanged first. Cleanup and deduplication come later, in
separate commits, if at all. **Do this phase after Phase 6:** that phase creates
the task/episode package boundary this move targets.

### Invariants at risk

- **Invariant 8** — background seed/refresh is server-owned; a live run can be
  paused, a checkpointed attempt resumed, and a paused/interrupted/failed attempt
  retried. Pause and resume are parent→child across a task chain, not one operation
  id.
- **Invariant 10g** — one episode parent, one native session, one graceful stop;
  the session binding is proved before the atomic claim and nothing ever falls back
  to a fresh session silently.

Both live in the clusters being moved. Run `tests/test_background.py`,
`tests/test_auto_research_recovery.py`, `tests/test_experiment_stop.py`, and
`tests/test_episode_lifecycle_acceptance.py` on every commit in this phase.

---

## Out of scope

Say plainly if any of these become tempting; do not fold them in.

- Splitting `AppStore` into narrow interfaces (Phase 3 explains why).
- Turning `BackgroundAgentTasks` into mixins (Phase 7 explains why).
- Renaming, comment coverage, or docstring polish justified only by readability.
- Anything in `web/` beyond keeping it working. This is backend structure.
- Behavior changes other than the one named in Phase 1.
- Adding a type checker, a coverage gate, or new lint rules.
- **`src/rcp/service.py`** — 2,116 lines, 20 of the last 60 commits. Considered and
  deliberately excluded, 2026-08-18.

  It is the smallest of the four hot files by a wide margin — `ProjectService` is
  1,520 lines across 42 non-constructor methods, about a third the size of
  `BackgroundAgentTasks`.
  More importantly its problem is a different shape. The file is mostly ordinary:
  twenty small data definitions and a class whose methods are normal sizes, except
  **`_build_sync_patches` at 362 lines** — a quarter of the entire class in one
  method.

  "This file is one lock nobody can enter partway" is defensible for the other
  three and is not defensible for this one. Breaking up one oversized method is a
  different job from splitting a file, and folding it in would make this handoff
  say "and also do this unrelated thing."

  Fix `_build_sync_patches` on the day someone next has reason to touch Sync.

## Definition of done

- All eight phases complete.
- `uv run pytest`, `uv run ruff check src tests`,
  `npm --prefix web run build`, `npm --prefix web test` green.
- `uv run pre-commit run --all-files` green, **plus**
  `uv run pre-commit run --files <new paths>` for every file this work adds.
  `--all-files` covers tracked files only and will not see them; that gap has bitten
  this repository before.
- The amended S10 lifecycle assertions and applicable browser path pass.
- The end-of-session sweep run over `pending` and `blocked-external` scenarios.
- No route handler body remains in `src/rcp/api/app.py`.
- `BackgroundAgentTasks` contains only the 24 policy-neutral engine shells in
  the Phase 7 ledger, with their surface-specific branches removed.
- `runs/tasks/` contains one-invocation executors and `runs/episodes/` contains
  parent lifecycle policy; no mode-switching runtime, policy registry, mixins,
  or content-free common package was introduced.
- `runs/tasks/work.py` holds `WorkTurn`, the ordinary-Work entry point, and
  genuinely shared Work mechanics only. Experiment-loop, result-view, and
  Auto-research-child policy live with their named owners.
- App startup follows the confirmed coordinator-preserve, generic-recovery,
  episode-reconciliation order before requests are accepted.

## Documentation to close

- **`docs/specs/projects-spaces-and-operations.md`** — agent-task lifecycle
  semantics change in Phase 1 (refusal is now recorded).
- **`docs/specs/conversations-episodes-and-watchers.md`** — check against Phases 6
  and 7; update if it describes where this behavior lives.
- **`docs/specs/api-web-and-desktop-projections.md`** — check whether it describes
  where routes live.
- **`AGENTS.md`** — the fan-out table's Service/API row names `app.py` as one area,
  and Run orchestration names `runs/`. Both change. Add any repeated failure this
  work uncovers.
- **This handoff** — archive to `docs/archive/handoffs/` when the last phase lands.
- **`rcp_architecture_audit.md`** — leave in place. It is the evidence and the
  explanation.

## Where this handoff disagrees with the audit

1. **The audit's "missing files" findings are false.** Artifacts of a broken
   extraction. All files are present and 2,204 tests collect.
2. **The audit implies route handlers reach broadly into `create_app`.** Measured,
   median transitive reach is 2 and maximum is 11.
3. **The audit frames the store's atomicity gap as systemic.** The first attempt
   to narrow it to zero nested writers and 19 outside callers did not survive
   live-tree verification. Phase 3 now requires a post-Phase-1 candidate audit
   based on harmful partial states, not raw call counts.
4. **The audit recommends narrowing `AppStore`.** Rejected — see Phase 3.
5. **The audit treats `app.py` as the structural problem.** It is one of four.
   `work.py` is larger and `test_api.py` is larger still.
6. **The audit's P0 recommendations are struck**, as already marked in that
   document.

## Appendix — measurements

All taken 2026-08-18 against the working tree. Reproduce before trusting any of
them in a later session; they will drift.

| Measurement                             | Value                                                                                                                       |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Pytest collection                       | 2,204 tests across 113 files                                                                                                |
| Routes                                  | 86 route entries — 82 in `app.py`, 4 from FastAPI; 90 expanded method/path pairs                                            |
| Project-scoped routes                   | 60 recursively; 0 in a flat `app.routes` walk                                                                               |
| OpenAPI                                 | 82 application operations; generated routes omitted                                                                         |
| `create_app`                            | lines 333–3278, 157 local names, 99 direct child functions                                                                  |
| Closure names reached by handlers       | 31 distinct; median transitive reach 2, max 11                                                                              |
| `BackgroundAgentTasks`                  | 3,916 lines, 70 methods                                                                                                     |
| `runs/work.py`                          | 88 module-level classes/functions plus 1 private alias; 2 public definitions                                                |
| `AppStore`                              | 242 public callable members, 10 mixin modules, referenced from 22 `src/rcp` files                                           |
| Transaction candidates                  | re-audit required after Phase 1; old 0/19 measurements withdrawn                                                            |
| Status-writer call sites                | 22 in `src/`, 164 in `tests/`                                                                                               |
| `lost its …` guards                     | 66 across 19 files; not all lifecycle-related                                                                               |
| Duplicate `_result_view_id`             | 2 copies, byte-identical                                                                                                    |
| Watcher exit contract                   | 5 prose statements, 1 enforcement                                                                                           |
| Broken lifecycle operation/status cases | 22 of 49; plus 4 illegal completion output deletions                                                                        |
| `work.py` internal call graph           | no cyclic strongly connected component; former patch recursion claim withdrawn                                              |
| `work.py` result-view cluster           | 10 definitions, reaches out to nothing                                                                                      |
| `background.py` method disposition      | 70 exact: 24 engine, 36 Auto-research, 4 Experiment, 2 common episode/report, 1 branch merge, 2 watcher, 1 shared authority |
| Route group cohesion                    | `paper` 1 shared dep; `health` reaches 7; Sync shares 9 of 11 transitive deps                                               |
| `ProjectService`                        | 43 methods including `__init__` (42 excluding it), 1,520 lines; `_build_sync_patches` 362                                   |
