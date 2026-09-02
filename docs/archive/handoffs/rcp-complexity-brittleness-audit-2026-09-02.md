# RCP complexity and brittleness audit

- Date: 2026-09-02
- Audited commit: `5e8a4f889c7988b345178ba68f143f05b3e8150d`
- Status: point-in-time review. This report is archived evidence, not current
  product authority and not an implementation handoff.

## Disposition (2026-09-02)

The human accepted findings 1, 4, 5, 6, and 7; they are carried by the
[complexity audit remediation handoff](handoff-2026-09-02-complexity-audit-remediation.md).
Findings 2 and 3 are carried by https://github.com/Zhi0467/RCP/pull/12.

## Conclusion

RCP is not generally over-abstracted. Its graph authority, append-only history,
typed Patch channel, transition closure, and backend-owned projections are
coherent responses to real product requirements. The unnecessary complexity is
concentrated elsewhere: the product is operating a bespoke source-deployment
platform before its first team deployment is closed, while three code seams now
coordinate multiple independent state machines by shared local state:
`create_app`, the root React `App`, and storage initialization.

Those three seams meet the repository's own threshold for structural work. They
have independent lifecycle requirements, block focused testing, and have already
produced regressions at their boundaries. The right response is not another
generic controller, plugin system, event bus, global frontend store, or split
SQLite service. It is a small number of concrete owners for state that already
has to commit together.

## Scope and standard

This review read the current design and specifications, the active first-team
server handoff, the structural-refactor closure decision, the code under
`src/rcp` and `web/src`, and the prior codebase-audit remediation and independent
verification. The prior High and Medium findings are treated as closed; they are
used only as evidence about which seams repeatedly generate faults.

A large file is not a finding by itself. Complexity is called brittle here only
when at least one of these is true:

1. one lifecycle fact must be coordinated through several nominal owners;
2. the code contains a lifecycle that can start, stop, recover, or fail
   independently but has no owner of its own;
3. recent defects occurred because two parts of the seam observed different
   state; or
4. product surface is growing faster than end-to-end evidence for the existing
   surface.

This was a read-only source review. I did not run the application, tests, live
server, desktop client, or migration fixtures. The findings below are structural
risk findings, not claims that the audited commit currently fails at runtime.

## Ranked findings

| Priority | Finding | Required direction |
| --- | --- | --- |
| P1 | Team/server surface is ahead of reference-deployment closure | Freeze new team/server state machines until the current lab closure drive passes |
| P1 | Source update and restore form a second application control plane | Move deployment ownership outside the ordinary RCP application process; prefer immutable versioned artifacts |
| P1 | `create_app` now owns several independent runtimes | Extract only the installed-service lifecycle as a concrete owner, not a generic application controller |
| P1 | `App.tsx` is a distributed project-session transaction coordinator | Put snapshot, draft, head, request-generation, and tab-cache reconciliation in one reducer-owned session boundary |
| P1 | Storage startup mixes versioned migrations with shape-driven mutation | Make every post-baseline schema or data change an ordered migration and validate the resulting schema separately |
| P2 | `BackgroundAgentTasks` has a growing policy-exception budget | Stop adding task-kind branches without removing or isolating an existing exception; pin the matrix behaviorally |
| P2 | Documentation authority is correct but saturated | Add no new global invariant without consolidating an existing rule and naming executable evidence |

## P1. Team/server surface is ahead of closure evidence

The core product can be evaluated in personal mode: one research graph, human
approval, agent conversations, bounded experiments, artifacts, and local or SSH
execution. Team mode adds a separate infrastructure product: enrollment,
membership, central checkouts, provider readiness by operating-system account,
project provisioning, personal-to-team transfer, backup, restore, member
removal, desktop switching, source install, source update, candidate rehearsal,
rollback, and recovery.

