# External supervisor and release artifacts handoff

Date: 2026-09-02
Status: active. Phases 0 and 1, the Phase 2 public-origin transition, and the
remaining Phase 2–5 supervisor implementation are merged through PR #65.
Independent deployment and restore journals, closed admission, generic
checkpoints, automatic boot recovery, artifact installation, source adoption,
operator delegation, and retirement of the old deployment owners are implemented.
The disposable reboot and historical-source adoption harnesses are implemented;
drive-found fixes use subsequent PRs. Actual Ubuntu qualification, fresh-host
GitHub checkout/key reconstruction evidence, human promotion, and the Phase 6
production drive remain outstanding.

Human clarification, 2026-09-06: finish the remaining coding in one PR, then drive
the system; bugs found by that drive belong in subsequent PRs. Earlier phases
and preparation receipts below are historical evidence, not instructions to
restore the retired source deployment path. The accepted authority remains
[the supervisor decision](../decisions/2026-09-02-deployment-moves-to-an-external-supervisor.md)
and the [operations spec](../specs/server-and-machine-operations.md).
The [team-server handoff is archived](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server.md);
its former surface freeze is closed.

Closure condition, all of it:

1. Phase 5 deletions are verified: `create_app` reads no deployment journal,
   release coordination belongs to the supervisor, and the full suite is green.
   The private CLI connection and non-deployment operations remain functional.
2. Phase 6 is recorded: the persistent lab server updated once through the
   supervisor and rehearsed one rollback, with the receipt in this file.
3. `docs/server.md`, [`docs/release.md`](../release.md), and the operations spec
   describe only the supervisor path.

When those hold, archive this handoff.

## Settled decisions

- Every server follows `stable`, the newest non-prerelease GitHub Release. A
  human promotes a build to `stable` when they choose. No cadence.
- CI publishes one build per merge to `main` as prerelease `build/<N>`: the
  `rcp` wheel, the hashed lock export, a SHA-256 manifest, the supervisor wheel, and its hashed lock. A later merge never cancels an earlier
  `main` run. Builds are pruned after thirty days; releases never.
- Promotion re-attaches the build's assets under `vX.Y.Z` and never rebuilds.
  The build's base version must equal the tag.
- The wheel version is `<__version__>+build.<N>.g<sha7>`. Bumping
  `src/rcp/__init__.py` is an ordinary pull request before promotion.
- The supervisor is Python, in this repository as `rcp_supervisor`, imports
  nothing from `rcp`, has its own version, and is released only when its own
  logic changes.
- Going public with a protected `main` happens inside this handoff, as the one
  bundled transition already designed: branch protection and retirement of the
  private-source deploy key together.
- Servers install RCP with `uv`; its Node.js/npm build prerequisite disappears.
  Git remains for research repositories. Provider-specific dependencies remain
  the provider's responsibility.
- Human clarification, 2026-09-05: retain the private CLI connection used by
  provisioning, transfer, provider checks, backup capture, and member removal.
  Remove deployment coordination, not the entire `control.py` protocol.
- Retain a narrow closed-admission/quiescence boundary: the application proves
  that tasks, mutations, machine operations, and watcher/recovery owners have
  reached safe boundaries before the supervisor stops it. A zero-active-tasks
  health snapshot cannot replace that interlock. Release identities, checkpoint
  journals, switching, and rollback belong exclusively to the supervisor.
- Forward-only migrations, direct upgrade from every server-era database, the
  old-data CI job, protected backups, systemd, and the operator and service
  privilege split all stay.
- Human clarification, 2026-09-06: interrupted deployment recovery runs
  automatically, including after reboot, before RCP can admit work. The
  supervisor owns the startup guard and uses local artifacts and checkpoints.
  Unknown journals fail closed. Recovery after admission reopened must preserve
  subsequently accepted work. Prove this with actual disposable-host reboots,
  not only process crashes or operator-driven re-entry.

## Remaining gates

1. Integration PR #65 merged as `94f37f4`; PR and merged-main CI passed, and
   `build/460` published all five assets. Drive-found fixes use separate PRs.
2. Drive `.github/workflows/supervisor-recovery-live.yml` on disposable hosted
   Ubuntu 22.04/24.04. It preflights QEMU/KVM and refuses qualification when the
   environment cannot prove real reboots. Exercise source adoption and fresh-host
   GitHub checkout reconstruction separately where synthetic fixtures do not
   establish those operational promises.
