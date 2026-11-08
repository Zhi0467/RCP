# Production-derived update qualification

Status: design for the remaining update and qualification work. The narrow
active-upload repair and production-derived socket regression are implemented
in commit `60456c8b5`; the changes below are not yet implemented.

## Outcome

Extend the existing installed-upgrade journey to use production-derived state,
and require its full result before promotion. Keep promotion before the real
server update. This is a small-maintainer workflow, with no production canary,
general sanitizer framework, new deployment owner, or silent fallback.

The qualification contract is a supported source artifact set, captured state,
exact candidate assets, and a complete documented update procedure. It is not a
proof about every future state or environment. Candidate-only preparation may
remain a fast check, but cannot count as a full update.

## The observed failure

The real outgoing v0.4.40 application rejected a completed historical transfer
while trying to enter maintenance. All 46 historical upgrade fixtures had
empty transfer tables. The candidate-only rehearsal never invoked outgoing
maintenance, and its `ready` receipt still allowed promotion.

The retained production-derived rows are in
[the terminal-transfer fixture](../tests/fixtures/production_upgrade_state/terminal-transfer/README.md).
They preserve the 0.3.4 writer shape, missing version field and completed state.
Synthetic application scaffolding is labelled separately. Raw exports and
identity maps stay private; new regression slices preserve structure and
relationships without passing through current models.

A candidate-side predicate fix cannot change the installed old binary. The
full old-wheel/state journey must therefore expose this failure until a real
supported update procedure succeeds. Do not remove the rows, repair them first,
change the declared source version, or call the expected refusal a passing update.

## Existing updater extension

Add an explicit `--stopped-source` option to the independent supervisor update
command. The source must already be stopped; the option does not stop a running
service or silently handle a maintenance error. The ordinary graceful path stays
unchanged. The installed wrapper already forwards update arguments, so the old
application parser does not change.

Reuse the existing stopped-source adoption preparation inside the existing
Coordinator: offline inventory, complete stopped checkpoint, offline preparation,
ordinary validation, fenced candidate probe, durable selection and startup.
Reuse existing rollback to the complete original roots and old-code fenced probe.
No new operation kind, journal fields, selected receipt, or recovery subsystem.

Ordinary verified artifact staging remains before the deployment journal.
The new early phase `preparing_stopped_source` records this entry before any
live application-data effect. Its early recovery aborts without calling the
unavailable source control socket or starting a service that the operator stopped. Keep this
phase through ownership verification, offline inventory and complete byte copying;
publish the checkpoint directly with existing `snapshot_ready`, then use the
existing later phases. An interruption before publication leaves unchanged
source roots and selection. Explicit command recovery does not start the stopped
unit. A reboot or an operator-requested service start follows ordinary enabled
service startup after safely aborting the early operation; no persistent stop
latch is introduced. Preserve source-stop
ownership through copy operations using the existing service and instance-lock mechanisms. Explain unavailable protected
backup as a warning, as ordinary update already does; never weaken the byte
checkpoint or proof.

Stopping the service cannot promise the ordinary path's graceful drain. The
operator must arrange interruption of current work before using this option.
It is a supported explicit update procedure for a stopped or maintenance-broken
source, rather than an automatic bypass. Ship it through newly versioned verified
supervisor assets; never edit an installed old wheel or released asset in place.

## Detailed implementation plan

1. **Modify supervisor CLI, driver, operations and runtime.** Carry the explicit
   option through confirmation/resume output. Add the single early journal phase,
   stopped inventory/preparation methods and the alternate entry to the same
   completion sequence. Reuse existing capability, ownership, locking, exact
   confirmation, checkpoint and rollback primitives. Bump the supervisor version.
   Tests cover a running-source refusal, early interruption, full checkpoint
   publication, candidate failure/rollback and normal-path parity.
