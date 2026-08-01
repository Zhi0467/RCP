---
id: S34-packaged-app-needs-no-toolchain
status: implemented
tier: packaged
driver: desktop
covered_by:
  - packaging/smoke-backend.py
  - tests/test_web_assets.py
  - tests/test_server_runtime.py
  - web/src-tauri/src/updates.rs::tests::disabled_updater_config_is_an_object
last_passed: 2026-07-31 — both app bundles rebuilt and opened through macOS
  LaunchServices directly into the project index, confirmed from their native
  accessibility trees and screenshots; the packaged sidecar owned a fresh server under
  a scrubbed environment and served its bundled React UI. Still unexercised: an account
  with no toolchain at all, the remote shared-parser path, and the no-provider state
requires: a built application bundle, and a machine with no developer toolchain
invariants: [8]
---

# Two artifacts: a dev shell that loads the checkout, and a release app that needs nothing

There are two applications, following the pattern Margin already uses.

**RCP Dev.app** is a thin shell with its own bundle identifier, so it installs
beside the release app. It records the checkout root and the `uv` executable in
its `Info.plist` and launches the backend from source, exactly as Margin's dev
bundle records `MarginDevelopmentRoot` and a Node path. Restarting it picks up
edits. It embeds no Python and never runs the packaging pipeline, so debugging
the shell costs a relaunch rather than a build.

**RCP.app** is self-contained. Someone who has never installed Python, Node,
npm, `uv`, or Rust opens it and it works. Everything below is a promise about
this artifact.

"Nothing installed" cannot include the providers. RCP is a shell around Codex and
Claude Code, both installed the way developer tools are installed, so the honest
promise is narrower than the title: **RCP adds no toolchain requirement of its
own.** A machine with genuinely nothing gets an application that boots, shows the
project index, and opens a project — and says clearly that it has no provider to
run, which is a different state from a provider it cannot find
([S35](S35-packaged-environment-parity.md)).

Three things in the source tree prevent the release app from booting at all.
The backend reads `record_parsing.py` as a **source file on disk** at import
time, because that text ships over SSH to hosts with no `rcp` package; a frozen
bundle ships bytecode, so importing the indexer raises before the server exists.
Starting a server **always invokes npm** to build the frontend first. And the
served assets and the build directory are located **independently**, each by
counting parents from a source file.

The application version is also three different strings today — the package
metadata, the FastAPI constructor, and `rcp.__version__` — which matters because
[S29](S29-refuse-instead-of-taking.md) refuses on version mismatch, and a rule
cannot compare a value that disagrees with itself.

**The dev app defers packaging bugs rather than removing them.** Every promise
below is exactly what it skips. So the release checks run as an automated smoke
on the built artifact, the way Margin runs `verify:package`, rather than by hand
on release day.

## UI path

Confirmed with the human on 2026-07-31. No new UI; the changes are structural:

- `record_parsing.py` ships as package data and is read through the packaging
  API rather than by walking to a neighbouring path.
- A prebuilt-assets mode starts the server without ever invoking npm; the
  developer path that builds first stays exactly as it is.
- One resolver answers "where are the web assets" for both the build step and
  the mount, honoring development and packaged layouts.
- One authoritative version, single-sourced from the package and reported
  through `/api/health`.
- The sidecar binary carries the target-triple suffix Tauri resolves by.
- Apple Silicon only for the first release.

Deliberately not possible: a release app that shells out to a build tool, a
version comparison against a string hardcoded in more than one place, and a dev
app that pretends to exercise the packaged path.

## Drive

1. Build both bundles.
2. Open `RCP Dev.app`, edit a source file, and relaunch.
3. On a machine or account with no Python, Node, npm, `uv`, or Rust on `PATH`,
   open `RCP.app`.
4. Open the project index, open a project, and read a graph.
5. Trigger the code path that ships the shared parser to a remote host.
6. Read `/api/health`, and run the packaging smoke check against the artifact.

## Assert

- `the_dev_app_loads_the_checkout_and_needs_no_packaging`
- `the_two_bundles_install_side_by_side`
- `the_release_app_boots_with_no_developer_toolchain_present`
- `no_npm_process_is_started`
- `the_frozen_backend_imports_the_indexer`
- `the_shared_parser_source_is_readable_from_the_bundle`
- `packaged_web_assets_resolve_through_the_same_resolver_as_development`
- `the_sidecar_resolves_by_target_triple`
- `health_reports_one_authoritative_version`
- `no_provider_installed_is_reported_as_its_own_state`
- `the_packaging_smoke_check_runs_without_a_human`
- `the_developer_entrance_still_builds_and_serves_from_source`

## Failure means

The application launches to a window that never loads, and the reason is an
import error in a process the human cannot see. Every other desktop promise is
untestable until this one holds — and if only the dev app is ever exercised, the
packaging failures arrive all at once on the day of a release.
