# Release updates and rollbacks stay clean

Date: 2026-09-24. Status: slice 2 implemented; online maintenance entrance unchanged.
Supervisor 0.1.6 captures every entry of each replacement root at the stopped
boundary before old preparation, cross-checks application-owned root discovery,
and proves the restored live tree before old startup. Recovery journals snapshot
creation separately from preparation and verifies old semantics on a disposable
copy. Journal-owned quarantines relocate only after rollback and complete
post-rollback backup, then expire with their retained checkpoint; legacy
quarantines remain available for inspection. Regression coverage includes
credentials, jobs, watermarks, empty directories and interrupted restoration.
The baseline already supplied explicit projection upgrades, old-shape cache
decoding, reconstruction on revision mismatch and the larger graph-read bound.
Remaining: slice 3 sign-in quiescence, probation and branch/operational proofs;
the unresolved legacy entrance and source floor; operator diagnostics and
installed-artifact qualification using an unmodified old wheel and old prepare.

Settled requirements: preserve append-only history, restore every entry in a
replaced root, preserve credentials and failed-run scratch, keep the supervisor
independent, introduce no configuration knobs, and retain all schema-era
fixtures. Recommended ownership and compatibility tradeoffs are in the
[decision](../decisions/2026-09-24-update-rollback-preserves-stopped-trees.md).
The bootstrap procedure and missing historical artifact inventory are review
items, not silently accepted assumptions. Slice 2 introduces no legacy online
refusal and makes no claim that installed-machine qualification has passed.

Closure: all five slices below are merged through normal PR review; required CI
proves every supported release source, exact rollback and complete subsequent
backup, ordinary startup/continuation, and local plus real SSH state; the changed
recovery boundaries pass disposable Ubuntu qualification. Update the current
specification and operator guide with each behavior change. Delete this handoff
when those conditions hold. The existing
[reboot qualification handoff](handoff-2026-09-06-disposable-supervisor-qualification.md)
still owns its unfinished broader adoption/restore cases; this work does not
claim those passed or authorize production fault injection.

## Decisions the human still owns

Implementation started 2026-09-24 on the parts below that do not depend on
these. Each needs an explicit human answer before its dependent work lands:

1. **Legacy first update.** Whether a running source that cannot prove sign-in
   quiescence is refused (`legacy_quiescence_unproven`) until an operator stops
   it, or updated online as today with the race documented.
2. **Supported source floor.** Only v0.3.24 onward are published releases.
   Whether to declare that as the floor, or recover older assets (v0.3.5 is the
   documented install boundary) first.
3. **Installed-artifact CI.** Whether the systemd/SSH transaction job (slice 5)
   becomes a required PR check, and its runner budget.

Started without waiting: exact stopped-tree rollback and its restore proof
(slice 2, online entrance unchanged), refusal-cause forwarding (slice 1), the
recursive projection-default invariant (from slice 3), and a release gate that
covers every published release instead of a fixed count (interim for slice 4).

## Verified diagnosis and current coverage

The supplied incident reports field-default refusal, old-cache refusal, and
loss of live entries on rollback. The code explains all three mechanisms. This
run did not access live server data or independently verify the reported manual
recovery. The supplied audit was against an earlier commit; use the findings
below rather than carrying its claims forward unchanged.

