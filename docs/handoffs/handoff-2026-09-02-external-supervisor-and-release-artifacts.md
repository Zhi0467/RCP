# External supervisor and release artifacts handoff

Date: 2026-09-02
Status: active, human-confirmed on 2026-09-02. The Phase 2 code half is
implemented on branch `deploy/phase2-public-origin`; the lab update from the
public origin, source deploy-key revocation, the later removal pull request, and
the fresh-install and old-archive proofs remain. The decisions are settled in
[the supervisor decision](../decisions/2026-09-02-deployment-moves-to-an-external-supervisor.md)
and repeated in the next section so this file stands alone. Phases 3 through 6
wait for
[the dev-team-space-and-server handoff](handoff-2026-08-27-dev-team-space-and-server.md)
to meet its closure condition and be archived, because the human has frozen new
team and server lifecycle surface until that first lab deployment is closed.

Closure condition, all of it:

1. Phase 5 deletions are verified: `create_app` reads no update or restore
   journal, `src/rcp/server_ops/control.py` and its protocol are gone, and the
   full suite is green.
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
- Servers install with `uv`; Node.js, npm, and Git leave the server
  prerequisites when the source path is deleted.
- Forward-only migrations, direct upgrade from every server-era database, the
  old-data CI job, protected backups, systemd, and the operator and service
  privilege split all stay.

## Ordering and gates

```
Phase 0 (contract)  ─┐
Phase 1 (builds)     ├─ may run while the lab handoff is open
Phase 2 (go public) ─┘
        │  gate: dev-team-space-and-server handoff archived
        ▼
Phase 3 (supervisor package) → Phase 4 (cutover) → Phase 5 (deletion) → Phase 6 (lab)
```

Phase 0 depends on the storage migration ledger being complete with its
read-only validator, which is packet S of the concurrent complexity-audit
remediation pull request. Phase 1 depends on nothing. Phase 2 depends on
Phase 1, so the first thing a public repository does is serve builds. Phases 3
and later are sequential. Do not start Phase 3 early on the argument that it is
"only a new package"; it is new server lifecycle surface and the freeze applies.

Sequence any two phases that touch `src/rcp/api/app.py`,
`src/rcp/server_ops/cli.py`, `src/rcp/server_ops/install.py`, or
`.github/workflows/`; these are composition seams, not parallel lanes.

## Phases

Each phase is one or more pull requests. Each names its owner files, the
behavior it must not change, and the proof that closes it. "Green" means the
baseline checks in `AGENTS.md` plus the phase's own checks.

### Phase 0 — contract the supervisor will rely on

Lands: `GET /api/health` adds `build`, `commit`, and `schema_ledger_head`
beside `version`; `rcp --version` prints the same three facts; `rcp migrate
--check` and `rcp migrate` run the storage ledger without serving and exit
nonzero on any unknown state; both emit the machine-readable event stream.

Owner files: `src/rcp/__init__.py`, `src/rcp/__main__.py`,
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

Lands: a `build` job in `.github/workflows/ci.yml` on `push` to `main` that
builds the `rcp` wheel with the `+build.<N>.g<sha7>` local version, runs
`uv export --frozen` with hashes, writes a SHA-256 manifest, and creates
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

Exit proof: two merges landing within one minute yield two downloadable builds,
neither run cancelled; one promotion
yields a `stable` release whose assets are byte-identical to the build's,
proven by the manifest hashes; a promotion whose base version mismatches the
tag fails with a plain message; the prune workflow's dry run lists only builds
past the window.

### Phase 2 — public repository and protected `main`

Code status: implemented on branch `deploy/phase2-public-origin`.

Lands: the bundled transition already designed in the 2026-08-27 update-channel
decision, in this order. Repository public. Branch protection on `main`
requiring the named CI jobs, rejecting direct pushes and failed or missing
checks. A one-time origin migration for installations that still use a
deploy-key SSH origin: `prepare_source_access` and `converge_source_checkout` in
`server_ops/install.py` refuse a mismatched origin today and gain exactly one
deliberate transition, deploy-key SSH to the public HTTPS origin, with the
machine config's `authentication` updated in the same step. The persistent lab
server is migrated and proven with one `rcp server update` from the public
origin. Only then are the `grant_needed` install pause, the `source_ed25519`
key material, and the `rcp-source:<id>` backup label removed together and the
lab server's deploy key revoked. Until Phase 4, servers keep building from
source; they simply fetch it from the public origin.

