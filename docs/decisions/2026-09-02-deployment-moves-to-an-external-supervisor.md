# Servers install promoted release artifacts through an external supervisor

**Status:** accepted by the human on 2026-09-02; clarified on 2026-09-05 to
retain the private CLI connection for non-deployment operations, and on
2026-09-06 to require automatic recovery after interruption or reboot. Amends
[the update-channel decision](2026-08-27-main-is-the-server-update-channel.md)
and [the install-and-update privilege decision](2026-08-27-source-server-install-and-update-privilege.md)
as stated at the end of this file. Implementation is planned in
[the supervisor handoff](../handoffs/handoff-2026-09-02-external-supervisor-and-release-artifacts.md).
Until its phases land, servers still build `origin/main` as the
[operations spec](../specs/server-and-machine-operations.md) describes.

## Decision

RCP stops deploying itself. A server no longer fetches and builds `origin/main`
from inside the running application. CI builds one release artifact per merge, a
human promotes one build to `stable` when they choose, and a small external
supervisor installs, switches, verifies, and rolls back.

### Builds and releases

A **build** happens on every merge to `main`. CI builds the `rcp` wheel once,
exports the locked dependency set with hashes, and publishes both with a SHA-256
manifest as a GitHub prerelease tagged `build/<N>`, where `<N>` is the CI run
number. Ten merges a day produce ten builds. Nobody acts on a build.

A **release** is a human act. A human promotes one build to `stable` by creating
the GitHub Release `vX.Y.Z` from it. Promotion re-attaches the build's exact
assets and never rebuilds, so the artifact CI tested is the artifact a server
installs. There is no release cadence; the human releases when they want.

**`stable` is the newest non-prerelease GitHub Release.** Every server follows
`stable`. An operator may pin a server to one named release to reproduce a
problem or to roll back. No server follows builds or `main`.

**Version identity.** `src/rcp/__init__.py` carries the next intended version.
A build's wheel version is `<that version>+build.<N>.g<sha7>`. Promotion
requires the build's base version to equal the tag it is promoted under, so a
version bump is an ordinary pull request that precedes the promotion. The
health endpoint and `rcp --version` report the base version, the build number,
and the commit.

**Retention.** A scheduled workflow prunes builds older than thirty days.
Promoted releases are never pruned.

### The supervisor

The supervisor is a Python package in this repository, `rcp_supervisor`, with
its own version. It imports nothing from `rcp`, the same rule that governs the
shipped `remote_*.py` modules. Once the package exists, CI builds its wheel and
attaches it to every build; builds made before that carry only the `rcp`
assets. Its version changes only when its own logic changes, so a supervisor
release is simply a build whose supervisor version differs from the installed
one. `rcp server install` installs it once; `rcp server supervisor update`
reinstalls it from the current `stable` release when the version differs. RCP
releases never force a supervisor release.

The supervisor keeps the privilege split of the 2026-08-27 install decision. It
is the narrow root coordinator for systemd and the current-release pointer. It
installs each artifact into the `rcp` account's `releases/<build>/` as `rcp`.
Node.js and npm leave RCP's source-build prerequisites once the source path is
deleted. Git remains necessary for research-project checkouts, provisioning,
and repository restore; individual providers may still require Node.js.

**Update sequence.** Download the release manifest and assets. Verify every
hash. Create `releases/<build>/` with an isolated environment installed from
the wheel and the hashed lock. Run `rcp migrate --check` against a copy of the
data directory. Take the protected backup. Close admission and stop the
service. With nothing able to mutate, take a crash-safe local checkpoint of the
data directory and every RCP-owned local state root, with a phase journal
fsynced beside it; this is the same pre-switch checkpoint the current
coordinator takes, and it is distinct from the protected backup. Switch the
current-release pointer. Start. Poll health until the reported build matches,
or a timeout passes. On any failure: stop, restore the checkpoint from its
journal, switch back, start the previous release, verify it, and report both
the failed target and the restored release. That is rollback, and it is never
silent. A forward migration that ran before the failure is undone by the
checkpoint, not by the old release reading migrated data. Re-entry after a
crash keeps the service stopped and completes whatever the journal says was in
progress.

**Automatic recovery**, confirmed by the human on 2026-09-06: systemd invokes
the supervisor's recovery path before allowing RCP to start after a reboot.
Recovery resumes an already-authorized operation from its local journal,
checkpoint, and installed artifacts; it does not select a newer release or
require GitHub access. Unknown or inconsistent recovery state keeps RCP stopped
and reports the required operator action. Once admission has reopened, recovery
must never restore the pre-deployment checkpoint and discard subsequently
accepted work. The supervisor owns this startup guard; RCP does not interpret
deployment journals.

Qualification must interrupt update and restore at durable journal boundaries
and actually reboot disposable Ubuntu 22.04 and 24.04 machines. A changed Linux
boot ID, unattended recovery, selected release/data verification, and proof that
admission remained closed until verification are required receipts. Include a
forward migration, interrupted rollback, network-unavailable recovery, invalid
journal refusal, and preservation of work accepted after reopening. Process
restart tests alone do not prove this contract.