| Finding | Current evidence and qualification |
| --- | --- |
| Incomplete rollback was accepted as complete | Slice 2 now seals a supervisor-owned full stopped-tree snapshot before `deployment.prepare` builds its selected semantic payload. Restoration independently rescans every live root; credentials, jobs, watermarks and empty directories are part of that proof. |
| Stale/old-shape cache and graph size | `upgrade_graph_projection` is used by deployment and the cache loader. `verify_live_application` reconstructs when revision differs. `_upgrade_previous_projection` now uses `PROJECT_DISPLAY_SNAPSHOT_MAX_BYTES`. The earlier 4 MiB diagnosis is resolved; the current test includes a graph larger than 4 MiB. |
| Reconstruction does not prove the served cache was repaired | Live verification uses the graph returned by `catalog.open_snapshot`. The recent-release test still expects a stale stored cache to remain stale afterward. It proves the verifier can reconstruct, not that the next served snapshot is current. |
| Quiescence is incomplete | `MaintenanceCoordinator.enter` drains admission, `background.runtime_is_idle`, and runtime pollers. `ProviderSignInRunner.start_sign_in` creates a daemon thread outside that idle set. It can wait for the credential gate before persisting the in-progress marker. SQLite alone cannot prove no pending sign-in. Active transfer uploads already refuse maintenance. |
| Probation can write beyond its intended boundary | `create_app` opens/migrates `AppStore`, resets missing credentials and settles interrupted sign-ins before deferred lifespan startup. Missing-cache verification can reach `HistoryManager.initialize`, including `StateWorkspace.publish`; remote publication is not protected by local rollback. Local SQLite migration inside a complete checkpoint is acceptable; external or canonical publication before choice is not. |
| Semantic proof is incomplete | `CandidateProjectVerification` has one main graph digest/revision; branch heads and merge receipts are captured by backup but not compared in update's read model. `StartupRecoveryReadModel` contains five ID lists. Successful task/watcher reads do not prove continuation, episode authority, or job receipts. |
| Ordinary startup is outside the recent-release gate | That test creates the candidate app without entering its lifespan, and calls validation directly in pytest. `candidate_chosen` precedes ordinary recovery, watchers and compute reconciliation. Failure there preserves candidate data and may leave the service unavailable. |
| Diagnostics disappear | `SystemRuntime.control` collapses authenticated `ok=false` to a generic refusal. `backup_project_files._capture_or_preserve_failure` retains some inventory/remote reasons but replaces most local exceptions with a generic capture failure. Probe readiness retries discard the last error and log path. |
| Installed-machine coverage is separate and incomplete | The recent-release harness builds old source with development dependencies and placeholder Web assets, not promoted wheels. Current Linux VM qualification is manually dispatched and uses synthetic bundles. Wrapper/identity/backup/stop/reboot checks elsewhere do not make this a required promoted-artifact transaction gate. |

Slice 2's order is install target, protected backup, maintenance enter/capture,
service stop, read-only root discovery and sealed stopped-tree checkpoint,
old-release prepare, candidate copied validation, pointer switch, fenced probe,
durable candidate choice, ordinary startup. Preparation can settle artifact
replacements only after the full snapshot exists. Remaining recommendations
below retain the unresolved entrance and installed qualification decisions.

## Exact rollback contract and ownership

Let B be the stopped boundary after admission closes and entered work settles,
before old preparation or candidate code may mutate live state. Let R be the
restored boundary before old ordinary startup. For every replacement root,
`inventory(R) == inventory(B)`. Do not compare with prepared payload inventory.
Work completed while draining belongs to B; this does not rewind work accepted
before maintenance. Stronger admission proof remains part of the unresolved
legacy entrance and slice 3; slice 2 preserves today's online entrance.

The application owns root discovery from the captured project registry and
manifest configuration. Add a small read-only inventory operation in
`server_ops/deployment.py`, with helpers in the existing snapshot module. It
reads a disposable copy of the stopped SQLite files, supports each admitted old
schema, and returns the data root plus every registered local canonical
`.research` root, local/remote identities, and typed external references. It
must not construct a live `AppStore`, initialize a graph, settle artifacts or
publish a workspace. Cross-check its project set with the captured registry;
uncaptured or unresolvable roots refuse. No filesystem-wide discovery scan.

The supervisor knows no graph or database schema. Extend `checkpoint.py` and
`fs_worker.py`'s existing opaque stopped snapshot to multiple roots, using that
root description. Root discovery is application policy; copying every entry
under each root is supervisor policy. The old release still provides its
semantic baseline/prepare result, but its `payload` is never the update rollback
source. Compare its declared roots with the discovered roots and refuse an
unexplained difference. Backup policy still governs the prerequisite protected
backup, independently of rollback inventory.

The snapshot retains the entire tree: credentials, jobs, caches, inbox entries,
empty directories, unknown regular files, watermarks, all branches, and scratch.
Record absence as well as presence for any optional root the operation can
create. Inventory the root itself and each relative entry using `lstat`: type,
regular-file size and SHA-256, symlink text where allowed, owner/group and modes
needed to restore access. The existing copier normalizes directories to 0700;
change it to preserve admitted directory modes, or refuse unsupported metadata
before capture. Do not silently lose ACLs/xattrs or hardlink relationships:
initial support may refuse them by name. Timestamps/inodes are not the equality
contract; required access metadata and bytes are.