The [active first-team server handoff](../../handoffs/handoff-2026-08-27-dev-team-space-and-server.md)
still lists integrated two-member desktop switching, first task, SSH execution,
backup/restore, transfer, and final one-lab closure drives as open. At the same
time, those paths already constrain ordinary application startup, storage,
identity, routing, desktop compatibility, and release engineering. Every new
research feature therefore acquires personal, team, desktop, server, remote,
backup, restore, and upgrade compatibility questions before the first complete
team reference deployment exists.

This is a sequencing problem, not an argument to delete team mode. Until the
current handoff closes:

- add no new team/server lifecycle, transfer phase, privileged operation, or
  desktop/server protocol surface;
- allow work needed to close the existing drives and correct defects they expose;
- keep new research-control features out of the team infrastructure path unless
  they are necessary for that closure.

The observable exit is the existing handoff's archive condition, not a new
internal milestone invented by this report.

## P1. Source update and restore form a second application control plane

The accepted [deployment-channel decision](../../decisions/2026-08-27-main-is-the-server-update-channel.md)
makes `origin/main` a live server release channel. The implementation goes
further than a normal updater: the old application release builds and rehearses
the new release, coordinates maintenance through a private protocol, switches
the service, verifies the replacement, releases startup-effect fences, and may
restore or roll back application data.

The control plane spans, among other files:

- [`server_ops/control.py`](../../../src/rcp/server_ops/control.py), whose strict
  request type carries sixteen operations and a cross-release protocol window;
- `server_ops/update.py`, `update_checkpoint.py`, and `update_cutover.py`;
- `server_ops/restore.py` and the backup family; and
- [`api/app.py`](../../../src/rcp/api/app.py), where the ordinary FastAPI process
  validates restore journals, owns update and restore admission gates, serves the
  private control socket, commits replacement activation, and resumes deferred
  startup.

The [prior remediation audit](handoff-2026-09-01-codebase-audit-remediation.md)
found severe update and restore failures, and its
[independent verification](handoff-2026-09-01-codebase-audit-remediation-verification.md)
found a new restore-activation lock regression in the attempted correction. All
are now fixed. Their recurrence is evidence that an application process owning
its own replacement is a fragile boundary.

After the first lab closure, invert this design:

1. a small external supervisor, invoked by the privileged CLI, owns checkout or
   artifact acquisition, build, stop/start, cutover, rollback, and restore;
2. the RCP application exposes only bounded health, version, identity, migration,
   and readback facts;
3. prefer immutable, versioned release artifacts over building arbitrary
   `origin/main` commits on the server; and
4. if source builds remain a requirement, keep the build but still move its
   orchestration outside the application process.

This removes the old-release/new-release private application protocol from the
normal runtime. It does not remove systemd, rollback, restore validation, or the
requirement that every supported persistence boundary upgrade directly.

## P1. `create_app` now owns several independent runtimes

The active [structural-refactor closure decision](../../decisions/2026-08-20-backend-structural-refactor-closure.md)
correctly rejects extraction from `api/app.py` based on file size alone. It says
to revisit the boundary after a measured maintenance collision, an independent
lifecycle requirement, or a concrete testing problem. Those conditions now
exist.

`create_app` begins at [`api/app.py:377`](../../../src/rcp/api/app.py) and runs for
roughly 1,700 lines. In one lexical scope it owns:

- installed update and restore startup boundaries;
- runtime and background admission gates;
- restore activation journal commit and deferred-start recovery;
- a sixteen-operation private server-control dispatcher;
- provider, provisioning, transfer, backup, and member-removal coordinators;
- all agent task stream dispatch;
- Auto-research child and episode reconciliation;
- watcher delivery, retry, and poll-time reconciliation;
- startup and shutdown ordering; and
- FastAPI middleware, exception handlers, routers, and static assets.

The problem is not that these objects are composed together. The problem is that
replacement activation, control-socket lifetime, deferred startup, and ordinary
application lifetime are mutated through nested closures and shared local
variables. The prior verification's restore-lock failure was hard to test because
the activation helper was exercised outside the enclosing admission lifecycle.
That is exactly the testing threshold the decision record names.

The bounded extraction is an `InstalledServiceRuntime` owner, or equivalent
concrete name, containing only:

