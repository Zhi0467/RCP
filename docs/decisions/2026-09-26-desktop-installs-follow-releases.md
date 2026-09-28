# Desktop installs follow releases

Date: 2026-09-26, revised 2026-09-28. Status: active.

## Decision

Desktop and local Web installs follow promoted releases (`vX.Y.Z` tags), the
same releases team servers install. Every release also publishes an unsigned
prebuilt macOS app, in a companion `desktop-vX.Y.Z` pre-release. That app is
the normal desktop install. A source checkout follows release tags too, and is
for developers. RCP does not pursue Apple signing.

The prebuilt app installs with one `curl … | sh` command and updates itself
from its Update button, through the Tauri updater and a free updater signing
key. Neither path asks for the Open Anyway approval. Team servers and source
installs are told an update is out, but RCP does not apply it for them.

## Why

- Team servers ran numbered releases while desktop apps were built from any
  commit on `main`. The desktop could be ahead of every team server, and a
  version mismatch showed up only as a failed team connection.
- Following `main` would make an update notice fire on every merge.
- Building from source asked every desktop user for Rust, the Xcode tools,
  Node, and uv. A prebuilt app removes all of that.
- Apple signing costs a yearly paid developer account. Without it, macOS blocks
  the first launch of a downloaded zip until the user approves it in System
  Settings. A file that `curl` downloads, and a file the Tauri updater
  downloads, carry no quarantine flag, so both avoid that step for free. On
  2026-09-28 the human decided the approval step was no longer an acceptable
  cost.
- The updater key is a minisign pair, not an Apple identity. A human generates
  it and keeps a backup, because a GitHub secret cannot be read back and a lost
  key strands every installed app.
- The app lives in a companion release because installed supervisors accept a
  server release only when it holds exactly their five files. Adding the app to
  `vX.Y.Z` would break every existing server's update.
- One-click update stays rejected for team servers and source installs. On a
  team server it needs a new channel from the unprivileged service to root,
  plus an operator role. For a source install it would run builds inside a
  checkout that may hold local work.

## Consequences

- The README leads with the install command; the manual zip, with its approval
  step, is the alternative. A source install, described in `docs/install.md`,
  checks out the latest release tag, not `main`.
- The updater reads `latest.json` from a fixed `mac-latest` pre-release, since
  the stable release cannot hold it. `docs/desktop.md` covers publishing it and
  replacing the key.
- A development build from `main` that is ahead of the latest release sees no
  update notice, and only the published prebuilt app has the updater enabled.
- The prebuilt app keeps the source build's Keychain storage, which any
  process running as the same user can read. The spec now accepts that for both
  kinds of build; app-bound access is later work, and easier once the app has
  a stable signing identity.
- If RCP ever pays for signing, the prebuilt app, the updater, and app-bound
  credentials change together.