Never follow symlinks; retain the existing scratch-link policy. Unsafe links
elsewhere, sockets/FIFOs/devices, foreign ownership, mutable sources, overlapping
roots and exhausted entry/byte/disk bounds refuse. Ordinary lock/metadata files
are included as stopped bytes. Their contents never establish ownership.
Keep the external deployment lock and startup guard throughout root swaps;
verify no app PID, helper writer or independent lock holder remains. Do not hold
a lock on an inode that the restore replaces and mistake it for the new root's
lock. Reacquire required path locks before releasing the outer guard.

SQLite's stopped main/WAL/SHM/journal entry set is copied exactly; interpretation
and checkpointing occur only on a disposable copy. A coherent backup SQLite
snapshot is useful for semantic comparison but cannot replace these raw bytes
in an exact-tree checkpoint. Hash before and after copying; seal and fsync the
manifest before preparation, validation, pointer changes or candidate startup.
Private roots containing tokens require private payload storage and redacted
diagnostics. Never publish payloads or credential hashes in CI artifacts.

### Transaction and rollback sequence

1. Validate source/target identities and capabilities, protected backup and
   current operation state. Close and drain all admitted owners, prove stopped
   writers, discover roots read-only, then seal the full stopped-tree checkpoint.
   Journal snapshot creation separately from semantic preparation. Before the
   seal there is no authority to run a live-mutating worker.
2. Obtain the old semantic baseline and run candidate migration/replay/API
   rehearsal on disposable copies. Authenticate the old graph bytes before
   applying explicit projection upgrades. All local preparation writes are now
   recoverable. Confine legacy workers to the existing disposable overlay when
   their owners could publish remotely: give them captured history and remapped
   paths, not live remote write access. Block remote publication and provider
   actions; compare canonical hashes and external sentinels around rehearsal
   and probation. If this cannot be proved for a legacy SSH source, refuse with
   `legacy_remote_probation_unproven`; this remains a qualification review item.
3. Switch and probe the candidate under closed admission. Only after its full
   read proof passes and the probe stops, persist `candidate_chosen`, select the
   release and start the ordinary lifespan. Failure after choice preserves data;
   report a selected-release startup failure without automatic old-data restore.
4. On any failure after checkpoint seal but before choice, stop every candidate
   writer, restore via the existing per-root atomic rename journal, and rescan
   the live roots independently against B. Missing, extra, wrong-type or changed
   entries cause `rollback_tree_mismatch` and keep service stopped. Compare the
   full root set too. Resume after each rename/fsync boundary; never overlay.
5. Verify old semantics on a disposable restored copy with old code, validate
   the previous installed identity, and use the structural proof for live data.
   Do not run the old cache verifier on restored live roots. Persist
   `previous_chosen` before ordinary old startup, then check service identity,
   member reads and a fresh complete protected backup. Post-choice startup
   failure retains the restored data for repair. Never reapply B over later work.

The old-code rehearsal must consume the same corpus paths through the existing
overlay builder; it must neither silently upgrade with candidate code nor probe
remote originals. Retain its initial successful baseline so recovery after a
preparation failure does not require a proof that was never produced. When no
semantic baseline exists, exact restoration plus the recorded pre-stop healthy
identity permits returning to old startup; label this as structural rollback
verification, not successful semantic rehearsal. Qualification covers this
failure separately. The proposed decision makes this change to old-live-probe
semantics explicit.

### First update and separately shipped supervisor

Ship and version the supervisor change separately, bundled with the target
release. The existing target-required-supervisor check makes `server update`
refuse until `server supervisor update` installs it. The new supervisor must
understand both old journals and the new checkpoint format; it must not upgrade
an incomplete old checkpoint's claim to exactness. An unfinished legacy rollback
without a complete baseline requires operator recovery, with all payloads kept.

A new supervisor plus a new candidate inventory worker can capture every byte
left out by an old `prepare` on the very first application update. Installing
only a new application preparation function cannot do so. The bootstrap test
must use an unmodified old wheel and old `prepare`, with token/job/watermark and
empty-directory entries it omits.

