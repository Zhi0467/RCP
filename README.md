# RCP

RCP is a source-built research control panel. The same Python backend and React
interface run in a browser or in the macOS desktop app.

## Set up with your agent

- **Supported agents:** Codex and Claude Code (more to come).
- **Supported platforms:** macOS 13+ on Apple Silicon for the desktop app;
  Ubuntu 22.04 or 24.04 LTS on x86-64 for the web app and team server.

Send this to your agent:

> Clone Zhi0467/RCP from GitHub. Build the web app and, on macOS, the desktop app
> from source, following the README and docs/desktop.md. Then walk me through
> using RCP.

For a Linux machine your team will share as an RCP server, make sure you have SSH
access, then send:

> Set up my RCP team server using `<ssh-host>` as the SSH host, following
> docs/server.md. Pause and ask about optional setup choices or when you need
> sudo access. Then show me how to use my team space in the desktop app,
> including inviting people, transferring projects, and creating shared projects.

## Features

- Visualize ResearchQuestions, Hypotheses, Evidence, Experiments, Decisions,
  Blockers, and their relationships as one durable research graph.
- Discuss graph nodes, dispatch bounded Experiments, and start Auto-research
  directly from the graph.
- Use Codex or Claude through one provider-agnostic task interface, locally or
  over SSH, with provider/runtime/model settings per agent role.
- Keep background tasks, watchers, budgets, recovery, and visual episode reports
  running independently of an open browser tab.
- Inspect generated artifacts in the built-in viewer, annotate text or image
  regions, and send questions or revision requests back to the originating
  agent.
- Build a paper introduction from human-approved research while agents gather
  evidence and propose graph changes.
- Bring your own machines and provider subscriptions; members sign each
  execution account in from Settings (Codex by device code, Claude by a pasted
  setup token), and RCP verifies every login with one real request.
- Run a team space on your own Linux server, with shared projects, one team
  provider credential per execution account, central Git checkouts, member
  attribution, backup/restore, and personal-to-team project transfer.

## Install from source

Requirements:

- Git;
- Node.js and npm;
- [`uv`](https://docs.astral.sh/uv/); and
- Codex CLI or Claude Code, installed and authenticated separately if you want
  to run agent tasks.

Clone and build in this order:

```bash
git clone https://github.com/Zhi0467/RCP.git
cd RCP
npm --prefix web ci
npm --prefix web exec playwright -- install chromium
npm --prefix web run build
uv sync
```

The Playwright command installs the managed Chromium binary used by the web
interaction tests and is needed once after `npm ci`. The build order matters
because the Python build includes `web/dist`.

Every `uv run` command rebuilds this checkout's editable install, so the same
order applies to each new checkout, including a new Git worktree. Run
`npm --prefix web run build` there before the first `uv run` in it. The
`web/dist present` hook in `.pre-commit-config.yaml` stops a commit with that
instruction if you forget, because otherwise the build failure appears as a long
traceback above the later passing hooks.

## Run the local Web app

For development with backend reload and automatic Web rebuild:

```bash
uv run rcp serve --reload
```

Open <http://127.0.0.1:8421>. React and CSS changes rebuild the bundle; refresh
the page to see them.

For a normal non-reloading launch that opens the browser:

```bash
uv run rcp open
```

Register and open a checkout at launch:

```bash
uv run rcp open /absolute/path/to/project
```

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

The desktop app additionally requires Rust and the Xcode command-line tools.

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
web/src-tauri/target/debug/bundle/macos/RCP.app
```

Launch that exact bundle. If you keep a Dock/Finder copy at
`/Applications/RCP.app`, rebuilding does not update it: quit RCP with Cmd+Q,
then replace the installed copy before reopening it:

```bash
ditto web/src-tauri/target/debug/bundle/macos/RCP.app /Applications/RCP.app
open /Applications/RCP.app
```

Closing the red window hides RCP. Use **Quit RCP** or Cmd+Q to end the
desktop-owned backend. See [docs/desktop.md](docs/desktop.md) for native build,
logging, and verification details.

## Team server

RCP can run a shared team space from source on a lab-owned Ubuntu server.
Installation, member invitations and joining, shared-project setup, provider
maintenance, updates, backup, restore, member removal, verification, and
recovery are all documented in the [team server guide](docs/server.md); connecting
phones and other devices is in [docs/device-pairing.md](docs/device-pairing.md).
Maintainers release through the build, tag, and promote process in
[docs/release.md](docs/release.md).

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
| `RCP_RUN_EXACT_BASE_UPGRADE=1` | upgrade from the exact previous commit's data |
| `RCP_FROZEN_BACKEND=<path to the built backend binary>` | local unpushed commits through a frozen desktop backend |
| `RCP_RUN_GIT_CREDENTIALS_LIVE=1` plus `RCP_LIVE_GITHUB_ADMIN_TOKEN` and `RCP_LIVE_GITHUB_REPOSITORY` | live GitHub credential checks |
| `RCP_RUN_PROJECT_CHECKOUT_LIVE=1` plus `RCP_LIVE_PROJECT_CHECKOUT_SSH_HOST` and `RCP_LIVE_PROJECT_CHECKOUT_SSH_ACCOUNT` | project checkout over real SSH |
| `RCP_RUN_PROVIDER_READINESS_LIVE=1` | provider readiness against real logins |
| `RCP_LIVE_TRANSFER_GIT_HOST` | transfer against a real Git host |

Point the live gates at a throwaway account, repository, or host. Never at a
real project's data.
