# RCP

RCP is a source-built research control panel. The same Python backend and React
interface run in a browser or in the macOS desktop app.

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
- Bring your own machines and provider authentication; RCP checks and uses the
  native credentials already present on each configured execution account.
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
recovery are all documented in the [team server guide](docs/server.md).
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