There is a separate legacy admission problem: the old maintenance RPC cannot
prove sign-in threads idle. Reading its SQLite capture, polling twice, or
checking only child PIDs does not close that race. Recommended first-update
policy: return `legacy_quiescence_unproven` while such a source is running, with
an operator action to finish/cancel sign-ins and work, stop the old service, and
retry the same update command. The stopped-source path must prove the service
and external writers are stopped, snapshot before any old worker, and reuse
the existing offline preparation/protection machinery for a complete backup.
It joins the same stopped-state transaction; no alternate restore algorithm,
force flag, or source-code hot patch. This is a proposed operator procedure,
not an action performed or approved in this run.

For newly capable sources, add a maintenance capability that covers sign-in
registration through completion and all writer owners. Close sign-in admission
under the same synchronization used to register threads, and refuse pending
sign-in with `provider_sign_in_pending` before stopping anything. The server
continues running after refusal and its admission reopens. Ordinary updates
then retain the online maintenance entrance. The legacy entrance and exact
historical floor need review before their dependent slice is implemented.

### Quarantine ownership

The supervisor owns only quarantines named by a restoration journal. The
filesystem worker moves them as the service account. Retain them through live
tree proof, old startup and a complete post-rollback backup. Then extend the
existing retention owner to relocate owned quarantines out of project repos
into the operation workspace, verifying copy/hash/fsync before removing the
source when a rename cannot cross filesystems. Cleanup must be resumable and
must not turn a completed rollback into a failed deployment.

Prune those diagnostic trees with their owning retained checkpoint under the
existing retention limits, including successful `rolled_back` operations.
Never delete by a `.rcp-quarantine-*` glob. An old quarantine predating exact
capture may contain the only copy of omitted state; keep it and name an
operator inspection action. This design does not infer which live file wins,
merge orphan contents, or remove the incident's leftovers automatically.

## Source-release rule and cost

Recommend every promoted release since a declared minimum supported packaged
update source, fixed at the first supported paired-wheel installation. Also keep
all immutable schema-era fixtures and the exact candidate-base test. This
preserves the permanent data compatibility decision; it does not create an
age-based support window. A server pinned for months remains eligible.

| Selection rule | Assessment |
| --- | --- |
| Newest five, or another count | Reject. CI silently drops still-running sources. The count is duplicated in Python and CI's `tail -5`. |
| Versions observed in the fleet | Useful extra cases, insufficient policy. Offline/pinned servers and restored installations are not a complete observable fleet. |
| Every promoted source since installation support began | Recommend. A monotonic, explicit source set is enforceable and auditable. Cost grows with actual releases, so share builds and corpus cases rather than drop sources. |

One application-owned release compatibility contract supplies the floor and
required maintenance capabilities. Candidate `capabilities` returns it without
opening data; CI uses the same module to select cases; the supervisor enforces
it against the authenticated selected receipt before backup/maintenance. Below
floor gives `unsupported_update_source` with the separate historical adoption
path, not a sequence of arbitrary intermediate releases. Unknown/newer source
identities and unsupported downgrade targets refuse explicitly.

The durable source set must come from promotion records, not `git tag 'v*'`
alone. Retain a digest-pinned catalog of promoted tag, commit, manifest and
assets; promotion extends it, never silently removes a source. Enumerate
non-draft/non-prerelease records and validate complete paired assets. CI fails
if catalog entries or assets are missing, rather than skipping absent indices.
The floor remains one code-owned value, not a second CI constant or config key.

Evidence gap: the local tags and a read-only release-list query in this run show
only v0.3.24 through v0.3.27. Existing fixture documentation explicitly names an
installed v0.3.5 boundary. Thus v0.3.24 is not defensible as the support floor
merely because it is the oldest visible release. Recover the historical
promotion/asset inventory first; recommend v0.3.5 as the initial candidate floor
subject to checking its paired assets and maintenance contract. If assets were
deleted, current source rebuilds cannot stand in for the original wheel proof.
Keep that release qualification visibly incomplete until resolved.

