# Build, tag, and release

This guide gives procedures for human maintainers. Release rules and recovery belong to
[server operations](specs/server-and-machine-operations.md#release-selection-deployment-and-automatic-recovery).

## Find a build

Find a green main-branch build and check its exact commit:

```bash
gh release list --repo Zhi0467/RCP --limit 1000 --json tagName,isPrerelease --jq '.[] | select(.tagName | startswith("build/")) | .tagName'
```

### If a build's publish step fails

After checking that the release is incomplete, delete its partial release and tag
and re-run the failed publishing job:

```bash
gh release delete build/<N> --cleanup-tag --yes
```

## Bump the version before you release

Update `src/rcp/__init__.py` in a PR. Match the native versions in
`web/package.json`, `web/package-lock.json`, `web/src-tauri/Cargo.toml`, and
`web/src-tauri/Cargo.lock`.
Merge it before selecting the build to promote. For supervisor changes, bump
`supervisor/src/rcp_supervisor/__init__.py` independently.

## What to check before promoting

Check that all CI jobs, including **old-data upgrade**, passed and that the
build's base version matches the intended `vX.Y.Z` tag. Each **installed upgrade**
job seeds a previous release with a coach on its own machine, an ended Experiment
and a queued nightly consolidation. After the update the candidate must serve the
space-wide reads before any project opens, launch-validate every queued turn and
admit one due consolidation. Review release notes for operator-visible changes,
prerequisites and migration time.

Run the egm drift sweep: one `egm-codebase-details` review per file in
`docs/specs/` plus `docs/design.md`. Fix confirmed drift before promoting.

Build and download a desktop candidate from that build:

```bash
gh workflow run desktop-candidate.yml --repo Zhi0467/RCP -f build=<N>
gh run download <run-id> --repo Zhi0467/RCP -n desktop-app
ditto -x -k desktop-assets/RCP-v<X.Y.Z>-macos-arm64.zip .
mv RCP.app "RCP Candidate.app"
```

Keep it outside `/Applications` and follow the
[disposable candidate checks](desktop.md#build-and-test-a-release-candidate).
Record the successful candidate run id for promotion.

Run the complete installed-update rehearsal on a private copy of each team's
state. Candidate-only `update-rehearsal` remains useful for diagnosis; its `ready`
result cannot qualify promotion.

Use a stopped whole-root checkpoint containing the data directory and every local
canonical root. Keep the copy outside the checkout in a mode-0700 operator
folder. Preserve paths, numeric ownership, metadata, links and special nodes;
never migrate the live roots for qualification. The private input format and
snapshot-digest checks are owned by
[`validate_private_state`](../tests/server_update_rehearsal.py). It names the
verified source manifest, captured root paths, original destinations and expected
space, project and canonical-history identities. Use the existing
[checkpoint copier](../supervisor/src/rcp_supervisor/checkpoint.py); do not rebuild
records with current models. Remote roots remain outside this local proof.

Download the genuine source release and exact candidate build, retaining each
verified bundle's `.receipt.json` from
[`download_installed_release`](../tests/server_upgrade_harness.py) and the
candidate bundle receipt. On a Linux x86_64 host with KVM and the QEMU tools from
[VM preflight](../tests/supervisor_reboot_vm.py), run:

```bash
uv run python tests/server_update_rehearsal.py \
  --base /private/qualification/source \
  --candidate /private/qualification/candidate \
  --private-state /private/qualification/private-state.json \
  --output /private/qualification/full-update \
  --procedure graceful \
  --confirm-disposable I_UNDERSTAND_THIS_CREATES_DISPOSABLE_VMS
```

Choose `--procedure stopped-source` only when that is the intended server update
procedure. The tool provisions verified wheels and dependencies online, then
disables guest egress before copying private bytes. It runs the real installed
supervisor update, application update, injected failure, exact rollback and
successful retry. Failure exits nonzero and produces no passing `result.json`.
Keep all VM disks, logs, snapshots and full reports private; they may contain
credentials. They are not CI artifacts. Retain needed evidence before removing
only the owned qualification directory.

## Promote a build to stable

Only a human runs promotion. Generate the public receipt from the complete private
reports and tested desktop candidate run; do not hand-build a readiness receipt:

```bash
uv run --no-project python packaging/release_build.py qualification-receipt \
  --report /private/qualification/full-update/result.json \
  --manifest /private/qualification/candidate/manifest.sha256 \
  --desktop-candidate-run <run-id> --output receipt.json
gh workflow run promote.yml --repo Zhi0467/RCP \
  -f build=<N> -f tag=v<X.Y.Z> -f desktop_candidate_run=<run-id> \
  -f receipt="$(cat receipt.json)"
```

Repeat `--report` for each team. The tool cannot discover an omitted team. The
public receipt contains only update identities, procedure, status and report
hashes. Promotion rejects version-1 readiness receipts, incomplete or failed
updates, different candidate bytes and a different desktop run. The generator
also rejects synthetic CI fixture reports. See
[qualification scope](specs/server-and-machine-operations.md#release-selection-deployment-and-automatic-recovery).
Check the workflow summary and published asset bytes before telling operators
to update. Promotion stays before deployment; no live production canary is
required.

## How servers pick it up

Follow [server update](server.md#update-the-server-release) and, if requested,
the separate supervisor update command there.

## Retention

Promote a build you need to keep before the retention window in
[`packaging/release_build.py`](../packaging/release_build.py) expires.

## Supervisor versions

Check the selected release contains both wheels, both hashed dependency lock
exports and `manifest.sha256`. Follow [startup recovery](specs/server-and-machine-operations.md#selection-and-startup-guard)
when checking an installed supervisor. A wheel alone does not prove reboot recovery.
