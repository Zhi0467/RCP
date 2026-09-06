# External supervisor and release artifacts handoff

Date: 2026-09-02
Status: active, human-confirmed on 2026-09-02. Phase 0 is implemented: health
reports build, commit, and the storage ledger head, and `rcp --version` and
`rcp migrate --check` exist. Phase 1 is implemented and its GitHub exit proof was
observed on 2026-09-03 (receipts below). Phase 2's public-origin transition is
implemented on main and the lab
server completed the deploy-key-to-public transition on 2026-09-03, with the
deploy key revoked and one key-free update proven (receipts below); the later
private-source cleanup and legacy-archive restore proof remain. Public fresh
installation passed hosted Ubuntu 22.04/24.04 run 33919068513 at 276a2bb. The
decisions are settled in
[the supervisor decision](../decisions/2026-09-02-deployment-moves-to-an-external-supervisor.md)
and repeated in the next section so this file stands alone. **Ready to dispatch:**
finish the remaining Phase 2 cleanup, then implement Phases 3–6. The predecessor
gate is satisfied: the [team-server handoff is archived](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server.md).

On 2026-09-05 the human accepted existing two-member production use and skipped
separate disposable-host SSH qualification. The backed-up CoT project then
completed desktop transfer to WTH UCSD, target import, activation-proof return,
source retirement, and canonical/provider-history verification. Its source-only
paused-attempt/recovery fixes are in the closure PR and must pass CI and human
merge normally. The temporary surface freeze is closed. This handoff prepares
supervisor implementation; the supervisor itself is not implemented.

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
  `rcp` wheel, the hashed lock export, a SHA-256 manifest, and, once Phase 3
  creates it, the supervisor wheel. A later merge never cancels an earlier
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

## Ordering and gates

```
Phases 0–1 complete; Phase 2 public transition complete, cleanup remains
        │  first-lab gate satisfied on 2026-09-05
        ▼
Phase 3 (supervisor package) → Phase 4 (cutover) → Phase 5 (deletion) → Phase 6 (lab)
```

The migration ledger and its read-only validator are already on main; there is
no outstanding concurrent-audit dependency. Phase 2's public transition depended
on Phase 1, so the public repository could serve builds immediately. Phases 3
and later are sequential. The former first-lab freeze no longer blocks them;
the documented package, cutover, deletion, and qualification checks still apply.

Sequence any two phases that touch `src/rcp/api/app.py`,
`src/rcp/server_ops/cli.py`, `src/rcp/server_ops/install.py`, or
`.github/workflows/`; these are composition seams, not parallel lanes.

## Dispatch order

Stay with small, coherent file/module assignments rather than assigning an
entire phase to one worker. The integrator owns the full diff and workflow
verification. Suggested short-PR order:

1. Phase 2 cleanup: source access/config and legacy-label compatibility tests.
2. Independent supervisor packaging and wheel/lock/promotion contracts.
3. Release resolution, bounded downloads, hashes, and isolated installation.
4. Checkpoint/journal owner and crash-safe switch with fake-service failures.
5. Restore candidate validation/publication under that same journal.
6. CLI delegation, maintenance admission, doctor/config, and hosted Ubuntu drive.
7. Delete replaced in-app deployment owners; prove every retained console
   operation and direct old-data upgrade still works.
8. Promote the first complete supervisor-bearing release and perform the
   protected lab cutover, then archive this handoff.

Do not run the new installer against an older `stable` lacking supervisor assets
or the required maintenance contract. Refuse with the exact missing requirement;
human promotion of a qualified complete build precedes lab installation. Do not
silently install from main or another prerelease instead.

## Phases

Each phase is one or more pull requests. Each names its owner files, the
behavior it must not change, and the proof that closes it. "Green" means the
baseline checks in `AGENTS.md` plus the phase's own checks.

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

Code status: public-origin transition implemented on main; removal of the
retired private-source creation path is still open.

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

Remaining steps, in order:

1. Remove new private-source key generation/grant pauses and new legacy-label
   emission. Preserve decoding of shipped configurations/archives and explicit
   revocation instructions for an old source key. Do not remove research-project
   deploy keys; those still support private project repositories.
2. Prove an old archive carrying the source label still restores; rerun the
   public-install qualification after that cleanup changes the installer.

Owner files: `src/rcp/server_ops/install.py`, `src/rcp/server_ops/config.py`,
`src/rcp/server_ops/backup*.py`, `src/rcp/server_ops/restore.py` (label
handling only), `docs/server.md` step 7,
`docs/specs/server-and-machine-operations.md`.

Must not change: install behavior on a host that never had a private origin;
backup archive compatibility for archives that carry the old label; any origin
other than the one deliberate deploy-key-to-public transition.

Exit proof: the recorded protection settings and required-check enforcement;
the lab server's `server doctor` showing the
public HTTPS origin and a successful update from it before the key is revoked;
a fresh install on a disposable host with no deploy-key step; an old archive
with the label still restores.

### Phase 3 — the supervisor package