Remaining human steps, in order:

1. The persistent lab server is migrated and proven with one `rcp server update`
   from the public origin.
2. The lab server's deploy key revoked.
3. The `grant_needed` install pause, the `source_ed25519` key material, and the
   `rcp-source:<id>` backup label removed together in the later pull request.
4. A fresh install on a disposable host with no deploy-key step; an old archive
   with the label still restores.

Owner files: `src/rcp/server_ops/install.py`, `src/rcp/server_ops/config.py`,
`src/rcp/server_ops/backup*.py`, `src/rcp/server_ops/restore.py` (label
handling only), `docs/server.md` step 7,
`docs/specs/server-and-machine-operations.md`.

Must not change: install behavior on a host that never had a private origin;
backup archive compatibility for archives that carry the old label; any origin
other than the one deliberate deploy-key-to-public transition.

Exit proof: a live enforcement record (a direct push to `main` refused, a PR
with a failed check blocked); the lab server's `server doctor` showing the
public HTTPS origin and a successful update from it before the key is revoked;
a fresh install on a disposable host with no deploy-key step; an old archive
with the label still restores.

### Phase 3 — the supervisor package

Lands: `src/rcp_supervisor/` with its own `pyproject` entry, version, and
`rcp-supervisor` console script; no import from `rcp`. Commands: `fetch`
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

Owner files: `src/rcp_supervisor/**`, `tests/test_supervisor*.py`,
`pyproject.toml`, `.github/workflows/ci.yml` (test job, and adding the
supervisor wheel to the `build` job's assets).

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
operator needs only `uv`; restore validation that today lives in
`server_ops/restore.py` moves to the supervisor's `restore` where it concerns
layout and to `rcp migrate --check` where it concerns data.

Owner files: `src/rcp/server_ops/cli.py`, `install.py`, `config.py`,
`layout.py`, `doctor.py`, `docs/server.md`,
`docs/specs/server-and-machine-operations.md`.

Must not change: the operator-visible command names; the data directory and
credentials layout; backup format.

Exit proof: on disposable Ubuntu 22.04 and 24.04 hosts, install from `stable`,
update to a newer release, force a failing health check and observe automatic
rollback, and restore a protected archive; `server doctor` reports the followed
release and installed supervisor version; no Node or Git on the host.

### Phase 5 — delete the in-app control plane

Lands: removal of `src/rcp/server_ops/update.py`, `update_checkpoint.py`,
`update_cutover.py`, `rehearsal.py`, `control.py`, and the update and restore
gates, private control server, activation journal commit, and deferred-start
recovery from `src/rcp/api/app.py`; removal of their tests; spec sections
rewritten to describe only the supervisor path. This resolves the audit's
`create_app` finding by deletion rather than extraction.

Owner files: the files above, `tests/test_server_*`, `tests/test_app_*`,
`docs/specs/server-and-machine-operations.md`, `docs/design.md` where it names
the control protocol.

Must not change: backup, provisioning, provider readiness, member removal,
transfer; startup order for everything that is not update or restore.

Exit proof: `create_app` reads no update or restore journal; `grep -r
control.sock src/` is empty; the full suite is green; the old-data job is
green; acceptance scenario S36 ("updating never interrupts work") is
re-driven or rewritten for the supervisor path and its status updated.

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

Phases 1 and 2 are proven on GitHub itself. Phases 3 and 4 are proven on
disposable Ubuntu 22.04 and 24.04 hosts; the existing
`tests/test_server_install_live.py` harness is the starting point. Phase 6 is
proven on the persistent lab server only after Phases 3 through 5 are green on
disposable hosts. Never test against the lab server's real data directory
first.
