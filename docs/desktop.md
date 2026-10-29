# Desktop testing and release

This guide covers native macOS procedures. Use the
[source install guide](install.md#install-from-source) first. `web/src-tauri/tauri.conf.json` owns platform settings and
`web/package.json` owns build commands.

## Three desktop builds

| Name on disk | Build | Use |
| --- | --- | --- |
| `/Applications/RCP.app` | the `desktop-vX.Y.Z` release download | everyday use, on real data |
| `RCP Candidate.app` | the `desktop-candidate.yml` artifact, renamed after unzipping; or `desktop:build` output, left in `target/release` | packaging, frozen-only, and pre-promotion checks |
| `RCP Dev.app` | `npm --prefix web run desktop:build-dev` (`desktop:dev` runs unbundled) | branch work |

Reserve `RCP.app` outside `target/` for the released download. Rename downloaded
candidates without rebuilding. Quit all other copies before testing. The
candidate shares the release bundle identifier. All three use the same default
backend port and data directory. Give tests a throwaway `RCP_DATA_DIR`.
Verify frozen-only behavior in the packaged app. Source runs cannot exercise it.

## Personal sign-in

Using disposable data, verify fresh start, adoption, restart, reload, sign-out
and a reset Keychain entry. When adopting a terminal backend without a matching
secret, paste its one-time sign-in code. Check the WKWebView owner cookie in
source and frozen builds, then exercise PDF, transfer, notifications and terminals.
See [personal sign-in](specs/projects-spaces-and-operations.md#personal-sign-in).

## Prerequisites

Install Rust via [rustup](https://rustup.rs) and Xcode command-line tools.
If `cargo metadata` cannot find Cargo, load its environment:

```bash
. "$HOME/.cargo/env"
```

Install and authenticate the provider CLI separately before agent checks.

## Test the desktop app

For native or packaging changes, run:

```bash
cargo fmt --manifest-path web/src-tauri/Cargo.toml -- --check
cargo clippy --manifest-path web/src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path web/src-tauri/Cargo.toml --locked
```

Use the live shell, or same-origin mode for backend-served frontend behavior:

```bash
npm --prefix web run desktop:dev
npm --prefix web run desktop:same-origin
```

For application launch, lifecycle, dialogs or `Info.plist` checks, build and
launch the exact bundle using [the source desktop procedure](install.md#run-the-source-built-macos-desktop-app).
Rebuild after Rust or Tauri changes. Replace any separate installed copy before
testing.

### Only the menu Quit stops the backend

Use **Quit RCP** or Cmd+Q. If another quit path leaves the backend alive, identify
and stop that process explicitly before relaunching. Do not treat an adopted
old backend as evidence for new source. Open the actual app bundle, drive the
affected workflow and inspect its logs.

## Keep-awake checks

Use a Mac you can leave on a desk. Disposable data does not isolate machine-wide
power state. Follow [the power contract](specs/server-and-machine-operations.md#keeping-a-mac-awake).

```bash
pmset -g assertions | grep caffeinate
pmset -g log
```

With a task or episode running, lock the screen past its sleep timers. Verify
that work advances without sleep. After work ends, verify the assertion disappears.

### Lid-closed mode

Run these checks in the packaged candidate:

```bash
pmset -g | grep SleepDisabled
ls -l /etc/sudoers.d/rcp-keep-awake /Library/LaunchDaemons/org.rcp.keep-awake-reset.plist
```

Install and uninstall the mode. With its flag set and lid closed, SIGKILL the test
backend and verify the watchdog clears the flag within its stale window. Reboot
with the flag set and verify it clears before login. Verify an overnight
lid-closed Auto-research run advances. To clear the flag manually:

```bash
sudo pmset -a disablesleep 0
```

### Linux laptops

For operator-managed lid behavior, set `HandleLidSwitch=ignore` and
`HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf`, then restart
`systemd-logind`. To inhibit idle sleep during a run:

```bash
systemd-inhibit --what=idle:sleep uv run rcp serve
```

## Artifact viewer checks

After rebuilding, use disposable data to open an artifact, episode report and
repository file. Verify main-window docking, floating, resizing and fullscreen,
viewer comments, PDF system viewing and native save dialogs (including an artifact
without a producing task). Test middle-click links, Enter or Space on the title bar,
floating-panel restoration through the dock tab, and closure on project switch.
Inspect logs for unrecognized same-origin popups. Check the opaque inner frame
and same-origin outer framing against [viewer rules](specs/paper-artifacts-and-result-views.md).

## WebView origin probes

Drive WKWebView's HTTP and pinned HTTPS probes:

```bash
python3 web/src-tauri/scripts/run-loopback-origin-probe.py --mode aliases --phase login
python3 web/src-tauri/scripts/run-local-https-origin-probe.py --phase login --cert-dir /tmp/rcp-probe-certs
```

Repeat HTTPS with `--phase resume` and the same certificate directory to check
restart persistence. Never print or export sealing keys or plaintext identities.
See [desktop trust rules](specs/api-web-and-desktop-projections.md#confirmed-team-desktop-target).
For the opt-in tunnel check, supply an existing system SSH login:

```bash
RCP_LIVE_SSH_TARGET=<ssh-alias-or-user@host> \
  cargo test --manifest-path web/src-tauri/Cargo.toml \
  team_tunnel::tests::live_system_ssh_child_forwards_through_owned_tls_proxy \
  -- --ignored --nocapture
```

## Build and test a release candidate

Download the exact candidate via [pre-promotion checks](release.md#what-to-check-before-promoting)
and rename it per [the build table](#three-desktop-builds). Quit other RCP copies
and launch with disposable data:

```bash
if curl -fsS -m 3 http://127.0.0.1:8421/api/health >/dev/null; then echo "another RCP is running; quit it first"; else open -n "RCP Candidate.app" --env RCP_DATA_DIR="$(mktemp -d)"; fi
```

`RCP_PORT` moves a build's backend and window to another loopback port; unset,
it is 8421. Pair it with a throwaway `RCP_DATA_DIR`. It does not separate the
bundle identifier, so a build with the released identifier still focuses a
running RCP and shares its settings.

For a local candidate, verify the intended revision and version. Check that the
scope is clean and the baseline and native checks pass. Then build:

```bash
npm --prefix web run desktop:build
```

Use `web/src-tauri/target/release/bundle/macos/RCP.app`. Its backend is one
directory in `Contents/Resources/backend`. A one-file backend unpacked into a new
temporary folder at every launch, and macOS scanned each copy, so the app and
every terminal took 10 s or more to start. In `MacOS`, PyInstaller's bootloader
would look for its libraries in `Contents/Frameworks`. Smoke-test its exact backend:

```bash
uv run python packaging/smoke-backend.py \
  web/src-tauri/target/release/bundle/macos/RCP.app/Contents/Resources/backend/rcp-backend
```

Repeat with `--terminal-only` to check frozen terminal commands, job control,
Stop and PTY cleanup. Launch the bundle with disposable data and verify project
index and project reads. Verify truthful provider readiness, affected native
interactions and backend ownership. CI's fake-provider smoke journey does not replace macOS qualification.
After final verification, remove disposable Rust output if no more native work is planned:

```bash
cargo clean --manifest-path web/src-tauri/Cargo.toml
```

## Publish the desktop companion release

Follow [promotion](release.md#promote-a-build-to-stable), supplying the tested
candidate run. Select a successful, non-rerun candidate of the same build before
its artifact expires; the retention window belongs to
[build-desktop.yml](../.github/workflows/build-desktop.yml).
Inspect [publish-desktop.yml](../.github/workflows/publish-desktop.yml) results:
check the release and build commit, native versions, frozen smoke test, bundle checksum
and updater signature before publication. Verify the downloaded-back companion
assets and the `mac-latest` updater pointer. Keep desktop assets in their
`desktop-vX.Y.Z` companion, separate from the five-asset server release.
For a failed updater-pointer job, use **Re-run failed jobs** to retry it alone.
Its workflow owns monotonic selection and restoring the previous pointer on failure.
Do not replace an already-published companion.

### Updater signing key

Keep the private key and password outside the repository. Configure them as
`TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` Actions
secrets. Keep the public key in `web/src-tauri/updater.pub`.
To generate a pair:

```bash
npm --prefix web exec -- tauri signer generate -w <path>
```

To rotate it, bridge the trust of clients that only know their shipped public key:

1. Move the old public key to `web/src-tauri/updater-signing.pub`; place the new
   public key in `updater.pub` and release, still signing with the old private key.
2. Replace both Actions secrets and delete `updater-signing.pub` in the next release.
3. Have clients that skipped the bridging release reinstall using
   [`scripts/install-macos.sh`](../scripts/install-macos.sh).

For an updater-enabled local build, set `RCP_UPDATE_ENDPOINT` and
`RCP_UPDATE_PUBKEY`, then run `npm --prefix web run desktop:build-signed`.
Use [build-release.sh](../web/src-tauri/scripts/build-release.sh) for the exact
signer and runtime settings. Ordinary source and local builds do not enable
the updater. Preserve [release signing settings](../web/src-tauri/tauri.release.conf.json):
release bundles are ad-hoc signed without hardened runtime so the frozen Python
backend can load.
