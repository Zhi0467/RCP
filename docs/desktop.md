# Desktop testing and release

RCP's desktop shell is a native macOS entrance to the same React interface and FastAPI
backend used by the browser path. This document covers the desktop-specific development,
verification, and release work that does not belong in the main README.

The current desktop target is Apple Silicon macOS. Read `web/src-tauri/tauri.conf.json`
for the authoritative platform settings and `web/package.json` for the available commands.

## Prerequisites

Start with the shared source setup in the [README](../README.md#shared-setup). Desktop work
also requires the Rust toolchain. Codex CLI or Claude Code must be installed and
authenticated separately to exercise agent features.

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

A successful build is not the desktop test. Open the relevant bundle through Finder,
exercise the affected workflow, inspect the visible result, and check the backend log for
errors. The desktop scenarios in [`docs/acceptance/`](acceptance/README.md) are the source
of truth for native window, Quit, artifact, packaged-environment, update, and text-scale
behavior.

## Build and test a release candidate

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

Smoke-test the backend inside that exact bundle:

```bash
uv run python packaging/smoke-backend.py \
  web/src-tauri/target/release/bundle/macos/RCP.app/Contents/MacOS/rcp-backend
```

Then open that bundle through Finder and run the desktop acceptance scenarios affected by
the candidate. Confirm that the project index opens, a project can be read, provider
readiness is truthful, desktop-only interactions work, and the app owns or reuses the
expected backend. Source behavior is not evidence for the packaged artifact.

When final desktop verification is complete and no more native work is planned, remove
disposable Rust build output:

```bash
cargo clean --manifest-path web/src-tauri/Cargo.toml
```

## Publish a release

The repository currently produces an unsigned, unnotarized local app. Publishing requires
all of the following outside the ordinary build:

1. An intentional version bump.
2. Apple Developer ID signing.
3. Apple notarization and stapling.
4. A downloadable archive or installer with checksums.
5. An updater signing key, HTTPS endpoint, and published manifest.

`npm --prefix web run desktop:build-signed` asks Tauri to create updater artifacts, but it
does not provide the signing identity, notarization credentials, updater configuration, or
publishing infrastructure. Do not describe a local bundle as a release until the signed,
notarized, distributed artifact has passed the packaged-app checks above.