3. Human-promote a complete build containing the maintenance contract and both
   wheels/locks. An older `stable` must fail with its missing requirement; never
   silently substitute `main` or a prerelease.
4. With complete protected backup verified, perform Phase 6 on production and
   record redacted receipts here. Archive this handoff only after all closure
   conditions hold.

Run [34050713688](https://github.com/Zhi0467/RCP/actions/runs/34050713688)
at `bcc2fd6` passed both actual KVM preflights after renewing the runner ACL,
but both first guest launches again failed with KVM permission denied. No guest
boot or recovery was proved. The workflow now grants the hosted runner membership
in the device's existing `kvm` group and enters that group for both preflight and
the complete drive. This replaces the transient ACL grants; QEMU remains
unprivileged, and all disposable-host and changed-boot gates remain required.
Run [34051609679](https://github.com/Zhi0467/RCP/actions/runs/34051609679)
at `f943744` then booted both requested Ubuntu guests. Their first boot IDs were
`49fdf4dc-1752-4a3f-858a-e31c6b40dbfb` (22.04) and `e4318aa1-5987-4a8e-bd1c-fcce218d6de2` (24.04).
The application bootstrap command failed before any recovery case; its stderr
was not retained by the prior controller. The harness now preserves bounded
guest stderr. Initial guest boot is proved; changed-boot recovery is not.

The workflow also packages exact historical source `203ad6a` and a bounded
Node.js 24/npm runtime for a separate pristine guest. Recovery and adoption run
as independent jobs for each Ubuntu version. Adoption runs the historical
installer, paired-wheel adoption, complete protected archive decryption/inventory
verification, and an offline reboot using the shipped startup guard. Receipts
keep the source identity, selected wheel identity, protected archive hashes,
changed boot IDs, preserved team/project/canonical data, and service ownership.
These additions are implementation and local test evidence until their hosted
run succeeds. Fresh-host GitHub checkout and replacement deploy-key evidence
remains a separate external gate; reusing the existing synthetic checkout does
not prove it.

## Phases

### Phase 0 — contract the supervisor will rely on

Status: implemented on main. The exit proof is met:
fresh, current, every frozen server-upgrade boundary, unknown-ledger,
ledger-ahead-of-registry, uncheckpointed-WAL, unowned pre-ledger shape, apply,
and held-instance-lock migration cases are covered; the database and any
pre-existing WAL bytes remain unchanged under `--check`; and health and both
version renderings assert the new identity fields.

Lands: `GET /api/health` adds `build`, `commit`, and `schema_ledger_head`
beside `version`; `rcp --version` prints the same three facts; `rcp migrate
--check` and `rcp migrate` run the storage ledger without serving and exit
nonzero on any unknown state; both emit the machine-readable event stream.

Owner files: `src/rcp/__init__.py`, `src/rcp/__main__.py`, `src/rcp/migrate_cli.py`,
`src/rcp/api/health.py`, `src/rcp/storage/base.py` (read-only use of the
ledger), `docs/specs/server-and-machine-operations.md`,
`docs/specs/api-web-and-desktop-projections.md`.

Must not change: what `serve` does at startup; any persisted value; the
existing health fields.

Exit proof: tests for `migrate --check` on a fresh database, on every frozen
fixture under `tests/fixtures/server_upgrade/`, and on a deliberately unknown
schema; a health test asserting the three fields. Build and commit are read
from package metadata, so a source checkout reports `build: null` honestly.

### Phase 1 — one build per merge, promotion without rebuild

Implemented: a `build` job in `.github/workflows/ci.yml` on `push` to `main` that
builds the `rcp` wheel with the `+build.<N>.g<sha7>` local version, runs
`uv export --frozen --no-dev` with hashes, writes a SHA-256 manifest, and creates
prerelease `build/<N>`; the workflow's `concurrency.cancel-in-progress` narrowed
to pull-request runs only, because today's setting cancels an earlier `main`
run when a second merge lands and would silently drop that merge's build; a
`promote` workflow with a `build` input that verifies the base version equals
the requested tag, creates release `vX.Y.Z`, and re-attaches the same assets;
a scheduled `prune` workflow that deletes `build/<N>` prereleases older than
thirty days; `docs/release.md` updated from "intended" to "current" wording.
The supervisor wheel is not part of this phase; Phase 3 adds it to the build
when the package exists.

Owner files: `.github/workflows/ci.yml`, `.github/workflows/promote.yml`,
`.github/workflows/prune-builds.yml`, `pyproject.toml`, `docs/release.md`.

Must not change: the wheel contents beyond the version string; the existing
lint, pytest, old-data, and web jobs; `__version__` semantics for a source
checkout.

GitHub exit proof, observed 2026-09-03 (UTC):

- Two merges landed twelve seconds apart (PR #17 at `3e8a98b`, 04:57:51; PR #16
  at `974084f`, 04:58:03). Both `main` runs completed and published `build/290`
  and `build/291`; neither run was cancelled.
- `build/259` (`77e4c99`) was verified locally end to end: manifest hashes,
  `check-promotion` accepting `v0.3.2` and refusing `v0.4.0`, the hashed lock
  installed into a clean virtual environment, and the wheel served on a spare
  port.
- The human promoted `build/291` to `v0.3.2` (run 33717746929, 05:10). The
  release targets the build commit, the `v0.3.2` tag resolves to `974084f`, and
  the three downloaded assets are byte-identical to the `build/291` assets and
  verify against the manifest. `v0.3.2` is the first `stable`.
- After the version-bump PR #26 merged as `0d53b1d`, its `main` run published
  `build/309` (`rcp-0.3.3+build.309.g0d53b1d`). The human promoted it to
  `v0.3.3` (run 33726666549, 16:5x); the tag resolves to `0d53b1d`, the release
  is `latest`, and all three assets are byte-identical to the build's. Later
  merges to `main` are served to the lab from source and were not promoted.
- The prune workflow dispatched in dry-run mode (run 33719387541, 05:34) listed
  no stale builds, all being younger than thirty days, and deleted nothing.

### Phase 2 — public repository and protected `main`

Code status: public-origin transition is merged. The integration PR removes
private-source key creation and label emission while retaining shipped config
and archive decoding and explicit old-key revocation information.

Lands: the bundled transition already designed in the 2026-08-27 update-channel
decision, in this order. Repository public. Branch protection on `main`
requiring the named CI jobs, rejecting direct pushes and failed or missing
checks. A one-time origin migration for installations that still use a
deploy-key SSH origin: `rcp server install` and `rcp server update` call the same
transition function in `server_ops/install.py` for the one deliberate move from
deploy-key SSH to the public HTTPS origin, with the machine config's
`authentication` updated in the same step. The persistent lab server is migrated
and proven with one `rcp server update` from the public origin. Only then are the
`grant_needed` install pause, the `source_ed25519` key material, and the
`rcp-source:<id>` backup label removed together and the lab server's deploy key
revoked. Until Phase 4, servers keep building from source; they simply fetch it
from the public origin.

Lab receipts, 2026-09-03 (UTC), host `wth-gpu-01`, installation
`624ec8e1-3e29-4024-9ddf-06176671aa2e`:

1. 05:20 `rcp server update` over the still-valid deploy key moved the server
   from `accb589` to `9db6b41`; health showed `build: null`,
   `schema_ledger_head: 5`.
2. 05:24 the next `rcp server update` converged the source: config rewritten to
   the public HTTPS origin with the same installation id, both `source_ed25519`
   files removed, the checkout's `origin` rewritten, and the wizard named deploy
   key `rcp-source:624ec8e1-…` for revocation. The same run fetched over HTTPS
   and updated to `8dc68e1`. `server doctor` then reported `configured_origin`
   `https://github.com/zhi0467/rcp.git`, `configured_authentication: public`,
   `source_public_key_fingerprint: none`, release and source aligned, no problems.
3. 05:29 the human deleted the GitHub deploy key; the repository has none.
4. 05:30 `rcp server update` fetched over HTTPS with no key and no grant prompt
   (`FETCH_HEAD` 05:30:22) and reported already current.
5. 16:38 `rcp server update` from `8dc68e1` to `0684bc4` was refused at the
   copied-state rehearsal with `migration ledger is invalid`. Cause: Phase 0
   made the store refuse a ledger that differs from its own, and
   `build_rehearsal_overlay` recorded the running release's startup-recovery
   expectation only after the candidate had migrated the copy (ledger 5 to 7).
   Every update that adds a migration was refused by the release already
   serving; the 05:20 update that added ledger row 5 had passed only because
   `accb589` had no ledger check. PR #28 (`5b5947b`) records the expectation on
   the unmigrated copy. A hand edit of the running release's file was refused
   by `server doctor` (dirty release tree) and `server install` refuses to move
   the release pointer, so no tool path existed for the running release.
6. 17:28 the refused update had already built `releases/5b5947b`. 17:35 the
   human stopped `rcp.service`, copied `data/rcp.sqlite3` (sha256 `d8df42fd…`)
   to `manual-checkpoint-2026-09-03/`, replaced the `/etc/rcp/current` symlink
   atomically, and started the service. Health: `running_commit 5b5947b`,
   `version 0.3.3`, `schema_ledger_head 7`; `server doctor` healthy;
   `rcp server update` reports already current. This one manual cutover is
   the only deviation from the update path in this migration; releases from
   `5b5947b` on rehearse candidates with added migrations normally.

Enforcement record: `main` protection requires the five CI jobs, includes
administrators, and forbids force pushes and deletion; PR #14 stayed `BLOCKED`
while its pytest jobs were pending. A refused direct push has not been recorded.

Rechecked on 2026-09-05: the repository is public and `main` protection still
requires lint/format, Python 3.11/3.12, old-data upgrade, and Web checks, including
administrators; force pushes and deletion are disabled. Hosted run
[33919068513](https://github.com/Zhi0467/RCP/actions/runs/33919068513) succeeded
at `276a2bb` for install and fresh-host restore on both Ubuntu 22.04 and 24.04.
This proves the public-install path, not a restore fixture carrying an old
`rcp-source:<id>` label. Do not push directly to protected main merely to retest
its refusal.

Phase 2 cleanup is implemented in `server_ops/install.py`, `config.py`, and
backup/restore policy. New installations never generate an RCP-source deploy key.
Research-project keys remain supported. Compatibility tests restore an archive
carrying the retired source label and a legacy main-head record with no transition
ID; a recorded, incorrect transition ID still fails closed. The current publisher
records the actual main transition. Hosted public-install qualification must be
repeated through the supervisor entrance before production cutover.

### Phase 3 — independent deployment and recovery

Implemented in `supervisor/src/rcp_supervisor/`: verified release preparation,
root-selected receipts, separate pinned root runtime and operator console,
service-account byte workers, strict operation journals, generic checkpoint
creation/publication, release switching, fenced probes, restore coordination,
and independent supervisor update. The package imports no `rcp` modules.
Application data decoding, canonical replay, baseline comparison, and restore
policy remain in bounded application commands in the selected wheel.

The coordinator takes a complete protected backup, then owns the existing backup
job lock through maintenance and publication. It stops the quiescent application,
checks consistent copies, seals every rollback root, and starts the candidate
behind closed admission. Durable `candidate_chosen` and `previous_chosen` phases
precede ordinary service admission. Recovery after either decision verifies the
chosen current data without restoring an old checkpoint over accepted work.
Failures retain checkpoints, quarantines, journals, and bounded diagnostics.

Supporting checks cover real filesystem/SQLite rollback, process interruption
at journal and per-root publication boundaries, fresh-data restore, exact release
and proof binding, subprocess deadlines, wrong identities, and strict journal
refusal. These checks do not prove machine reboot recovery.

### Phase 4 — installed command and boot integration

Implemented: the paired wheel bootstrap installs root-owned launchers and the
systemd startup guard. `rcp server install`, `update`, `restore`, and `supervisor
update` delegate to the supervisor, preserving the sealed wizard event contract.
Human restore authority choices are mutually exclusive and require explicit
selection. Machine-readable commands never execute operator actions.

The supervisor's own terminal mode renders the same one-line wizard as the
application: a live current-step line, bounded fields, and stop guidance. A
failed operation is a usable breakpoint carrying the bounded cause, the retained
deployment phase, an exact `--machine-readable` diagnostic rerun, a `server
doctor` inspection command, and the exact continue command.

Fresh restore starts from uninitialized app data without creating a temporary
team. Source adoption first retains a stopped opaque snapshot, requires a complete
current encrypted backup, and verifies the candidate before replacing launch
authority. A failed adoption restores the old bytes and integration. After its
chosen decision, recovery preserves the selected data. Research Git, provisioning,
provider checks, backup, transfer, and member operations remain supported.

The disposable QEMU harness uses an external controller, real systemd startup,
changed boot IDs, offline recovery, forward migration, interrupted rollback,
unknown-journal refusal, and accepted-work preservation. It includes 84 cases per
Ubuntu version. Local fixture builds and refusal checks pass; actual VM execution
and the fresh-host GitHub reconstruction drive remain pending. The fixture restore
uses an existing Git checkout and must not be described as new-host key proof.

### Phase 5 — retired deployment owners removed

Deleted `server_ops/update.py`, `update_checkpoint.py`, `update_cutover.py`, and
`rehearsal.py`. `create_app` does not decode deployment journals; it owns only the
application maintenance boundary and its normal startup recovery. Restore policy
is extracted from the old in-app coordinator. Non-deployment authenticated control
operations remain. The old source-install/restore live drivers are replaced by
the supervisor workflow and guest driver.

Current behavior is documented in `docs/server.md`, `docs/release.md`, and the
operations spec. The full backend, web, lint, documentation, and old-data checks
must pass for the completed integration commit. S103/S104/S135 remain pending
where actual hosted or production drives are still required; S36 remains the
separate packaged-desktop updater scenario.

### Phase 6 — the lab

Lands: the persistent lab server moves to the supervisor path: install the
supervisor, update once to a promoted release, force one rollback rehearsal,
and record both receipts here with dates and release names.

Exit proof: the receipts, and `server doctor` on the lab host reporting the
followed release. Then archive this handoff.

## Not in scope

- Any new team feature, transfer phase, or desktop protocol surface.
- Rolling back data after a cutover has verified and reopened service. Inside
  the switch window the pre-switch checkpoint is restored automatically; after
  the window, restore from the protected backup remains the answer, as today.
- A package repository or PyPI publication. Assets live on GitHub Releases.
- Windows or non-Ubuntu servers.

## Verification environments

Phases 1 and 2 use GitHub metadata/workflow evidence. Phase 3 starts with local
temporary layouts and fake services, with real frozen-release integration.
Phase 4 uses `.github/workflows/supervisor-recovery-live.yml` and
`tests/supervisor_reboot_live.py` on hosted disposable Ubuntu 22.04/24.04 runners. No personally supplied disposable VM is required. Phase 6 is
proven on the persistent lab server only after Phases 3 through 5 are green on
disposable hosts. Never test against the lab server's real data directory
first.

On 2026-09-06 the human confirmed that `wth-gpu-01` is production and authorized
using its sudo-ready `rcp-update` tmux session for protected backup, supervisor
installation, and update. It is not a disposable test host. Use GitHub-hosted
runners for qualification; the human has no separate disposable VM to provide.
The reboot harness must preflight guest virtualization there and leave
[S135](../acceptance/S135-supervisor-recovers-automatically-after-reboot.md)
pending if an actual reboot cannot be driven. Production cutover still follows
qualified disposable-host proofs, a verified backup, and a human-promoted
complete release.

### Preparation evidence, 2026-09-06

The frozen promoted RCP `v0.3.3` assets
(`0.3.3+build.309.g0d53b1d`) verified against their original manifest. A separate
local test bundle combined those unchanged RCP bytes with the new supervisor
wheel/lock; it is not a promoted release. The supervisor installed it into an
isolated temporary environment. Actual serve startup reported build 309,
commit `0d53b1d`, and ledger head 7; health, HTML, JavaScript and CSS returned
HTTP 200, followed by clean shutdown. This is package/startup evidence, not
cutover or reboot proof.

Verification for this preparation slice: the full backend baseline passed
(3,980 tests, 11 explicit skips); after review tightened manifest/installed-byte
binding, the final focused supervisor, release-build, and documentation checks
passed (96 tests). The Web build and all 626 Web tests passed. Ruff and
pre-commit passed for existing and newly added paths. The production update,
supervisor recovery, and reboot exit proofs remain outstanding.

Production doctor and protected backup were driven through the authorized tmux
session. Doctor reported an active service with aligned source/current/running
release and healthy control socket. A fresh protected backup passed archive
readback and decryption, with no retention deletions. Coverage remains partial:
one project was captured and one was omitted because its checkout-recovery
machine set differs from canonical configuration. No app-data entries were
unclassified. Resolve that coverage gap before claiming complete cutover
protection. Exact production identifiers and backup receipts remain private on
the host. Production was not updated, restarted, or rebooted during these checks.

Read-only diagnosis narrowed the coverage defect to a configured default machine
that owns no repository and therefore has no resolved checkout root. The backup
descriptor incorrectly required every configured machine to have a checkout
recovery record. PR #62 merged the correction, preserving unused machine
configuration through restore. The production backup must be repeated after
adoption can use that correction; no production manifests or provisioning records
were edited to satisfy the validator.

### Integration closeout, 2026-09-06

Remaining coding for this handoff was finished in one integration PR. Besides
the wizard rendering above, the final read-only integration review confirmed
and this PR fixed: the control transport rejecting a successful
`maintenance_release` or an open `maintenance_status` because the open result
carries no boundary identity; `server install` re-entry skipping an unfinished
source adoption and publishing selected authority that blocked its rollback; a
concurrent update entering deployment with a stale previous release, now
refused under the coordinator lock; restore never enabling the unit after its
durable activation; the guest fixture writing its qualification drop-in before
the production doctor check it must pass; `--machine-readable` refused at
intermediate application parsers; and guide text describing the retired
empty-target restore and deleted install workflow. Confirmed findings from the
review of PR #61 were fixed in the same files: the releases root's writable
ancestors, wheel metadata that crashed verification, an unmapped UID crashing
the machine-readable stream, a success receipt surviving a failed durability
sync, slow response headers escaping the fetch deadline, FIFO substitution
blocking verification, and packaging version rules that disagreed with the
supervisor's. The DAG zoom findings are unrelated to deployment and shipped
separately as PR #64.

Verification on the final tree: `uv run pytest` 4,184 passed, 10 skipped; Ruff
check and format clean over `src tests packaging supervisor/src`; pre-commit
clean over every modified and untracked path; Web build and 643 Web tests
passed. One earlier full run failed
`test_launcher.py::test_stream_records_explicit_terminal_provider_event` once
under eight workers and passed alone and in the final run; it is outside this
change. The two synthetic qualification bundles were rebuilt from the final
code at `/private/tmp/rcp-supervisor-qualification-bundles-20260906` and
verified by `rcp-supervisor verify` (base manifest `3e9976ad…`, target manifest
`772ff7e6…`, target ledger head 9); they are not promoted releases.

Coverage boundaries that stay open gates: the Linux parent-death test skips on
macOS and runs only in Linux CI; the fixture restore reuses an existing Git
checkout and proves no fresh-host GitHub key or checkout reconstruction; source
adoption has local recovery regressions but no actual old-source drive;
installation from promoted GitHub assets is not exercised by the synthetic
workflow; and no actual reboot has run, so S135 stays pending. No production
update, restart, reboot, or app-data mutation occurred.

A second read-only review of the pushed PR found and this PR fixed: a fresh-host
restore that enabled the unit only after deployment, so a reboot during the
restore never ran the recovery guard (the unit is now enabled before deployment
and the startup guard still keeps an uninitialized rollback target stopped); a
rolled-back adoption that blocked every later `server install` retry; the root
runtime receipt surviving a failed durability sync; a failure inspection command
that ran doctor as root, which the launcher refuses; unavailable-project restore
proofs hashing rehearsal paths; the delegation client publishing success before
the supervisor's stream and exit code were validated; and S95 plus the spec's
restore prerequisite still describing the retired source install and an
empty-only target.

### First disposable qualification attempt, 2026-09-06

[Run 34048821544](https://github.com/Zhi0467/RCP/actions/runs/34048821544)
executed the Ubuntu 22.04/24.04 matrix from merged `94f37f4`. Both initial
preflights successfully initialized QEMU with KVM and built the synthetic
bundles. Both drive steps then refused at Python's advisory `/dev/kvm` access
check before creating a guest. No reboot, update, restore, or adoption was
proved. The original preflight receipt was overwritten by the later refusal;
the successful first probe remains established by each workflow step's exit.

The follow-up makes the actual bounded QEMU KVM initialization the access gate,
retains its error output on refusal, and saves initial and drive preflights as
separate receipts. This does not establish why the two original preflights
disagreed; the corrected workflow must still run successfully on both hosts.
No emulation fallback or relaxed reboot requirement is introduced. Production
was inspected read-only and remains on its previous healthy source release.

[Run 34050003292](https://github.com/Zhi0467/RCP/actions/runs/34050003292)
used merged follow-up #67 (`44b0e73`) and retained the actual QEMU refusal:
`failed to initialize kvm: Permission denied`. The first preflight succeeded,
so this is lost device access between preparation and the drive. The next
workflow change reapplies the same named-runner ACL immediately before the
drive, recording identity and before/after ACLs. The controller still requires
actual KVM initialization and changed guest boot IDs. Production remains behind
the qualification gate.
