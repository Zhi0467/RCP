# Desktop testing and release

RCP's desktop shell is a native macOS entrance to the same React interface and FastAPI
backend used by the browser path. This document covers the desktop-specific development,
verification, and release work that does not belong in the main README.

The current desktop target is Apple Silicon macOS. Read `web/src-tauri/tauri.conf.json`
for the authoritative platform settings and `web/package.json` for the available commands.

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

`Quit RCP` in the application menu, and its Cmd+Q accelerator, are one custom
`MenuItem` routed through `on_menu_event` to `request_app_quit`. That path runs the
shutdown and stops the backend the app owns.

Every other quit gesture leaves that backend running: the Dock icon's Quit,
`osascript -e 'quit app "RCP"'`, and logout or restart. macOS terminates the process
without Tauri emitting `RunEvent::ExitRequested`, so the handler in `src/lib.rs` never
runs. Measured on 2026-08-29 by logging every `ExitRequested` and observing none, while
the shell exited and its backend kept serving.

This matters because a leaked backend still holds 8421, so the next launch adopts it
through `--reuse-existing` and source edits do not take effect. Stop it explicitly:

```bash
pkill -f "rcp serve"
```

A fix belongs in the macOS `applicationShouldTerminate:` delegate, not in a wider match
arm. The handler's `code: None` guard is deliberate: the shutdown's own `app.exit(code)`
re-enters that event with `Some(code)`, so matching every code would re-enter quit.

A successful build is not the desktop test. Open the relevant bundle through Finder,
exercise the affected workflow, inspect the visible result, and check the backend log for
errors. Native window, Quit, artifact, packaged-environment, update, and text-scale
behavior are verified through the desktop itself; a browser check does not stand in
for any of them.

## Keep-awake checks

