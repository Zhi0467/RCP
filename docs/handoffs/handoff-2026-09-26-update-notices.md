# Tell people when an update is out

Status on 2026-09-26: design confirmed by the human. Nothing is implemented.

- Implemented: nothing yet.
- Remains: changes 1–7 below, on one pull request.
- Settled (human, 2026-09-26):
  - Local source installs (the desktop app and the local Web app) follow
    promoted releases, not `main`. See
    [the decision record](../decisions/2026-09-26-desktop-installs-follow-releases.md).
  - No Apple signing and no paid Apple account.
  - Every promoted release also publishes an unsigned prebuilt macOS app. It
    becomes the normal desktop install. Building from source stays for
    developers. This ships in the same pull request.
  - Notify only. There is no one-click update. Each notice has a Copy command
    button.
  - The notice is app-wide, not only in Settings.
- Closure: the pull request merges, the checks in
  [Verification](#verification) pass, the spec sentences are added, and this
  handoff is deleted.

## Why

Today nothing tells anyone that an update exists.

- Desktop: the Tauri updater is compiled in but disabled. No build sets its
  endpoint or key, and no UI calls `check_for_update`.
- Team server: Settings → Server has an "Update is available" label that never
  turns on. `_inspect_source` in `server_ops/doctor.py` reports the installed
  release as its own upstream, so the state is always `aligned`.
- So a security fix reaches team members only when an operator happens to run
  `rcp server update`.

There is also a version mismatch. Team servers install numbered releases.
Desktop apps are built from whatever commit the checkout is on. Releases carry
no desktop app at all, so every desktop user needs Rust, the Xcode tools, Node,
and uv, and a build of several minutes.

## Design

### 1. One release check

- New module `src/rcp/release_check.py`. It asks GitHub for the latest
  published stable release (`/releases/latest`). It applies the supervisor's
  stable rules: not a draft, not a pre-release, a `vX.Y.Z` tag.
- It runs inside `rcp serve`, for personal and team spaces alike. The first
  check runs shortly after startup, then every 6 hours. The result is cached
  in memory. The interval and timeout live in `limits.py`.
- It is advisory. It never downloads or installs anything. The supervisor
  keeps its own verified download path.
- It does not import the supervisor's code. The supervisor is a separate wheel
  that desktop installs do not have, and this check needs none of its download
  verification. It reads one tag.
- A failed check shows no banner. Settings shows "Could not check for updates"
  and the time of the last successful check.
- `RCP_UPDATE_CHECK=off` turns the network call off, for servers without
  internet access or for privacy. Each check sends GitHub the machine's IP
  address and nothing else.

### 2. What is compared

- Team space: the installed release tag, from the selected release receipt,
  against the latest tag. If the server config pins a release, there is no
  banner. Settings says "Pinned to vX.Y.Z".
- Local install: the running `rcp.__version__` against the latest tag. Only a
  strictly newer release counts. A checkout ahead of the latest release, such
  as a development build from `main`, sees nothing.
- A local install is one of two kinds. A prebuilt app runs a frozen backend
  (`sys.frozen`). Anything else is a source checkout. The kind picks the
  banner's action.

### 3. One endpoint

- `GET /api/update-notice` returns the install kind (`team_server`,
  `prebuilt_app`, or `source_checkout`), the current and latest versions, the update command, the
  last check time, and a status: `update_available`, `current`, `pinned`,
  `unchecked`, `failed`, or `off`.
- Any signed-in member may read it. It carries no secrets.
- Settings → Server reads the same cached result, so its existing label starts
  working. `rcp server doctor` runs the same lookup live, so the CLI and the UI
  agree.

### 4. The banner

- App-wide: in the app shell, above every view, in the existing banner style.
- Team space: "RCP v0.4.3 is out. This team server runs v0.4.2." Then "The
  server operator updates it with:", the command `sudo rcp server update`, and
  a Copy command button.
- Prebuilt app: "RCP v0.4.3 is out. This app is v0.4.2." Then a Download
  v0.4.3 button that opens the desktop release page in the browser. It shows
  only once that release exists (change 7); until then the banner waits.
- Source checkout: "RCP v0.4.3 is out. This app is built from v0.4.2." Then the
  command `scripts/update-from-source v0.4.3`, run in the checkout, and a Copy
  command button.
- Dismiss hides the banner for that release in that browser. A newer release
  shows it again. Dismissal lives in `localStorage`; losing it only shows the
  banner again.
- Every team member sees the team banner. Team spaces have no operator role,
  and a member who is not the operator can pass the message on.
- Limitation: in the desktop app, the page comes from whichever space is open.
  The local-install banner shows only in the personal space, and the team
  banner only in a team space.

### 5. One update command for source installs

New checked-in script `scripts/update-from-source <tag>`. It:

1. refuses if the checkout has uncommitted changes;
2. fetches tags and checks out the tag, detached;
3. runs `npm --prefix web ci`, builds `web/dist`, and runs `uv sync`;
4. on macOS with Rust installed, rebuilds the desktop app
   (`desktop:build-dev`) and prints the lines that replace
   `/Applications/RCP.app` and relaunch it. It does not replace a running app.

The user runs it in a terminal; it is not a button. It stops at the first
failed step and names that step.

The README's source-install section changes from "clone `main`" to "clone,
then check out the latest release", and points to this script for updates.

### 6. Align the version-mismatch messages

- `team_session.rs` tells a desktop that is too old to "Update and rebuild RCP
  desktop from current origin/main". It should name the latest release and the
  script instead.
- `docs/desktop.md`: state that Apple signing is not planned, and why.

### 7. A prebuilt unsigned app in every release

- The app cannot go into the `vX.Y.Z` release itself. Installed supervisors
  accept a release only if it holds exactly its five server files
  (`_bundle_names` in `supervisor/.../releases.py`). One more file would break
  `rcp server update` on every existing team server.
- So `promote.yml` also publishes a companion release, `desktop-vX.Y.Z`, from
  the same commit. It is marked pre-release and not latest. GitHub's
  `/releases/latest` skips pre-releases, so old supervisors and the new release
  check still see only `vX.Y.Z`.
- A new macOS job in `promote.yml` builds the app with
  `npm --prefix web run desktop:build` (frozen backend inside), runs
  `packaging/smoke-backend.py` against that exact bundle, zips it with `ditto`
  so macOS metadata survives, and uploads the zip and its SHA-256 checksum.
- Apple Silicon only, matching the supported platforms.
- The app's version is stamped from the release tag at build time. Today
  `web/package.json` says `0.3.2` while RCP is `0.4.2`.
- If the desktop job fails, the `vX.Y.Z` server release still stands. The
  prebuilt-app banner waits until `desktop-vX.Y.Z` exists.
- The release notes and the README explain the one-time approval: the first
  launch is blocked, then System Settings → Privacy & Security → Open Anyway.
  A manually downloaded update asks again.
- The README's desktop section leads with the download. The source build moves
  below it, for developers.

## Out of scope

- One-click update for team servers. The server runs as `rcp`, and updating
  needs root. A button would need a new privileged channel and an operator
  role that team spaces do not have. It would be its own project.
- One-click desktop update through the Tauri updater. It would remove the
  repeated approval for prebuilt apps, but whether it works without Apple
  signing is untested. Revisit after the prebuilt app has shipped.
- Apple signing.

## Verification

- Python: `release_check` against a fake GitHub server: newer, equal, older,
  pre-release, malformed, timeout, and `off`. The comparison for a team
  server, a pinned server, and a local install. The endpoint's shape. The
  GitHub base URL is overridable only for tests.
- Web: the banner for each status, dismissal per release, and the copy button.
- Served app: a team fixture whose installed receipt is one release behind,
  with a fake GitHub server, shows the banner in every view, and the Settings
  label agrees. A personal space served from an older version shows the local
  banner.
- Script: on a throwaway clone at an older tag, it ends on the new tag with a
  built `web/dist`. It refuses a dirty checkout.
- Native: rebuild Tauri for the message change and run the `team_session`
  tests.
- Prebuilt app: run the new promote job on a fork or a throwaway tag. Download
  the zip on a Mac, approve it once, and open it. The project index opens, the
  bundle's backend passes the smoke test, and the banner shows for an older
  build. Check that `/releases/latest` still returns `vX.Y.Z`, and that an
  installed supervisor's dry-run update accepts that release.
- Specs at closure: `docs/specs/server-and-machine-operations.md` and
  `docs/specs/api-web-and-desktop-projections.md`.
