# Desktop testing and release

Native macOS testing and release procedures. The target is Apple Silicon;
`web/src-tauri/tauri.conf.json` owns platform settings and `web/package.json`
lists build commands.

## Personal sign-in

The desktop authenticates before protected requests using its data-directory
Keychain secret and captured one-time startup code. Downloads, PDF, transfer,
notifications and update checks share that client. See
[personal sign-in](specs/projects-spaces-and-operations.md#personal-sign-in).

When adopting a terminal backend without a matching secret, paste its one-time
code. Failure leaves the adopted process running. Restarting an owned backend
can recover a missing Keychain entry from launch JSON's `owner_sign_in_code`;
codes never enter diagnostics.

Verify fresh start, adoption, restart, reload, sign-out, and a reset Keychain entry
with disposable data. Check the plain-HTTP WKWebView owner cookie in both source
and frozen builds, then exercise PDF, project transfer, notifications, and terminals.

## Prerequisites

Start with the source installation in the [install guide](install.md#install-from-source). Desktop work
also requires the Rust toolchain from [rustup](https://rustup.rs). rustup installs `cargo`
under `~/.cargo/bin` and offers to add that directory to your shell's PATH. If you declined,
or a `desktop:*` command fails with `failed to run 'cargo metadata' command ... No such file
or directory`, load it in that shell first:

```bash
. "$HOME/.cargo/env"
```

Codex CLI or Claude Code must be installed and authenticated separately to exercise agent
features.

## Test the desktop app

Run the Rust checks when native code or desktop packaging changes:

```bash
cargo fmt --manifest-path web/src-tauri/Cargo.toml -- --check
cargo clippy --manifest-path web/src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path web/src-tauri/Cargo.toml --locked
```

Use the live shell for the normal native development loop:

```bash
npm --prefix web run desktop:dev
```

This starts Vite, compiles the Rust shell in debug mode, and starts or reuses the Python
backend from the checkout.

Use the same-origin mode when the behavior depends on the frontend being served by the
backend rather than by Vite:

```bash
npm --prefix web run desktop:same-origin
```

Build a Finder-launchable development app when the behavior depends on application launch,
window lifecycle, native dialogs, or `Info.plist` configuration:

```bash
npm --prefix web run desktop:build-dev
```

The bundle is written to:

```text
web/src-tauri/target/debug/bundle/macos/RCP Dev.app
```

`RCP Dev.app` records the checkout and absolute `uv` executable in its `Info.plist` and
launches the backend from source. Rebuild it after Rust or Tauri configuration changes.
An older `/Applications/RCP Dev.app` is a separate copy and is not changed by that build.
Quit it with Cmd+Q before replacing and reopening that copy:

```bash
ditto "web/src-tauri/target/debug/bundle/macos/RCP Dev.app" "/Applications/RCP Dev.app"
open "/Applications/RCP Dev.app"
```

### Only the menu Quit stops the backend

Use **Quit RCP** or Cmd+Q to shut down the owned backend through the native menu.
Other macOS quit paths can exit without Tauri's `ExitRequested`, leaving the
backend serving on 8421. A later launch then adopts it and source changes do not
load. If a backend leaked, stop it explicitly (this targets all matching servers):

```bash
pkill -f "rcp serve"
```

Open the actual bundle, exercise the affected workflow and inspect backend logs.
Browser checks do not prove native windows, Quit, packaged environment, updater
or text scaling. The `ExitRequested` handler's `code: None` guard prevents its own
`app.exit(code)` from recursively starting shutdown.

## Keep-awake checks

The idle hold is a machine-wide power assertion, so a disposable
`RCP_DATA_DIR` does not isolate it. Behavior is in
[the server spec](specs/server-and-machine-operations.md#keeping-a-mac-awake).
With work running, the assertion is listed:

```bash
pmset -g assertions | grep caffeinate
```

1. **Lock screen.** Start an episode or task, lock the screen, and wait past
   the display and system sleep timers. Unlock: `pmset -g log` shows no sleep
   in between, and the work kept advancing.
2. **Work ends.** When nothing is running, the assertion is gone.

### Lid-closed mode

Keep-awake changes machine-wide power state, so a disposable `RCP_DATA_DIR`
does not isolate it. Run these on the packaged candidate, on a Mac you can
leave on a desk. Inspect the flag at any point:

```bash
pmset -g | grep SleepDisabled
```

```bash
ls -l /etc/sudoers.d/rcp-keep-awake /Library/LaunchDaemons/org.rcp.keep-awake-reset.plist
```

1. Install, then uninstall.
2. With the flag set and the lid closed, SIGKILL the backend: the flag
   clears and the Mac sleeps within the watchdog's stale window.
3. Reboot with the flag set: the flag is clear before login.
4. One overnight lid-closed Auto-research run advances.

To clear the flag by hand:

```bash
sudo pmset -a disablesleep 0
```

### Linux laptops

RCP does not change Linux power settings. To keep a Linux laptop running with
the lid closed, set `HandleLidSwitch=ignore` and
`HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf`, then
restart `systemd-logind`. To stop idle sleep while RCP runs, start it under
`systemd-inhibit --what=idle:sleep`.

## Artifact viewer checks

Artifacts, episode reports, and repository-file previews open in the panel inside
RCP's main window. There is no native preview window or preview-opening command.
PDFs still open in the system viewer, and downloads still use the native save dialog.

After rebuilding the desktop, open an artifact, an episode report, and a
repository file using disposable data. Verify that each stays in the main window,
that the panel can dock, float, resize, and go full screen, and that a comment
sends from the viewer. Verify a PDF opens in the system viewer and a download
opens the save dialog, including a stored artifact with no producing task. Verify
middle-clicked artifact and report viewer links return to the main panel, while
unrecognised same-origin popups are logged. Use Enter or Space on the focused
title bar to toggle full screen, restore a floating panel through the dock tab,
and check that it restores docked. Switching projects closes the panel.
The agent content remains inside the opaque inner frame;
the surrounding viewer and repository preview allow only same-origin framing.

## WebView origin probes

These probes drive WKWebView, not a substitute browser. The HTTP probe checks
that Secure cookies are lost on plain loopback, including exact localhost:

```bash
python3 web/src-tauri/scripts/run-loopback-origin-probe.py --mode aliases --phase login
```

The HTTPS probe uses a self-generated certificate pinned in the production native
trust hook. Its Cargo feature enables only the GUI example. Reuse `--cert-dir`
so restart tests cookie persistence rather than certificate replacement:

```bash
python3 web/src-tauri/scripts/run-local-https-origin-probe.py --phase login --cert-dir /tmp/rcp-probe-certs
```

Run `--phase resume` afterwards against the same directory to check that the
session survives an application restart. Each run first asserts that the host
does not already trust the probe certificate, so a success cannot be confused
with pre-existing system trust.

Production seals the local HTTPS identity under app config with a Keychain key.
Released and dev bundles use separate services/files; legacy dev identity adoption
requires authenticating the sealed file first. Never print/export keys or plaintext.
The `/usr/bin/security` helper avoids rebuild ACL churn but does not protect against
another deliberate same-user process; this is the cooperative account boundary.
Team tokens use the same bounded helper with one account per saved connection.

Only exact saved `rcp-<uuid>.rcp.localhost` HTTPS origins and their pinned certificate
enter the main window. Identity version 4 rotates its 365-day certificate in the
last seven days; valid older identities/origins migrate, malformed state refuses.
Certificates must pass hostname, validity and server-use checks. ATS exceptions
cover only `rcp.localhost` and direct connection subdomains, never authorize HTTP.
The [projection spec](specs/api-web-and-desktop-projections.md#confirmed-team-desktop-target)
owns connection, credential and trust contracts.

The ignored D3 live test uses the same production tunnel owner with an existing
system SSH login, forwards only the remote host's loopback SSH banner, then
stops and reaps the local child. It does not start or stop a remote service:

```bash
RCP_LIVE_SSH_TARGET=<ssh-alias-or-user@host> \
  cargo test --manifest-path web/src-tauri/Cargo.toml \
  team_tunnel::tests::live_system_ssh_child_forwards_through_owned_tls_proxy \
  -- --ignored --nocapture
```

## Build and test a release candidate

To test the exact bytes a promotion would publish, run the `desktop-candidate.yml`
workflow on the build, as described in [docs/release.md](release.md#what-to-check-before-promoting).
Rename the unzipped `RCP.app` to `RCP Candidate.app`, following the
[naming convention](../AGENTS.md#three-desktop-builds).

Every desktop build binds backend port 8421 and defaults to the same data
directory, and a candidate has the released app's bundle identifier. Opening a
second copy with that identifier only focuses the running one, and a new app
attaches to any backend already on the port. So a candidate is tested only
when no other RCP is running, and on its own data:

```bash
if curl -fsS -m 3 http://127.0.0.1:8421/api/health >/dev/null; then echo "another RCP is running; quit it first"; else open -n "RCP Candidate.app" --env RCP_DATA_DIR="$(mktemp -d)"; fi
```

The local steps below build the same app from a checkout; launch that bundle
the same way.

Before packaging, verify that the intended revision is checked out, the version is
intentional, no unrelated changes will enter the artifact, and the baseline and desktop
checks pass. Building requires Python and `uv`, Node.js and npm, and Rust.

Build the application:

```bash
npm --prefix web run desktop:build
```

The build compiles Web, freezes Python with PyInstaller as one directory, and
copies that directory into the app's `Contents/Resources/backend`:

```text
web/src-tauri/target/release/bundle/macos/RCP.app
```

Packaging includes first-party package source/data and Web assets with a verified
SHA-256 inventory. Keep runtime resources inside the package; no per-file list is needed.

The backend is one directory, not one file. A one-file backend unpacks its
libraries into a new temporary folder at every launch, and macOS scans each
copy again, so the app and every terminal took 10 s or more to start. It lives
in `Resources`, not `MacOS`: in `MacOS`, PyInstaller's bootloader looks for its
libraries in `Contents/Frameworks` instead of beside itself.

Smoke-test the backend inside that exact bundle:

```bash
uv run python packaging/smoke-backend.py \
  web/src-tauri/target/release/bundle/macos/RCP.app/Contents/Resources/backend/rcp-backend
```

The smoke test drives a disposable SSH-project Discuss turn over the served API
using fake SSH/provider executables, verifying packaged staging and a saved answer
without real credentials/model calls. Candidate CI runs the bundled backend; Linux
PR freezing checks the same journey but does not replace macOS qualification.

Then launch that bundle as above, with a throwaway `RCP_DATA_DIR`, and exercise the desktop workflows affected by
the candidate. Confirm that the project index opens, a project can be read, provider
readiness is truthful, desktop-only interactions work, and the app owns or reuses the
expected backend. Source behavior is not evidence for the packaged artifact.

When final desktop verification is complete and no more native work is planned, remove
disposable Rust build output:

```bash
cargo clean --manifest-path web/src-tauri/Cargo.toml
```

## Publish the desktop companion release

Each promotion ends by calling `.github/workflows/publish-desktop.yml`, which
can also be run by hand for the same tag. Given a desktop candidate run, it
publishes that run's tested app after checking it was built from the release's
build; the candidate's app expires 14 days after it was built, and a candidate
run more than once is refused. Otherwise it builds the exact commit of the
published `vX.Y.Z` release on an Apple Silicon runner, checks that the native
versions equal the tag (`packaging/release_build.py check-desktop-version`),
and smoke-tests the bundled backend. Every build, candidate or fresh, is the
updater-enabled app: it verifies the updater bundle's signature against the
signing key with `minisign` before anything is uploaded. Either way it uploads
`RCP-vX.Y.Z-macos-arm64.zip`, its `.sha256`, and the signed updater bundle
`RCP-vX.Y.Z-macos-arm64.app.tar.gz` with its `.sig` to a draft
`desktop-vX.Y.Z` release. It downloads them back,
verifies them, and only then publishes the draft as a pre-release that is not
latest. The app cannot live in `vX.Y.Z` itself: installed supervisors accept a
server release only with exactly its five files. A failed run leaves the server
release unchanged and can be rerun; a published companion is never replaced.

A final job then points the updater at the new release. It writes
`latest.json` (`packaging/release_build.py updater-manifest`) and uploads it to the fixed `mac-latest` pre-release, creating that release the
first time. It refuses to replace a `latest.json` that names a newer version,
and runs one at a time across tags. A failed upload puts the previous
`latest.json` back. If only this job fails, rerun it alone with
**Re-run failed jobs**; it reads everything from the published companion.

`scripts/install-macos.sh` is the README's one-command install. It follows the
`releases/latest` redirect to the version, downloads that companion's zip and
`.sha256`, checks the checksum and the unpacked app's signature, and swaps
`/Applications/RCP.app` with a backup and rollback. It refuses Intel Macs,
macOS before 13, a running RCP, and an `/Applications` the account cannot
write, and it never uses `sudo`. `curl` sets no quarantine flag, so the app
opens without Open Anyway.

### Updater signing key

The updater key is a free minisign key pair, not Apple signing, and it does not
expire. The private key and its password are the `TAURI_SIGNING_PRIVATE_KEY` and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` Actions secrets, which the desktop
candidate and promotion both forward to `build-desktop.yml`; the public key is `web/src-tauri/updater.pub`. A human made the
pair with `npm --prefix web exec -- tauri signer generate -w <path>` and keeps a
backup of the private key; a GitHub secret cannot be read back.

Installed apps trust only the public key they shipped with. To replace the key:

1. Generate a new pair.
2. Move the old public key to `web/src-tauri/updater-signing.pub`, put the new
   one in `updater.pub`, and release that, still signed with the old private
   key. The publish check verifies against `updater-signing.pub` when it exists.
3. Replace both secrets with the new private key and password, and delete
   `updater-signing.pub` in the next release.

Apps that skipped the release in step 2 reinstall once with the install command.

The native versions in `web/package.json`, `web/package-lock.json`, and
`web/src-tauri/Cargo.toml`/`Cargo.lock` must equal `src/rcp/__init__.py`; a test
enforces it, so a version bump changes all of them.

Release bundles are ad-hoc signed without hardened runtime: sealing prevents
macOS's damaged-app error, while hardened runtime blocks unpacked Python loading.
Credentials use the same cooperative Keychain boundary as source builds.

Only the published prebuilt app enables the Tauri updater:
`npm --prefix web run desktop:build-signed` requires `RCP_UPDATE_ENDPOINT` and
`RCP_UPDATE_PUBKEY`, compiles both into the app, and gives the same key to the
Tauri signer. Every other build sets neither, reports `enabled: false`, and
takes update notices from the release check. Apps from v0.4.4 or earlier have
the updater off and reinstall once with the install command.