2. **Modify the installed-upgrade harness and its tests.** Add a raw retained-state
   input at the stopped setup boundary. Keep current release-matrix seeds. Run the
   actual old wheel with the production-derived transfer slice. Begin with the
   source's actual supervisor; select the verified candidate bundle through the
   harness's existing artifact-selection hook, run the real supervisor update,
   and verify the selected supervisor identity before invoking the new option.
   This hook changes artifact transport only, not application/update logic.
   Verify ordinary
   maintenance refusal remains a failed attempt; then stop the disposable source
   and execute the real confirmed stopped-source command. Require committed
   selection, retained state, serving identity, injected rollback and retry.
   Add the same source/state case to PR and main's required old-data upgrade gate.
3. **Modify that same harness and existing VM controller for private rehearsal.**
   Capture source identity and a coherent private snapshot using the existing
   stopped-root copy primitives; an operator-stopped source may be briefly copied
   and restarted unchanged. Run no migration or trial deployment on the live roots.
   Restore the private application snapshot and local registered roots at
   their original absolute paths inside an owned disposable Linux VM. Use exact
   source/candidate bundles and the same updater sequence. Install dependencies
   before introducing private state and prevent guest egress afterward. Do not
   replace private state with synthetic records or upload private reports.
4. **Modify release tooling, its tests, promotion workflow and release guide.**
   Generate a version-2 public receipt only from the complete installed-update
   report. Bind source manifest, candidate manifest, captured-state report digest,
   selected procedure, committed journal and serving result. Reject failed,
   skipped or preparation-only reports. Public entries contain only procedure,
   source/candidate/report digests and committed status, plus the existing desktop
   run binding. No private names, paths or warnings. Reject old version-1 receipts
   before publication. The receipt remains maintainer-controlled evidence; no new
   signing/attestation infrastructure is introduced.
5. **Modify version files and owning docs.** Use a fresh application version for
   the repair release and matching native versions. Document only implemented
   behavior in the server-operations spec and commands in operator guides. Delete
   this design before merge after accepted rules move to their owners.

After promotion, the stopped-source operator procedure first updates the
supervisor through the documented command, verifies its version, arranges current
work to stop, stops the source service, and confirms the exact target with
`--stopped-source`. An uncommitted or failed attempt is never described as an update.

## Acceptance

The original production-derived case must fail on the original implementation
and pass the real current maintenance socket after the predicate repair. The
actual v0.4.40 source/state must then complete the documented installed updater
procedure, including exact rollback and retry. A candidate-only test cannot
substitute for either result. The same candidate bytes must pass the full private
rehearsal; failed or missing full-update evidence must make promotion exit nonzero
before creating a release. Preserve current CI PR/main parity, required hooks,
code/spec reviews and exact release identity checks.

## Implementation contracts

Stopped entry holds the existing operation and deployment guards throughout.
Use a new read-only `SystemRuntime.require_stopped_source()`; never implement
that precondition by calling `stop_service()`. Refuse a running service and a
competing instance owner before copying. `checkpoint_stopped_source(operation)`
holds `stopped_data_lock()` around target `offline-inventory` and the whole-root
copy; inventory does not acquire the application lock itself. Factor the existing
unlocked snapshot call for reuse rather than nesting exclusive flock opens.
Release the parent lock before `prepare_stopped_source(operation)` calls target
`offline-prepare`, which takes that lock in its child process. Both commands use
the previous selected commit. Require the two target capabilities before entry.
Then run `check-roots` against the durable checkpoint and ordinary `validate`.

| Durable phase | Interrupted attempt behavior |
| --- | --- |
| `preparing_stopped_source`, including inventory and partial copying | Abort; roots and selection unchanged; explicit recovery makes no source control/start call; startup recovery permits the normally requested old-service start |
| `snapshot_ready` through candidate verification | Existing full-root rollback and old-code fenced probe/start |
| `candidate_chosen` or `previous_chosen` | Existing recovery of that durable choice; never restore pre-choice data over accepted work |

The production-derived installed case has an explicit sequence: insert raw rows
while stopped; start the genuine old application; upgrade the old supervisor
through its real command and verified bundle-selection hook; attempt ordinary
update and assert its format failure and aborted journal; stop; inject the
existing candidate failure into stopped-source update; assert exact restoration
and old serving identity; stop again; retry stopped-source to committed selection.
Compare both retained rows' raw columns and JSON after rollback and success.
Other release-matrix cases keep their ordinary path.
Test explicit early recovery with the unit still stopped, and startup recovery
which safely aborts before allowing ordinary startup of the unchanged selection.

