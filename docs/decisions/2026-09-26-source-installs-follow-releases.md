# Source installs follow releases

Date: 2026-09-26. Status: active.

## Decision

Local source installs, meaning the desktop app and the local Web app, follow
promoted releases (`vX.Y.Z` tags). These are the same releases team servers
install. RCP does not pursue Apple signing. RCP tells people when an update is
out, but does not apply it. People update with one command they run
themselves.

## Why

- Team servers ran numbered releases while desktop apps were built from any
  commit on `main`. The desktop could be ahead of every team server, and a
  version mismatch showed up only as a failed team connection.
- Following `main` would make an update notice fire on every merge.
- Apple signing costs a yearly paid developer account. It only helps prebuilt
  app downloads. RCP users build the app on their own Mac, and macOS does not
  gatekeep an app built locally.
- One-click update was rejected for now. On a team server it needs a new
  channel from the unprivileged service to root, plus an operator role. For a
  source install it would run builds inside a checkout that may hold local
  work.

## Consequences

- The README installs from the latest release tag, not from `main`.
- A development build from `main` that is ahead of the latest release sees no
  update notice.
- If RCP ever ships prebuilt app downloads, revisit Apple signing and the Tauri
  updater together.