**Restore** unpacks a protected archive into a candidate data directory beside
the live one and runs the release's `rcp migrate --check` on it. It then stops
the service, checkpoints the live data directory under the same journal,
atomically publishes the candidate into the configured `RCP_DATA_DIR`, starts,
verifies, and rolls back by re-publishing the checkpoint. A crash mid-restore
re-enters and finishes either the publication or the rollback; it never leaves
a mixed data directory.

**Contract with RCP**, fixed and extended only by adding fields:

- `GET /api/health` reports version, build number, commit, and the storage
  migration ledger head.
- `rcp migrate --check` exits zero only when the data directory is at the
  current schema or every pending migration is known; `rcp migrate` applies the
  ledger without serving.
- Every server command emits the existing machine-readable event stream.
- The application retains a narrow authenticated maintenance boundary that
  closes new-work admission and proves existing work has settled before stop.
  A health snapshot alone is not that fence. The supervisor owns release
  selection, checkpoints, cutover journals, and rollback; the application owns
  its live-task and mutation admission rules.

### Going public

The repository becomes public with a protected `main` inside this work, not at
a later sharing milestone. It stays one bundled transition: branch protection
requiring the named CI jobs, and retirement of the private-source deploy key
together with its install pause and backup label. An installation that already
uses a deploy-key SSH origin is converged once to the public HTTPS origin, and
proven to update from it, before its key is revoked; a public repository does
not make an SSH remote credential-free. Once public, release assets download
without a token, so no server credential replaces the deploy key.

### What the application loses and keeps

Deleted from the application when the supervisor owns cutover: the in-process
update and restore coordinators, their release-specific control messages,
restore activation journals, deployment-specific deferred-start recovery, and
the fenced candidate rehearsal that ran a copy of the new release inside the
old one. Keep the safety fence until the supervisor path proves equivalent
closed-admission and crash-recovery behavior; deletion is not permission to
restart into partially migrated state.

The human explicitly retained the private authenticated CLI connection on
2026-09-05. Project provisioning, transfer upload/import, provider checks,
backup capture, and member removal still use it. Neither the whole
`server_ops/control.py` module nor its versioned protocol is a deletion target.
This remains separate from the deliberately thin desktop/server handshake.

Kept: forward-only migrations, the promise that every server-era database
upgrades directly, the old-data CI job and its frozen fixtures, protected
backups, the crash-safe pre-switch checkpoint and its phase journal, systemd,
the split operator and service privilege, and the operator
commands `rcp server update` and `rcp server restore`, which now delegate to
the supervisor.

## Why

The running release currently drives a newer release it has never seen, through
a private protocol with a compatibility window. Both audits in the week of
2026-09-01 found their most severe defects in the update and restore paths, and
the attempted correction introduced a restore-activation lock regression that the
tests could not see because activation was exercised outside the enclosing
admission lifecycle. An application that owns its own replacement is a fragile
boundary and a hard one to test.

Building on the server means the deployed bytes are whatever that machine's
build produced from whatever commit `main` pointed at that minute. Building once
in CI and promoting the tested artifact makes the deployed bytes the tested
bytes, gives every server a version that is a name rather than a commit, and
removes the JavaScript toolchain from the server.

A separate supervisor can recover when the replacement release fails to start.
It relies on package identity, the migration CLI, health, and the narrow
maintenance boundary, rather than a running release interpreting its successor's
deployment journals. Fake-service tests cover coordinator failure behavior;
real integration tests still prove admission, migration, and recovery safety.

Artifacts were rejected on 2026-08-27 because the repository was private with no
public transition scheduled and CI produced no artifacts. Going public is now
part of this work and CI already builds the wheel, so that reason has expired.

## Rejected alternatives

- Keep in-app self-update and add tests: keeps the cross-version protocol and
  the app's ownership of its own replacement; the defect history says the seam
  itself is the problem.
- An external orchestrator that still builds RCP from source on the server:
  removes the in-app coupling but keeps the JavaScript build toolchain and the
  "which commit" ambiguity on every server.
- A tag or release per merge: turns ten merges a day into ten releases nobody
  chose; builds are cheap files, releases are decisions.
- Servers that follow `main` or builds: reintroduces production configuration
  as development state, which the 2026-08-27 decision already rejected.
- A shell-script supervisor: harder to test against a fake service and outside
  the repository's existing fixture and pytest tooling.
- A separate repository for the supervisor: another release line and another
  place for the contract to drift.
- A permanent `release` branch: a second drifting line and a separate promotion
  event, rejected on 2026-08-27 and not changed here.

## Amendments to earlier decisions

[The update-channel decision](2026-08-27-main-is-the-server-update-channel.md):
`main` remains the only development target and the source of every release.
Servers no longer consume commits from `origin/main`; they consume promoted
releases built from those commits. Going public moves from "before external
sharing" into this handoff. Everything else in that decision stands.

[The install-and-update privilege decision](2026-08-27-source-server-install-and-update-privilege.md):
per-commit source builds at `releases/<commit>/` become per-build artifact
installs at `releases/<build>/`, and its rejected alternative "Package or
download release artifacts" is adopted. The disposable bootstrap, the dedicated
`rcp` account and its layout, the narrow root coordinator, and the loopback
service stand.