- update/restore boundary discovery;
- its two admission gates and startup-effect fence;
- private control-server construction and dispatch;
- replacement activation commit; and
- the start, release, timeout, and stop hooks for that runtime.

`create_app` should receive the resulting gates, services, and lifespan hooks and
continue to compose routes, background tasks, and watcher runtime explicitly.
Do not create a `RunDispatcher`, `StartupReconciler`, generic controller,
registry, callback bus, or application facade. A successful extraction is proven
when update/restore activation can be driven end to end without constructing a
FastAPI application, and `create_app` no longer reads or writes update/restore
journals or matches private control operations.

## P1. `App.tsx` is a distributed project-session transaction coordinator

[`web/src/App.tsx`](../../../web/src/App.tsx) is about 152 KB. Its size is again
not the finding. The root component owns a project-session state machine that is
split among React state, refs, several hooks, browser storage, tab caches, and
backend snapshots.

The key evidence is the transaction shape:

- `CachedProjectTabState` combines project, history, task, chat, graph-selection,
  draft, usage, watcher, transition-head, ruleset, manifest, and preview state;
- `restoreProjectTabState` restores one cached tab by calling many independent
  hook actions and setters;
- `applyProjectSnapshot` arbitrates request freshness, revision monotonicity,
  transition heads, manifest invalidation, draft rebase, local persistence,
  graph replacement, and chat reconciliation;
- `reload` separately fetches project, tasks, usage, watchers, and chats; and
- `heartbeatProjectCache` contains different reconciliation paths for active,
  inactive, missing, unreadable, and newly reactivated tabs.

The design invariant says the browser renders one revision at a time. The current
component enforces that invariant procedurally across many mutable locations.
The resolved frontend races in the prior audit and verification, especially the
attention-membership mismatch and autosave/poll generations, show the cost of
that arrangement.

Create one project-session reducer/hook that owns only the state which must
change coherently:

- canonical project snapshot and rendered revision;
- transition head, ruleset, manifest, and draft preview;
- human draft and its base revision;
- request generation/freshness; and
- tab-cache serialization and restoration.

Fetch effects may remain ordinary hooks, and chats, tasks, history, graph
selection, and visual components should retain their existing concrete owners.
They should submit events to the project-session owner rather than participate in
its commit. Do not introduce Redux, a generic global store, or another client-side
projection of backend lifecycle rules.

The boundary is complete when snapshot application and tab restoration each
become one reducer transition, their serialization/restore pair has behavioral
round-trip tests, and `App` no longer commits one project revision through a
sequence of unrelated setters.

## P1. Storage startup mixes versioned migrations with shape-driven mutation

Keeping one SQLite file and one public `AppStore` is reasonable. The brittleness
is inside [`AppStoreBase._initialize`](../../../src/rcp/storage/base.py), which
starts at `storage/base.py:168` and ends near line 1,615.

The code now has a useful `storage_schema_migrations` ledger and runs migrations
2 through 4 through `_run_storage_schema_migration`. That improvement coexists
with a second migration system embedded in startup:

- table- and column-existence checks decide whether old data needs repair;
- booleans such as `members_table_existed` act as one-time migration markers;
- current DDL, trigger replacement, index replacement, legacy detection,
  semantic backfills, and bootstrap recovery share one transaction body; and
- helpers such as `_ensure_column`, `_allow_consumed_project_transfer_uploads`,
  `_migrate_project_invitation_revocation`, and
  `_relax_episode_wrapup_ending` perform additional shape-driven changes.

A new database, an old database, a partially initialized team space, and every
historical schema therefore enter the same large interpreter. The registry makes
some expensive migrations once-only, but the ordering contract still spans both
version numbers and inferred database shape.

Complete the direction already started:

1. define one baseline schema for a new database;
2. route every post-baseline schema or semantic data change through the ordered,
   named migration ledger;
3. permit shape inspection only inside the migration that owns that historical
   shape;
4. after migration, run a read-only schema/invariant validator; and
5. retain frozen historical database and backup fixtures for each persistence
   era that the server promises to upgrade.

