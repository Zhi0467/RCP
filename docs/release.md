# Build, tag, and release

Phase 1 of
[the supervisor handoff](handoffs/handoff-2026-09-02-external-supervisor-and-release-artifacts.md)
is implemented: CI builds one prerelease per successful merge to `main`, a
human can promote that build without rebuilding it, and a daily workflow prunes
old build prereleases. Servers do not consume these artifacts yet. Until Phase
4 lands, they still build `origin/main` from source exactly as
[the team server guide](server.md) describes.

## The two events

A **build** is automatic. A **release** is a human decision. They are separate
so that merging often costs nothing and releasing stays deliberate.

| | Build | Release |
| --- | --- | --- |
| Trigger | every merge to `main` | a human promotes one build |
| Name | `build/<N>`, `<N>` is the CI run number | `vX.Y.Z` |
| GitHub | prerelease | release; the newest one is `stable` |
| Contents | `rcp` wheel, hashed lock export, SHA-256 manifest | the same files, re-attached, never rebuilt |
| Kept | thirty days | forever |
| Who acts | nobody | a human, never an agent |

## What happens on a merge

1. The ordinary CI jobs run: lint, pytest on 3.11 and 3.12, old-data upgrade,
   web. A build exists only if they all pass.
2. The `build` job builds the wheel once. Its version is
   `<__version__>+build.<N>.g<sha7>`, where `__version__` comes from
   `src/rcp/__init__.py`.
3. The job exports the locked dependencies with hashes, writes a manifest of
   SHA-256 sums, and publishes everything as prerelease `build/<N>`.
4. A later merge never cancels an earlier `main` run. Every merge that passes
   CI gets its own build, however close together they land.

Find a build under the repository's Releases page, filtered to prereleases, or
with:

```bash
gh release list --repo Zhi0467/RCP --limit 1000 --json tagName,isPrerelease --jq '.[] | select(.tagName | startswith("build/")) | .tagName'
```

### If a build's publish step fails

If publishing creates `build/<N>` but fails before every asset uploads, delete
the partial release and tag, then re-run the failed job:

```bash
gh release delete build/<N> --cleanup-tag --yes
```

The workflow refuses to overwrite an existing release so a partial or changed
asset set cannot silently replace the build's original byte identity.

## Bump the version before you release

`src/rcp/__init__.py` holds the **next intended** version. Promotion refuses a
build whose base version differs from the tag you ask for, so:

1. Open an ordinary pull request that changes `__version__` to the version you
   intend to release.
2. Merge it. The build that results carries that base version.
3. Promote that build, or any later one with the same base version.

Do not bump the version in the same pull request as a risky change; keep the
bump reviewable on its own.

## Promote a build to stable

Pick the build. Confirm on GitHub that its CI run is green and that its commit
is the one you mean. Then run the promotion workflow:

```bash
gh workflow run promote.yml --repo Zhi0467/RCP -f build=<N> -f tag=v<X.Y.Z>
```

The workflow verifies the base version, creates release `v<X.Y.Z>` pointing at
the build's commit, and re-attaches the build's assets unchanged. If the version
does not match the tag, it stops with a plain message and creates nothing. The
new release is now `stable`.

Only a human promotes. An agent may prepare the version-bump pull request and
may tell you which build is green; it does not run the workflow.

## How servers pick it up

**Not yet in effect.** Phase 4 of
[the supervisor handoff](handoffs/handoff-2026-09-02-external-supervisor-and-release-artifacts.md)
will connect server updates to promoted artifacts. Today servers still build
`origin/main` from source through the commands in
[the team server guide](server.md); they do not follow GitHub Releases.

## Retention

A scheduled workflow deletes `build/<N>` prereleases older than thirty days
and cleans up their tags. A build that was promoted lives on as its release;
the prerelease entry may still be pruned. If you need to reproduce a build
older than thirty days, promote it before it ages out, or rebuild the commit
locally and accept that the bytes will not be the tested ones.

## Supervisor versions

**Not yet in effect.** The supervisor package and its independent version do
not exist yet. Phase 3 of
[the supervisor handoff](handoffs/handoff-2026-09-02-external-supervisor-and-release-artifacts.md)
will add them. Current build and release assets therefore contain only the RCP
wheel, hashed lock export, and manifest.

## What to check before promoting

- The build's CI run is green on all jobs, including old-data upgrade.
- `__version__` in that build equals the tag you intend.
- The release notes, if you write any, name behavior changes an operator would
  notice: new prerequisites, changed commands, migration time.
- If the change touched `src/rcp/storage/`, a frozen fixture exists for the new
  persistence era, per
  [the schema compatibility decision](decisions/2026-08-27-server-schema-compatibility.md).
