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
build's base version matches the intended `vX.Y.Z` tag. Review release notes for
operator-visible changes, prerequisites and migration time.

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

Rehearse the candidate against copies of each team's data before promotion.
Run as the server service account. The command leaves the service running:

```bash
RCP_REHEARSAL_DIR=$(mktemp -d /home/rcp/rcp-rehearsal-XXXXXX)
gh release download build/<N> --repo Zhi0467/RCP -D "$RCP_REHEARSAL_DIR" -p 'rcp-*.whl' -p requirements.lock.txt -p manifest.sha256
(cd "$RCP_REHEARSAL_DIR" && sha256sum --check --ignore-missing manifest.sha256)
umask 077
export PYTHONDONTWRITEBYTECODE=1 UV_CACHE_DIR="$RCP_REHEARSAL_DIR/uv-cache"
uv venv -q "$RCP_REHEARSAL_DIR/venv" --python 3.12
uv pip install -q --python "$RCP_REHEARSAL_DIR/venv" -r "$RCP_REHEARSAL_DIR/requirements.lock.txt"
uv pip install -q --python "$RCP_REHEARSAL_DIR/venv" --no-deps "$RCP_REHEARSAL_DIR"/rcp-*.whl
"$RCP_REHEARSAL_DIR/venv/bin/python" -I -m rcp.server_ops.deployment update-rehearsal - > "$RCP_REHEARSAL_DIR/rehearsal.json" <<EOF_REHEARSAL
{"version":1,"data_dir":"/home/rcp/rcp-server/data","output_dir":"$RCP_REHEARSAL_DIR/out"}
EOF_REHEARSAL
printf '{"manifest_sha256":"%s","status":"%s"}\n' \
  "$(sha256sum < "$RCP_REHEARSAL_DIR/manifest.sha256" | cut -c1-64)" \
  "$("$RCP_REHEARSAL_DIR/venv/bin/python" -I -c 'import json, sys; print(json.load(sys.stdin)["status"])' < "$RCP_REHEARSAL_DIR/rehearsal.json")"
```

The rehearsal refuses when a copied record or cache breaks a space-wide read;
see [copied-state reads](specs/server-and-machine-operations.md#application-boundary-and-local-checkpoint).
CI's web job also browses seeded rehearsed data; that journey covers no real records.
Inspect the result and any backup warnings, including omitted projects. When
the result is `ready`, save the printed line on your machine as
`rehearsal-<team>.json`. Retain needed diagnostics before removing this exact
temporary directory:

```bash
rm -rf "$RCP_REHEARSAL_DIR"
```

See [application proof](specs/server-and-machine-operations.md#application-boundary-and-local-checkpoint)
for the qualification boundary. A green seeded CI run cannot prove real records.

## Promote a build to stable

Only a human runs promotion. Combine every team's rehearsal line and the tested
desktop candidate run into the qualification receipt, then promote:

```bash
jq -s --arg run <run-id> '{version: 1, desktop_candidate_run: $run, rehearsals: .}' \
  rehearsal-*.json > receipt.json
gh workflow run promote.yml --repo Zhi0467/RCP \
  -f build=<N> -f tag=v<X.Y.Z> -f desktop_candidate_run=<run-id> \
  -f receipt="$(cat receipt.json)"
```

Include one line per team; promotion cannot tell a team is missing. The receipt
is public, so it carries no warnings or other fields. Promotion refuses any
other field, no rehearsal, one that did not pass or tested other bytes, or
another desktop run; see
[release selection](specs/server-and-machine-operations.md#release-selection-deployment-and-automatic-recovery).
Check the workflow summary, which keeps the receipt, and the published release
before telling operators to update.

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