The supplied local estimate is 3–4 seconds per semantic case plus a uv
environment build per tag; this run did not remeasure it. For N releases and K
profiles, budget roughly `N * (build time + K * 3–4 seconds)` plus packaging/VM
cost. Cache one immutable environment per manifest/interpreter/platform, and
reuse it across cases. At N=25, the existing two cache profiles cost about
150–200 seconds excluding builds. Report measured per-tag setup/case time in CI;
only shard when those measurements warrant it. Installed systemd/SSH/reboot
cost is separate and must not be represented by the 3–4 second estimate.

## Shared corpus and schema-change guarantee

Extend the existing `supervisor_reboot_data.py` builder and
`release_update_base.py` driver into one reusable old-server corpus. Both the
fast compatibility gate and installed transaction gate consume it. Keep frozen
schema-era bundles immutable; share expectations and state modifiers rather
than regenerating historical bytes. Run each producer using its own installed
old wheel in isolation and record module path/version/manifest identities.
Never fabricate old state by dumping current models.

Use a small set of named profiles: mixed durable state; display-cache variants;
busy/refusal states; remote availability; size/recovery boundaries. The mixed
profile includes synthetic credentials, completed job receipts, transfers,
failed-run scratch, nested records, branches, merges and empty directories.
Historical producers explicitly declare features unavailable in their era;
those cases are exercised from the first supporting release, not silently
dropped. Avoid a Cartesian product of every state axis; pair corruption/busy
modifiers with the mixed profile and run the common clean profile for every
release. The table below states where focused tests supplement that profile.

Add one recursive projection-compatibility invariant in
`tests/test_application_deployment.py`. Traverse the actual graph model's union
variants and nested record fields, including nonempty optional/list/map
examples. Compare old serialized paths with current paths. Each newly added
defaulted path must have an explicit upgrade example: remove that path from a
valid minimal legacy document, run `upgrade_graph_projection`, and compare to
the expected current projection. Reach nested fields even when their container
is empty by requiring a populated coverage example for every reachable type.
A model/path coverage assertion fails when a new type or field lacks an example;
the semantic assertion fails when its upgrade is missing. Test idempotence and
preservation of explicit non-default values in the same invariant.

Do not use a blanket current-model validate/dump as the upgrader or expected
result: it can silently add all defaults, normalize values, derive layers, and
drop unknown fields. Pin the explicit transition expectations to old inputs;
preserve old edge layers and all unlisted values. A required field, changed
default, renamed field or changed meaning needs an explicit versioned migration
and frozen boundary fixture, not a wider digest exception. Nondeterministic
defaults must not be invented during upgrade.

The corpus coverage check derives node variants and legal relation/endpoint
combinations from the model/rule registries. Cover every node type, relation
type, layer class, custom ontology type/field/relation, and nested record
(assessment, attempt/debug/pins, proxies, proposals, glossary, ambiguity,
validation/replay records, branch metadata, provenance and merge receipts).
Separate canonical committed content from derived diagnostics when asserting
replay; a nonempty model instance alone is not a reachable legal graph.

The new application proof compares main and every branch's exact head, immutable
base, replay digest and merge receipts, plus Patch byte hashes. For old proofs
that lack branch fields, the candidate may derive the supplementary branch
baseline only from authenticated captured histories and the old-code rehearsal;
it must not treat a candidate's own replay as both expected and observed.
Version the extended proof without teaching strict old models new fields.

Compare representative durable operational bindings too: chat/session/stage and
graph target; episode authorization/budget/Stop state; watcher delivery state;
job identity and receipt paths; provider login generation; ingestion cursor;
transfer receipt. Ordinary lifespan tests prove reconciliation/continuation
exactly once with deterministic local test workers, without real provider calls.
This is a bounded guarantee for admitted states, tested releases and supported
filesystems, not a proof about arbitrary corruption or external side effects.

## Server state matrix

“Gate today” below means the recent-release test, unless another test is named.
Reason codes are proposed stable operator diagnostics, not existing wire values.
Every refusal test checks that the selected release and durable data remain
usable and that any entered maintenance boundary is released.