Lands: an independently buildable `rcp_supervisor` distribution with separate
build metadata, version, wheel, and `rcp-supervisor` console script; no import
from `rcp`. Choose the smallest subproject layout compatible with the existing
single-package root build; adding a console entry to the RCP wheel is not
independence. Commands: `fetch`
(manifest, assets, hash verification, `stable` or a named release), `install`
(isolated environment under `releases/<build>/` from wheel plus hashed lock, as
`rcp`), `check` (copy data directory, run the release's `rcp migrate --check`),
`switch` (protected backup, stop, crash-safe local checkpoint of the data
directory and every RCP-owned local state root with an fsynced phase journal,
pointer switch, start, health poll, and on failure restore the checkpoint from
the journal, switch back, start and verify the previous release), `restore`
(unpack the archive into a candidate data directory beside the live one, run
`check` on it, stop, checkpoint the live data directory under the same journal,
atomically publish the candidate into `RCP_DATA_DIR`, start, verify, and roll
back by re-publishing the checkpoint), `self-update`. Event stream in the same
machine-readable shape the CLI uses. The checkpoint is what makes rollback
after a forward migration possible; the old release never reads migrated data.

Owner files: the new supervisor package/build metadata,
`tests/test_supervisor*.py`, `.github/workflows/ci.yml`, and the existing release
asset/promotion verification code. Publish and hash the supervisor wheel and
its required dependency lock as part of the same immutable build; preserve
promotion without rebuilding. Keep normal tests in the existing CI jobs.

Must not change: anything under `src/rcp/`; the server layout from the
2026-08-27 install decision; systemd unit contents.

Exit proof: tests against a fake service that fails health checks, hangs, and
reports a wrong build; tests against a frozen real RCP build installed into a
temporary layout; a test that the package imports nothing from `rcp`; a test
that an interrupted `switch` re-enters and completes rollback from its journal,
including after a forward migration ran; a test that an interrupted `restore`
re-enters and finishes either the publication or the rollback, never leaving a
mixed data directory.

### Phase 4 — cutover: operator commands delegate

Lands: `rcp server install` installs the supervisor and installs the first
release from `stable` instead of building the managed checkout; `rcp server
update` and `rcp server restore` delegate to the supervisor and keep their
wizard presentation; `rcp server supervisor update`; server config gains the
followed release (default `stable`) and an optional pin; `docs/server.md`
rewritten for the new prerequisites and commands.

Settle inside this phase, with these defaults: the one-time bootstrap entry
becomes `uv tool run --from <stable wheel URL> rcp server install`, so the
operator uses `uv` for the bootstrap, alongside the existing OS, systemd, SSH,
Git, age, and privilege prerequisites; restore validation that today lives in
`server_ops/restore.py` moves to the supervisor's `restore` where it concerns
layout and to `rcp migrate --check` where it concerns data. Replace
`restore.py:_git_commit_is_supported`'s RCP-source `git merge-base` dependency
with explicit release/archive and schema compatibility checks. Unknown formats
and schemas still fail closed; preserving project Git does not justify keeping
an RCP source checkout solely for ancestry validation.

The current migration check proves SQLite ledger compatibility, not canonical
graph replay or retained project-file integrity. Preserve those application-owned
checks through an explicit bounded offline CLI entry as needed; do not duplicate
RCP's graph/schema logic in the supervisor or mistake a schema-only check for a
full copied-state rehearsal. Copy/snapshot capture must be consistent, and the
final fenced checkpoint must match the checked boundary or be checked again.

The integration also proves the maintenance handshake from the settled
decisions: no new task or mutation can race the final checkpoint or cutover,
watcher/recovery owners cannot publish during it, and admission stays closed
after a failed or interrupted activation until the supervisor verifies recovery.

Owner files: `src/rcp/server_ops/cli.py`, `install.py`, `config.py`,
`layout.py`, `doctor.py`, `docs/server.md`,
`docs/specs/server-and-machine-operations.md`.

Must not change: the operator-visible command names; the data directory and
credentials layout; backup format.

Exit proof: on disposable Ubuntu 22.04 and 24.04 hosts, install from `stable`,
update to a newer release, force a failing health check and observe automatic
rollback, and restore a protected archive; `server doctor` reports the followed
release and installed supervisor version; no RCP source checkout or JavaScript
build tools are required. Git remains installed for the project/restore drive.

### Phase 5 — delete in-app deployment coordination, retain console operations

Lands: removal of `src/rcp/server_ops/update.py`, `update_checkpoint.py`,
`update_cutover.py`, and `rehearsal.py` once their responsibilities have moved;
remove release-specific control messages, activation-journal commits, and
deployment-journal recovery from `src/rcp/api/app.py`. Keep the private control
server and `control.py` operations for provisioning, transfer, provider checks,
backup capture, and member removal. Keep the narrow admission safety owner, not
the old release coordinator behind a renamed facade. Replace obsolete tests
with supervisor/integration coverage before deleting them; rewrite specs to
describe only the new deployment path.

Owner files: the files above, `tests/test_server_*`, `tests/test_app_*`,
`docs/specs/server-and-machine-operations.md`, `docs/design.md` where it names
the control protocol.

Must not change: backup, provisioning, provider readiness, member removal,
transfer; startup order for everything that is not update or restore.

Exit proof: `create_app` reads no deployment journal; non-deployment CLI
operations still pass through their authenticated connection; the full suite
and old-data job are green. Re-drive the server lifecycle promise in
[S103](../acceptance/S103-server-operations-are-console-operations.md) and the
hosted server-install harness. S36 covers signed packaged-desktop updates and
must not be rewritten as a server-supervisor scenario.

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
Phase 4 reuses `.github/workflows/server-install-live.yml` and
`tests/test_server_install_live.py` on hosted disposable Ubuntu 22.04/24.04
runners. No personally supplied disposable VM is required. Phase 6 is
proven on the persistent lab server only after Phases 3 through 5 are green on
disposable hosts. Never test against the lab server's real data directory
first.