Keep-awake changes machine-wide power state, so a disposable `RCP_DATA_DIR`
does not isolate it. Run these on the packaged candidate, on a Mac you can
leave on a desk. Behavior is in
[the server spec](specs/server-and-machine-operations.md#keeping-a-mac-awake).

Inspect the state at any point:

```bash
pmset -g | grep SleepDisabled
```

```bash
pmset -g assertions | grep caffeinate
```

```bash
ls -l /etc/sudoers.d/rcp-keep-awake /Library/LaunchDaemons/org.rcp.keep-awake-reset.plist
```

1. **Install.** In Space Settings, tick **Lid-closed mode**, read the dialog,
   and press **Install**. Cancel the admin prompt once: nothing changes. Then
   approve it: the three paths exist, and the flag reads 0.
2. **Overnight.** Start an Auto-research episode that waits on a compute job.
   Close the lid overnight. In the morning, the episode has advanced through
   the watcher wake, the continuation, and a Patch apply.
3. **Faults.** With lid mode active, `kill -9` the backend worker, then repeat
   with `kill -STOP`. Within about a minute the flag reads 0 and a closed Mac
   sleeps. Kill the watchdog: the next pass replaces it or releases.
4. **Reboot.** With the flag set by RCP, restart. After login the flag reads 0.
5. **Battery.** Unplug and let it reach 20%: lid mode releases and a closed
   Mac sleeps. Plug in: it re-arms.
6. **Uninstall.** Press **Uninstall**: the flag reads 0, and the sudoers file
   and LaunchDaemon are gone.

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

Two example probes drive a real WKWebView to answer questions browser tests
cannot. The original HTTP probe remains evidence only. The HTTPS probe exercises
the same native trust primitive now linked into every macOS desktop build; its
test servers and automatic navigation remain example-only.

`run-loopback-origin-probe.py` is the original HTTP drive. It records that a
`Secure` session cookie is lost on plain loopback origins, including exact
`localhost`:

```bash
python3 web/src-tauri/scripts/run-loopback-origin-probe.py --mode aliases --phase login
```

`run-local-https-origin-probe.py` repeats that drive over local HTTPS with a
certificate the probe generates for itself, pinned in the WebView's own
server-trust challenge. Its `https-trust-probe` Cargo feature enables only the
standalone GUI example. `--cert-dir` reuses one certificate across runs so the
restart phase measures cookie persistence rather than a changed certificate:

```bash
python3 web/src-tauri/scripts/run-local-https-origin-probe.py --phase login --cert-dir /tmp/rcp-probe-certs
```

Run `--phase resume` afterwards against the same directory to check that the
session survives an application restart. Each run first asserts that the host
does not already trust the probe certificate, so a success cannot be confused
with pre-existing system trust.

Production startup loads or creates one versioned local-HTTPS identity in
`local-https-identity-v1.sealed` under the app configuration directory. The file
is authenticated encryption with mode `0600`; its 32-byte key is the Keychain
item at service `<bundle identifier>.local-https`, account
`desktop-identity-sealing-key/source-v1`. The released app's service is
`app.researchcontrolpanel.rcp.local-https`; `RCP Dev.app` keeps its own under
`app.researchcontrolpanel.rcp.dev.local-https`, because its configuration
directory, and so its sealed file, is separate. A dev bundle whose file was
sealed under the shared service before this split adopts that key once it
authenticates the file, and leaves the shared entry for the released app. Never print or export the Keychain
value or decrypted identity. The source-built app accesses that short key only
through `/usr/bin/security`, whose ACL remains stable across ad-hoc rebuilds.
That ACL authorizes the Apple tool, not the calling RCP process: it prevents
rebuild churn and keeps secrets out of argv/page/log state, but it does not
protect them from another process deliberately invoking the tool as the same
macOS user. The current cooperative provider model accepts that limitation for
source builds and the unsigned prebuilt app alike; app-bound credential access
is later work.
Permanent team member tokens use the same bounded helper under service
`app.researchcontrolpanel.rcp.team-member-token.source-v1`, with one
`team-connection/<uuid>` account per saved connection.
Saved team connections use connection-bound `rcp-<uuid>.rcp.localhost` origins;
only their exact validated origins may enter the main window, and only the
stored certificate fingerprint is passed to the native trust hook.
The encrypted version-4 identity records a 365-day certificate lifetime and
rotates atomically during the final seven days. Its leaf explicitly carries
`CA:FALSE`, TLS server-auth usage, and the key usages required by WKWebView.
Startup reissues valid earlier identity records and migrates the exact previously
shipped `rcp-<uuid>.localhost` registry origin; malformed or partial identity
state still fails closed. Both macOS bundle plists grant the manual-trust ATS
exception only to `rcp.localhost` and its direct connection subdomains. That
exception does not authorize HTTP: the native navigation owner still admits
only exact saved HTTPS origins and the trust handler still requires the pinned
certificate to pass hostname, validity, and server-use evaluation.

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

This command compiles the frontend, freezes the Python backend with PyInstaller, prepares
the target-specific sidecar, and assembles:

```text
web/src-tauri/target/release/bundle/macos/RCP.app
```

The frozen backend includes the entire first-party Python package as source and
package data, including SSH helpers, nested skills, and service templates.
Packaging generates a SHA-256 inventory from those files and the frontend;
startup verifies the extracted files against it. Adding a runtime resource inside
the package does not require another per-file packaging or validation list.

Smoke-test the backend inside that exact bundle:

```bash
uv run python packaging/smoke-backend.py \
  web/src-tauri/target/release/bundle/macos/RCP.app/Contents/MacOS/rcp-backend
```

The smoke test also creates a disposable remote project and runs a Discuss turn
through the served API. It uses isolated SSH and provider test executables to
exercise the packaged staging scripts and verify a persisted agent answer without
credentials or a real model request. Candidate CI runs it against the bundled backend.
PR CI also freezes the backend on Linux and runs this same journey; the macOS
candidate remains the check for the actual desktop artifact.

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

Apple signing is not planned. It needs a paid Apple Developer account, and a
locally built app is never gatekept, so it would only spare prebuilt-app users
the one-time Open Anyway approval. The release build is ad-hoc signed, without
the hardened runtime: an unsealed bundle makes macOS call the downloaded app
damaged instead of offering Open Anyway, and the hardened runtime stops the
backend from loading its unpacked Python library. Without a signing identity the Keychain
cannot bind credentials to the app, so the prebuilt app keeps the source build's
Keychain storage. Only the published prebuilt app enables the Tauri updater:
`npm --prefix web run desktop:build-signed` requires `RCP_UPDATE_ENDPOINT` and
`RCP_UPDATE_PUBKEY`, compiles both into the app, and gives the same key to the
Tauri signer. Every other build sets neither, reports `enabled: false`, and
takes update notices from the release check. Apps from v0.4.4 or earlier have
the updater off and reinstall once with the install command.