Do not introduce an ORM, split the SQLite file, or create independent store
services. The measurable result is that ordinary startup performs no schema
mutation outside new-database creation and the migration runner, and that a
migration's identity and order are explicit rather than inferred from whatever
columns happen to exist.

## P2. `BackgroundAgentTasks` has a growing policy-exception budget

The accepted design deliberately keeps [`BackgroundAgentTasks`](../../../src/rcp/background.py)
as the common launch/runtime engine and permits honest named calls to concrete
owners. That coupling is not itself a defect.

The watchpoint is that `recover_at_startup`, `start`, `resume`, and `retry` now
know special rules for Auto-research, branch merge, episode report, Experiment
loop, graph repair, result-view revision, ingestion, native-session ownership,
provider session limits, stage reuse, and execution-host pinning. A new task
surface can easily require changes in several of those methods plus its concrete
owner, producing an implicit kind-by-continuation-by-episode matrix.

Do not refactor this into a registry or plugin API now. Instead:

- add no new `kind`, `patch_kind`, or request-subtype branch to the engine unless
  the change removes an existing exception or proves that the rule belongs to
  universal task-row construction;
- maintain one table-driven behavioral matrix for Start, Resume, Retry, recovery,
  and graph repair across the existing task families; and
- when the next genuine exception is required, move one complete policy decision
  to its concrete owner rather than adding a callback facade.

The trigger for structural work is a feature that must edit three or more engine
entry points, not the current named imports or file size.

## P2. Documentation authority is correct but capacity is finite

RCP depends on documentation more than an ordinary application because coding
agents consume it as an authority system. The hierarchy is clear, archived work
is separated, and the prior drift findings were corrected. The remaining
brittleness is finite capacity, not exhaustion: [`AGENTS.md`](../../../AGENTS.md)
is 209 lines at the audited commit, inside its 180–220-line target band and with
about twenty lines of headroom to the enforced 230-line hard ceiling, while the
same cross-cutting promises also appear at coarser grain in `design.md` and are
referenced by specifications, acceptance scenarios, decisions, source comments,
and tests.

Do not add another documentation layer or generated policy registry. Apply a
stricter admission rule instead: a new global invariant must replace or
consolidate an existing global rule, name its concrete code owner, and cite an
executable test or acceptance scenario. Module behavior stays in its
specification. Failure history stays archived. This keeps the authority packet
small enough to be read rather than merely present.

## Complex mechanisms that should remain

The following are substantial but justified by current requirements and should
not be simplified merely to reduce file count:

- one append-only canonical Patch history with deterministic replay;
- one typed graph-change channel and one transition owner;
- graph-only Auto-research branches with human-dispatched semantic merge;
- backend-owned lifecycle and transition projections;
- one SQLite file and one public `AppStore` surface;
- exact write containment and fail-closed authority checks;
- self-contained source shipped to remote execution helpers; and
- explicit named coupling instead of a plugin framework or event bus.

The canonical-repository/SQLite boundary is an unavoidable consistency tax under
the current portability design. Treat it as a scarce boundary: new features
should not add another reconciliation direction or another source of canonical
truth.

## Proposed sequence if the findings are accepted

1. Close and archive the existing first-team-server handoff; freeze new server
   surface until then.
2. Decide whether deployment moves to an external supervisor and immutable
   release artifacts. Record that product decision before restructuring code.
3. Finish the storage migration ledger and frozen historical-fixture matrix,
   because every deployment design depends on a trustworthy upgrade boundary.
4. Extract the installed-service runtime from `create_app` along the chosen
   deployment direction.
5. Introduce the reducer-owned frontend project-session boundary with behavioral
   race and tab-round-trip tests.
6. Add guardrail tests for the background-engine exception matrix and global
   invariant ownership; do not pre-emptively redesign either subsystem.

Do not implement these as one refactor. Each accepted item should become its own
human-confirmed handoff with a concrete behavior-preservation boundary and an
independent closure condition. Rejected items should be recorded as decisions so
the same file-size arguments are not repeatedly re-litigated.