| State at update | Gate today | Required outcome and CI proof |
| --- | --- | --- |
| Current display cache | Yes, recent sources | Proceed; public snapshot equals fresh replay after ordinary startup. |
| Old-shape display cache | Yes, via actual old writers | Proceed only through explicit projection upgrades; model/path coverage catches every missing default. |
| Valid stale cache after failed refresh | Yes, verifier reconstructs; stored cache can remain stale | Proceed; inject a real failed refresh after accepted Patch, reconstruct read-only during probation, repair display cache through its owner after choice, and assert the served head is current. |
| Missing/invalid/truncated cache or refresh temporary file | No release matrix case | Proceed from valid canonical history; cache failure is never canonical failure. Test missing, invalid and stale-same-revision digest cases, with zero canonical/remote publication before choice. Unknown files are preserved by rollback. |
| Main plus nonempty branches and merge history | No branch proof | Proceed; compare all heads, bases, receipts and Patch hashes. A changed branch gives `branch_proof_mismatch` even if main is unchanged. |
| Empty branch `patches/`/`merges/`, empty `facts/`/`paper/`/`chat/`, `cursors.json` | Not asserted | Proceed; exact tree oracle includes directories/watermark. After forced rollback, run real protected backup and require zero uncaptured projects. |
| Managed credentials and local job files | Not in rollback assertion | Proceed when idle; synthetic token/login generation and completed job receipts survive byte-for-byte. Backup still excludes secrets. |
| Local project, complete history | Yes, one representative project | Proceed; multi-project root-set proof catches omitted local roots and accidental overlap. |
| Reachable SSH canonical project | No | Proceed after complete capture and stable head proof; real disposable SSH host, read-only probation/rehearsal, unchanged remote inventory and no publish calls. Recheck head drift; refuse `project_head_changed`. Legacy workers also need copied-overlay proof or refuse `legacy_remote_probation_unproven`. |
| Unreachable SSH or any uncaptured project | Recent test bypasses protected backup | Refuse `backup_project_uncaptured` naming safe project ID and reason. Do not weaken this to an offline card. Test disconnect before backup and between backup and final capture. |
| Pending provider sign-in | No | Capable source refuses `provider_sign_in_pending`; test a thread waiting before it persists the marker as well as an active login process. Legacy running source refuses `legacy_quiescence_unproven`. |
| Queued episodes or paused/failed tasks | Only paused Work scratch in common builder | Proceed if all bindings/stages are valid; no pre-choice launch. After choice continue or settle once according to existing lifecycle rules; preserve authority, Stop, scratch and patch text. Missing required stage gives `recovery_stage_missing`. |
| Active provider turn or stopping episode | No release journey | Drain entered work within existing bound; otherwise `maintenance_busy`, with no forced cancellation. Stop fence and target/session remain identical. |
| Watchers, pending wake/delivery | No release journey | Pause polling and drain in-flight callbacks; proceed if durable. Assert no pre-choice delivery and one post-choice delivery; timeout gives `maintenance_busy`. |
| Active local compute helper writing inside a captured root | No | Refuse `local_job_active`; do not snapshot mutable logs or kill the job to update. Test real helper process and final exit/receipt race; retry succeeds once stopped. |
| External scheduler/SSH job outside replacement roots | No | Proceed only with durable receipt, callbacks fenced and no writes into captured roots; external job continues. After choice reconcile its real synthetic job receipt once. Unknown local writer gives `state_writer_unproven`. |
| Complete transfer inbox entry; empty inbox | Typed snapshot tests only | Proceed; retain entry set/receipts and exact empty directory on rollback. |
| Active upload, partial/untyped inbox or export | Active upload refused; typed snapshot tests | Refuse `transfer_upload_active` or `transfer_state_incomplete`; completing the transfer permits retry. Never silently omit its bytes. |
| Large graph | One focused >4 MiB case; normal corpus small | Proceed through the supported 16 MiB graph bound; test >4 MiB actual content and boundary/over-bound cases. Beyond bounds gives `graph_size_limit`; checkpoint/disk/time limits give named bounded refusals, not truncation. |
| Terminal `rolled_back`/`aborted` operation and owned quarantine | No recent-release case | Proceed if no active journal and pointer/receipt agree; observational `update_operation_state` is not authority. Exercise a second update, backup and bounded quarantine retention. |
| Legacy/unowned quarantine or unfinished/corrupt journal | Coordinator tests, no installed corpus | Preserve legacy quarantine and report it; it alone does not block a clean tree. Unfinished operation must recover first; ambiguity gives `operation_recovery_required`. No glob deletion or automatic import from quarantine. |
| Partial backup, imported-history corruption, unsafe paths or storage exhaustion | Other focused tests, not release gate | Refuse before switch with `backup_incomplete`, `imported_history_invalid`, `checkpoint_unsafe_entry` or `checkpoint_capacity`. Assert useful per-project reason, no discarded scratch, and a successful retry after the fixture defect is removed. |

