# Install and run RCP

Install the released Mac app or run from a checkout. Use [the team server guide](server.md)
for Ubuntu 22.04 or 24.04 LTS x86-64 and [desktop testing](desktop.md) for native qualification.

## Install the released macOS app

Use macOS 13 or later on Apple Silicon. Quit RCP with Cmd+Q before installing
or replacing it. From an admin account, install Git if needed and run:

```bash
xcode-select --install
curl -fsSL https://raw.githubusercontent.com/Zhi0467/RCP/main/scripts/install-macos.sh | sh
open /Applications/RCP.app
```

Use the app's **Update** button for subsequent releases, or rerun the installer.
For manual installation, download `RCP-vX.Y.Z-macos-arm64.zip` from the matching
`desktop-vX.Y.Z` [release](https://github.com/Zhi0467/RCP/releases), unzip it and
move `RCP.app` to Applications. If macOS blocks first launch, open **System
Settings → Privacy & Security → Open Anyway**.

## Install from source

Install Git, Node.js, npm, and [`uv`](https://docs.astral.sh/uv/).
Install and authenticate a supported provider CLI separately for agent tasks.
Copy a `vX.Y.Z` tag from the [latest release](https://github.com/Zhi0467/RCP/releases/latest),
then replace `<tag>` below:

```bash
git clone https://github.com/Zhi0467/RCP.git
cd RCP
git checkout --detach refs/tags/<tag>
npm --prefix web ci
npm --prefix web exec playwright -- install chromium
npm --prefix web run build
uv sync
```

Build `web/dist` before `uv sync` or `uv run`. The Python package includes it.
Repeat the Web dependency install and build in each new checkout or worktree.

## Update a source checkout

Quit desktop RCP with Cmd+Q and stop every backend using this checkout.
Commit or move local changes and untracked files. Replace `<tag>` with a
promoted release tag, then run one of:

```bash
scripts/update-from-source <tag>
scripts/update-from-source <tag> --desktop
```

For `--desktop`, install Rust and Xcode command-line tools first. Follow any
recovery commands printed on failure. Restart Web with `uv run rcp serve`.
Replace a copied development app using the commands below.

## Run the local Web app

Run this command to enable backend reload and automatic Web rebuilds.
Then open the printed sign-in URL:

```bash
uv run rcp serve --reload
```

Refresh after React or CSS changes. For a normal launch that opens the browser:

```bash
uv run rcp open
uv run rcp open /absolute/path/to/project
```

When attaching to an existing backend, confirm the project locator in the browser.
To serve without opening a browser:

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

Set `RCP_DATA_DIR` before launch to select a different app-data directory.
See [personal sign-in](specs/projects-spaces-and-operations.md#personal-sign-in)
for authentication and data ownership.

## Run the source-built macOS desktop app

Install Rust and Xcode command-line tools. Run from the checkout or build a
Finder-launchable development app:

```bash
npm --prefix web run desktop:dev
npm --prefix web run desktop:build-dev
```

Launch `web/src-tauri/target/debug/bundle/macos/RCP Dev.app`. To refresh a
separate installed copy, quit RCP with Cmd+Q first, then:

```bash
ditto "web/src-tauri/target/debug/bundle/macos/RCP Dev.app" "/Applications/RCP Dev.app"
open "/Applications/RCP Dev.app"
```

Follow [the three-build naming convention](desktop.md#three-desktop-builds).
Use **Quit RCP** or Cmd+Q to stop the desktop-owned backend.

## Team server

Follow [server installation](server.md), [device pairing](server.md#reach-the-team-space-from-a-phone),
and [release promotion](release.md) for those procedures.

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

Enable applicable gates below. Point live checks at throwaway accounts,
repositories and hosts. Never use a real project's data.

| Gate | Check enabled |
| --- | --- |
| `age` and `age-keygen` on `PATH` | Tests real backup encryption. |
| `systemd-run` on `PATH` | Probes the compute backend. |
| `RCP_RUN_EXACT_BASE_UPGRADE=1` | Tests upgrades from the exact previous commit and supported stable releases. It needs authenticated `gh` and fetched release tags. |
| `RCP_FROZEN_BACKEND=<binary>` | Tests local unpushed commits through a frozen desktop backend. |
| `RCP_RUN_GIT_CREDENTIALS_LIVE=1`, `RCP_LIVE_GITHUB_ADMIN_TOKEN`, `RCP_LIVE_GITHUB_REPOSITORY` | Tests live GitHub credentials. |
| `RCP_RUN_PROJECT_CHECKOUT_LIVE=1`, `RCP_LIVE_PROJECT_CHECKOUT_SSH_HOST`, `RCP_LIVE_PROJECT_CHECKOUT_SSH_ACCOUNT` | Tests a real SSH project checkout. |
| `RCP_RUN_PROVIDER_READINESS_LIVE=1` | Tests real provider logins. |
| `RCP_LIVE_TRANSFER_GIT_HOST` | Tests transfer against a real Git host. |