The private input is a JSON manifest naming `source_manifest_sha256`, the data
snapshot, registered local snapshots with original absolute destinations, their
tree digests, expected space/project identities and canonical file digests.
Create `tests/server_update_rehearsal.py` as the thin VM controller. Add a private
state entry to the same installed guest driver, without enrollment or synthetic
seeding. Use its owned-guest marker/token instead of fabricated GitHub runner
flags. The VM controller may run on an explicitly confirmed Linux x86_64 operator
host even when that host runs RCP: its effects stay in its owned VM directory.
The direct installed driver still refuses any pre-existing host installation.
Provision exact bundles/dependencies online before private bytes; boot with
`offline=True` before transferring those bytes and retain offline mode on every
restart/reset. Fault injection modifies a fixture-owned sentinel and journal
boundary, never an assumed synthetic project filename. Detailed outputs stay
under the private output directory, outside CI artifact collection.

Extend the existing full report with this required contract (other diagnostic
fields may remain). `serving.commit` comes from health `running_commit`, not
the short display commit. The selected supervisor version must match the
verified candidate bundle. Supported procedures are `graceful` and
`stopped-source`:


```json
{
  "version": 1,
  "journey": "installed-update",
  "status": "passed",
  "source": {"manifest_sha256": "<64 hex>", "commit": "<40 hex>", "version": "<source build version>"},
  "candidate": {"manifest_sha256": "<64 hex>", "commit": "<40 hex>", "version": "<candidate build version>", "supervisor_version": "<bundled supervisor version>"},
  "input_sha256": "<64 hex>",
  "procedure": "stopped-source",
  "supervisor_version": "<verified selected supervisor version>",
  "journal": {"phase": "committed", "previous_manifest_sha256": "<source digest>", "target_manifest_sha256": "<candidate digest>"},
  "serving": {"status": "ok", "commit": "<candidate commit>", "version": "<candidate version>"},
  "rollback_exact": true,
  "retry_committed": true
}
```

Create the `qualification-receipt` command in `packaging/release_build.py` with
repeatable `--report`, `--manifest`, `--desktop-candidate-run`, and `--output`.
It compares verified source/candidate identities to journal previous/target and
observed serving identity, requires successful full update, rollback and retry,
and hashes each complete private report. Emit only
`{version:2, desktop_candidate_run, rehearsals:[{kind:"installed-update",
procedure, source_manifest_sha256, manifest_sha256, qualification_sha256,
status:"committed"}]}`. The same existing `check-receipt` validates that public
projection against candidate bytes. Reject malformed digests, mismatches,
missing/failed/preparation-only reports, unsupported procedures and v1 receipts.
Strict public fields protect privacy; extra private diagnostic fields are allowed.

Exact change boundaries, with focused tests after each merge:

- Modify `supervisor/src/rcp_supervisor/{cli,driver,operations,runtime,__init__}.py`
  and `tests/test_supervisor_{driver,operations,runtime}.py` for stopped entry.
- Modify `tests/server_installed_upgrade.py`, `tests/server_installed_upgrade_hook.py`,
  `tests/test_server_installed_upgrade.py`, `tests/test_server_installed_upgrade_artifacts.py`
  and `.github/workflows/ci.yml` for source/corpus jobs and the full report.
- Create `tests/server_update_rehearsal.py` and `tests/test_server_update_rehearsal.py`;
  modify `tests/supervisor_reboot_vm.py` and `tests/test_supervisor_reboot_vm.py`
  for the owned private VM input and persistent offline mode.
- Modify `packaging/release_build.py`, `tests/test_release_build.py`,
  `.github/workflows/promote.yml`, `docs/release.md`, `docs/server.md` and the
  server-operations spec for generated evidence and operator commands.
- Modify `src/rcp/__init__.py`, `web/package.json`, `web/package-lock.json`,
  `web/src-tauri/Cargo.toml` and `web/src-tauri/Cargo.lock` for the fresh release.
  Delete this design when its accepted behavior is implemented in those owners.