A failed pre-choice update must exercise rollback in every source-release
profile containing old shapes, not only test successful validation. The core
installed journey is: old healthy service, complete encrypted/read-back backup,
update, forced live verification failure, exact restored inventory, old service
and member reads, complete backup, then another successful update. Compare raw
tree equality before restarting old code; afterward compare durable semantics
and backup completeness. Archive byte size alone is not the correctness oracle.

## Diagnostics

`SystemRuntime.control` first authenticates socket peer and envelope
request/instance/protocol identity, then distinguishes a valid refusal from a
malformed response. A valid refusal preserves bounded `code` and redacted
`message`; it does not require success-only `result` fields. Malformed or
unauthenticated replies remain generic failures. Operator events, terminal
output and the private operation record carry the same cause.

Capture owners produce a safe category plus project ID and relative failing
component, such as an absent branch directory. Preserve inventory/imported
history/local/SSH distinctions through backup receipt and supervisor event.
Do not dump raw exception text containing credentials or locators. Old sources
that emitted only a generic reason cannot be made more precise retroactively;
report that limitation and the retained capture receipt, never invent a cause.
Keep old strict receipt readers compatible using a versioned result or optional
diagnostic sidecar outside their parsed document.

Probe failures include phase, bounded last readiness error and private log
reference. Graph mismatch reports safe project/target ID and changed field
paths or digests of nonsecret graph data, never entire graphs or provider state.
Preserve the original failure alongside any recovery failure.

## Five independently mergeable slices

Each slice changes its owning spec when its behavior lands and carries one
parameterized test per invariant, extending existing tests before adding files.
No exact prose assertions, per-directory test copies, or separate old-state
fixture builders. Later gate expansion is required for overall closure even
when an earlier slice can safely merge alone.

### 1. Preserve refusal causes

Owners: supervisor `runtime.py`/events; application backup capture/result owners.
Add authenticated refusal forwarding, safe per-project capture diagnostics and
probe log/last-error references. Keep wire compatibility with old sources.
Two invariants: authenticated causes survive end to end; malformed/untrusted
responses cannot supply operator text. Extend existing backup/runtime tests.

Checks: `uv run pytest -n0 tests/test_supervisor_runtime.py tests/test_backup_capture.py tests/test_supervisor_events.py`.
Run `uv run ruff check` and `uv run pre-commit run --files` on the changed files.
Drive one disposable backup refusal and one maintenance refusal through the CLI.

### 2. Make stopped-tree rollback complete

Owners: supervisor checkpoint/fs worker/runtime/operations/retention; application
read-only root inventory; existing deployment and reboot corpus helpers.
Implemented: the B/R proof, capture-before-prepare phases, old-copy verification,
journal-owned quarantine cleanup and supervisor version bump. Protected
restore/adoption retain their distinct authority. The online maintenance
entrance remains unchanged; the proposed legacy stopped-source entrance and
bootstrap refusal await a human decision and are not enabled by this slice.

Checks: `uv run pytest -n0 tests/test_supervisor_checkpoint.py tests/test_supervisor_operations.py tests/test_application_deployment.py tests/test_supervisor_retention.py tests/test_supervisor_restore.py tests/test_supervisor_migration.py`.
Extend the existing real payload round-trip with a full tree oracle taken before
prepare, omitted entries and empty directories; force failure during prepare
and verification and interrupt every restoration boundary using the existing
parameterization. Assert a fresh backup is complete after rollback. Run focused
Ruff/hooks. Promotion waits for slice 5's unmodified-old-wheel bootstrap proof.

### 3. Prove quiescence, probation and graph semantics

Owners: maintenance, provider sign-in, application startup, catalog/history read
path, deployment/application validation. Add atomic sign-in admission and
writer checks, read-only graph reconstruction, branch/merge and operational
proofs, and the recursive explicit-projection invariant. Reuse the ordinary
startup owner; defer effectful login/recovery/publication until choice, keeping
permitted local migrations within the complete checkpoint. Publish a distinct
capability for the stronger maintenance contract, retaining old-source decoding.

