# Install and run RCP from source

Build order, local runs, the desktop app, and the checks that verify a
checkout. The [README](../README.md) is the short version.

## Install from source

Requirements:

- Git;
- Node.js and npm;
- [`uv`](https://docs.astral.sh/uv/); and
- Codex CLI or Claude Code, installed and authenticated separately if you want
  to run agent tasks.

Open the [latest release](https://github.com/Zhi0467/RCP/releases/latest) and
copy its `vX.Y.Z` tag. Clone, select that release, and build in this order
(replace `<tag>` with the copied tag):

```bash
git clone https://github.com/Zhi0467/RCP.git
cd RCP
git checkout --detach refs/tags/<tag>
npm --prefix web ci
npm --prefix web exec playwright -- install chromium
npm --prefix web run build
uv sync
```

Playwright installs test Chromium. Build `web/dist` before `uv sync` or `uv run`:
the editable Python package includes it. Each new checkout/worktree needs its
own untracked Web dependencies and bundle:

```bash
npm --prefix web ci && npm --prefix web run build
```

The `web/dist present` pre-commit hook checks this prerequisite.

## Update a source checkout

Quit the desktop app with Cmd+Q and stop every backend using this checkout,
including backends with other data directories. Commit or move any local changes
and untracked files before updating. Copy the `vX.Y.Z` tag from the latest
release, then run from the checkout (replace `<tag>`):

```bash
scripts/update-from-source <tag>
```

For a source-built desktop app, include the desktop build:

```bash
scripts/update-from-source <tag> --desktop
```

The script refuses active backends or missing tools, records the start revision,
fetches/checks out the tag detached and rebuilds Web/Python. `--desktop` also needs
Rust/Xcode and builds the dev app. Failure restores the recorded checkout/build;
if recovery fails it prints remaining commands. No branch reset or `/Applications`
replacement occurs.

For the Web app, restart with `uv run rcp serve`. For the desktop app, replace
`/Applications/RCP Dev.app` with `web/src-tauri/target/debug/bundle/macos/RCP Dev.app` and
reopen it, as described below.

## Run the local Web app

For development with backend reload and automatic Web rebuild:

```bash
uv run rcp serve --reload
```

Open the sign-in URL printed to the terminal. Its code works once and expires
after ten minutes. A signed-in browser keeps an owner session. React and CSS
changes rebuild the bundle; refresh the page to see them.

For a normal non-reloading launch that opens the browser:

```bash
uv run rcp open
```

Open a checkout at launch:

```bash
uv run rcp open /absolute/path/to/project
```

If a backend is already running, the browser asks you to confirm the project
locator after sign-in. The CLI does not register it through the API. A fresh
backend still registers a project supplied at startup.

Serve without opening a browser:

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

On macOS, local app data defaults to:

```text
~/Library/Application Support/research-control-panel/
```

Set `RCP_DATA_DIR` before launch to use another data directory. Canonical
research history remains in each project's configured state repository.

## Run the source-built macOS desktop app

Source desktop builds also require Rust and Xcode command-line tools; Command Line Tools for Xcode 26 enable SpeechAnalyzer without full Xcode.

Run directly from the checkout:

```bash
npm --prefix web run desktop:dev
```

Build a Finder-launchable development app:

```bash
npm --prefix web run desktop:build-dev
```

The app is written to:

```text
web/src-tauri/target/debug/bundle/macos/RCP Dev.app
```

Launch that exact bundle. If you keep a Dock/Finder copy at
`/Applications/RCP Dev.app`, rebuilding does not update it: quit RCP with Cmd+Q,
then replace the installed copy before reopening it:

```bash
ditto "web/src-tauri/target/debug/bundle/macos/RCP Dev.app" "/Applications/RCP Dev.app"
open "/Applications/RCP Dev.app"
```

`/Applications/RCP.app` is reserved for the released download; see
[AGENTS.md](../AGENTS.md#three-desktop-builds) for the naming.

Closing the red window hides RCP. Use **Quit RCP** or Cmd+Q to end the
desktop-owned backend. See [docs/desktop.md](desktop.md) for native build,
logging, and verification details.

## Team server

Install promoted artifacts on Ubuntu using the [team server guide](server.md).
See [device pairing](device-pairing.md) for phones and [release](release.md) for
maintainer promotion.

## Verify a checkout

```bash
uv run pytest
uv run ruff check src tests packaging web/src-tauri/scripts
npm --prefix web exec playwright -- install chromium
npm --prefix web test
npm --prefix web run build
uv run pre-commit run --all-files
```

### Opt-in test gates

Some tests skip unless a tool is installed or an environment variable is set, so
a clean run still reports skips. CI installs `age` and runs the exact-base
upgrade; the rest need real credentials or a reachable host and are opt-in
locally.

| Gate | Unlocks |
| --- | --- |
| `age` and `age-keygen` on `PATH` | real age encryption of a backup archive, rather than the stub |
| `systemd-run` on `PATH` | the compute-job backend probe |
| `RCP_RUN_EXACT_BASE_UPGRADE=1` | upgrade from the exact previous commit's data, and release updates from every supported published stable `v*` release at or above `UPDATE_SOURCE_FLOOR` with current and stale caches, including the switched release's live check (needs authenticated `gh` and every release tag fetched) |
| `RCP_FROZEN_BACKEND=<path to the built backend binary>` | local unpushed commits through a frozen desktop backend |
| `RCP_RUN_GIT_CREDENTIALS_LIVE=1` plus `RCP_LIVE_GITHUB_ADMIN_TOKEN` and `RCP_LIVE_GITHUB_REPOSITORY` | live GitHub credential checks |
| `RCP_RUN_PROJECT_CHECKOUT_LIVE=1` plus `RCP_LIVE_PROJECT_CHECKOUT_SSH_HOST` and `RCP_LIVE_PROJECT_CHECKOUT_SSH_ACCOUNT` | project checkout over real SSH |
| `RCP_RUN_PROVIDER_READINESS_LIVE=1` | provider readiness against real logins |
| `RCP_LIVE_TRANSFER_GIT_HOST` | transfer against a real Git host |

Point the live gates at a throwaway account, repository, or host. Never at a
real project's data.
