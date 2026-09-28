# Install and update the Mac app without Open Anyway

Date: 2026-09-28
Status: design settled with the human on 2026-09-28. Nothing is implemented.
Split out of the install-and-push design; push notifications stay in their own
handoff and PR. One PR delivers everything below.

Close this handoff when all three hold on real hardware:

- a fresh Mac installs RCP with one pasted command and opens it with no
  System Settings approval;
- running the same command again over an installed app replaces it in place;
- an app installed from release N shows an Update button for release N+1, and
  clicking it replaces and relaunches the app with no approval.

## What it does

1. **One-command install.** The README's first install step becomes one
   `curl … | sh` line. A file that `curl` downloads has no quarantine flag, so
   macOS does not block the first launch.
2. **One-click update.** The Tauri updater, already wired in the shell, is
   switched on. The update notice's Update button replaces and relaunches the
   app. The updater downloads the file itself, so it is not quarantined either.

## Settled

### Release layout

- Keep two releases per version. Installed supervisors accept a stable
  `vX.Y.Z` release only when it holds exactly their five files
  (`supervisor/src/rcp_supervisor/releases.py`), so the app cannot join it
  without breaking every server that has not updated its supervisor by hand.
  Merging the releases is separate work.
- The app stays in the `desktop-vX.Y.Z` pre-release. Pre-release status keeps it
  out of GitHub's single Latest slot, which servers read.

### Install script

- `scripts/install-macos.sh` is a checked-in file. The README command fetches
  it from `main` on `raw.githubusercontent.com`. It depends only on release
  asset names, so a script newer than the release is safe.
- It reads the version from the redirect of `releases/latest`, then downloads
  `desktop-vX.Y.Z/RCP-vX.Y.Z-macos-arm64.zip` and its `.sha256`. No GitHub API
  and no JSON parsing. A missing desktop release stops it with a clear message.
- It refuses anything but Apple Silicon macOS 13 or later.
- It verifies the `.sha256` before touching `/Applications`.
- It installs only to `/Applications/RCP.app`. If `/Applications` is not
  writable, it stops and says to run it from an admin account. It never calls
  `sudo` and never falls back to another folder.
- It unpacks next to the old app, checks the result, and swaps it in with one
  `mv`. Any failure leaves the old app untouched.
- If RCP is running, it asks the person to quit with Cmd+Q and stops. It never
  kills the app or its backend.
- The manual zip download stays documented as the alternative, with its
  one-time **Open Anyway** step.

### Updater

- The shell already has check and apply (`web/src-tauri/src/updates.rs`), the
  web hook (`web/src/hooks/useDesktopShell.ts`), and the notice that prefers the
  Update button over the release notice (`web/src/components/UpdateNotice.tsx`).
  They are off only because no build sets `RCP_UPDATE_ENDPOINT` and
  `RCP_UPDATE_PUBKEY`.
- **Key.** The human runs `tauri signer generate` once, stores the private key
  and its password as the `TAURI_SIGNING_PRIVATE_KEY` and
  `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` Actions secrets, and keeps a backup. CI
  does not generate it, because a GitHub secret cannot be read back. The public
  key is checked into the repo and passed to the build as `RCP_UPDATE_PUBKEY`.
  The key does not expire.
- **Endpoint.** One fixed pre-release, tag `mac-latest`, holds only
  `latest.json`. Not `macos-latest`, which is a GitHub runner label. The app
  reads `https://github.com/Zhi0467/RCP/releases/download/mac-latest/latest.json`.
- **Publish order.** `publish-desktop.yml` builds with the endpoint and public
  key set, signs the updater bundle, finishes publishing `desktop-vX.Y.Z`, and
  only then replaces `latest.json` with `gh release upload --clobber`.
- **Key rotation.** `docs/release.md` gains the procedure: ship one release
  signed with the old key that carries the new public key, then switch the
  secret. Apps that skipped that release reinstall once with the `curl` line.
- Apps at v0.4.4 or older have the updater off, so they reinstall once with the
  `curl` line to get the button.

### Decision and docs

- This amends the
  [desktop installs decision](../decisions/2026-09-26-desktop-installs-follow-releases.md):
  the approval step is no longer an accepted cost, and one-click update is
  in scope for the prebuilt app. The same PR updates it.
- Current-behavior docs change with the code: the README, `docs/install.md`,
  `docs/desktop.md` (updater no longer disabled), `docs/release.md`, and the
  update-notice section of `docs/specs/api-web-and-desktop-projections.md`.

Not in scope: Linux desktop, Windows, Intel, Homebrew, Apple signing, and one
release per version.

## Checks

- The install script refuses a checksum mismatch, Intel, an unwritable
  `/Applications`, and a running app, and leaves the existing app untouched in
  each case. Test it against a local fixture release, not GitHub.
- `publish-desktop.yml` uploads `latest.json` only after `desktop-vX.Y.Z` is
  published, and the signature in it verifies against the checked-in public key.
- A build without the endpoint or key still reports the updater as disabled.
- Tests assert ids, states, and counts, never wording.
- The three real-hardware journeys in the closing condition, across two real
  releases.