Checks: `uv run pytest -n0 tests/test_application_maintenance.py tests/test_application_deployment.py tests/test_application_snapshot.py tests/test_server_upgrade.py`.
Add the existing provider-login and branch test paths actually changed to the
same focused run. Drive full lifespan with queued/stopping episodes, watchers,
job receipts and a paused sign-in thread. Real SSH qualification asserts no
pre-choice publication. Run focused Ruff/hooks; no Web build is needed unless
an actual Web source/type contract changes.

### 4. Replace release-count selection and share the corpus

Owners: application release compatibility contract; supervisor capability/source
admission; `server_upgrade_harness.py`, `release_update_base.py`, common corpus;
CI and promotion workflows. Resolve the historical catalog/floor before enabling
its gate. Replace both `RECENT_RELEASE_COUNT` and `tail -5`, enforce the same
source policy at runtime, pin artifacts, cache environments once per source,
and parameterize the state profiles. Retain permanent fixtures and exact-base
coverage. Workflow changes are part of this slice's explicit review scope.

Checks: `RCP_RUN_EXACT_BASE_UPGRADE=1 uv run pytest -n0 tests/test_server_upgrade.py`;
`uv run pytest -n0 tests/test_supervisor_driver.py tests/test_supervisor_releases.py tests/test_supervisor_packaging.py`.
Run with the complete catalog fetched, record collected tags and timings, and
prove missing assets fail rather than skip. Test at-floor, below-floor,
newer/unknown-source and incomplete-capability refusal. Run focused Ruff/hooks.

### 5. Require installed update, rollback and continuation

Owners: existing supervisor build/guest/controller harness and CI; no parallel
deployment implementation. Install each supported promoted source wheel with
its hash-locked runtime dependencies and real bundled Web assets. Build the
candidate wheel once using the release pipeline and install it with runtime
dependencies only. The candidate-under-test has build provenance, not a fake
claim to be an already promoted release. Reuse guests/environments between
disposable cases after resetting their corpus.

Add a required Linux transaction job for the source corpus: actual service
account/environment/umask, private socket peer checks, systemd stop, encrypted
backup, candidate migration, forced verification failure, exact rollback,
complete backup, successful retry, member HTTP reads and ordinary continuation.
Use a real disposable SSH endpoint for the remote profile. Capture console,
HTTP errors and service/probe logs; a direct Python call is insufficient.
Keep one clean and rollback journey per supported release; run busy/corruption
modifiers once per relevant capability era rather than multiplying all axes.

Checks: `uv run pytest -n0 tests/test_supervisor_reboot_fixtures.py tests/test_supervisor_reboot_hooks.py tests/test_supervisor_reboot_vm.py tests/test_supervisor_integration.py`;
`uv run python -m tests.supervisor_reboot_build --output /tmp/rcp-update-qualification-bundles`.
Extend the existing `tests.supervisor_reboot_live` driver rather than inventing
an unimplemented test command. On a preflighted disposable Linux host, use its
`preflight --output <receipts>` and `drive --ubuntu <version> --bundles <bundles>
--output <receipts>` commands and existing disposable-host authorization guard.
Require successful rollback/second-update receipts, and reboot the new durable
phases on both supported Ubuntu versions before this supervisor is promoted.
The manually dispatched full restore/adoption qualification remains a distinct
record; do not report it green merely because the new PR transaction job passes.

## Deliberate exclusions

- No production recovery, quarantine deletion, release promotion, push, commit
  or configuration change in this design run. Real-data qualification uses a
  sanitized copy, never the live data directory.
- No migration rollback logic and no canonical Patch rewrite. Rollback restores
  the previously accepted stopped state; forward migration remains one-way.
- No provider-home, source-checkout or remote-job rollback. These effects remain
  fenced or cause refusal; full machine-loss restore is a different procedure.
- No new knobs, count window, fleet telemetry requirement or unbounded copying.
  Existing code-owned resource bounds fail explicitly when exceeded.
- No claim of universal schema safety from fixtures alone. The explicit model
  invariant, old-writer corpus, runtime refusal boundary and installed recovery
  journey jointly define the guarantee and its stated limits.
