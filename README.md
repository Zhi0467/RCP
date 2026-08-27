# RCP

RCP is a source-built research control panel. The same Python backend and React
interface run in a browser or in the macOS desktop shell.

## Features

- **Structured research graph:** organize ResearchQuestions, Hypotheses,
  Evidence, Experiments, Decisions, Blockers, and their relationships in one
  durable visual workspace.
- **Work directly from the graph:** discuss a node, run bounded Experiments, or
  launch Auto-research without translating the project into a separate task
  tracker.
- **Provider-flexible execution:** use Codex or Claude through one RCP task
  interface, with provider profiles running locally or over SSH.
- **Durable autonomous work:** background tasks, watchers, recovery, budgets,
  and visual episode reports survive closed tabs and hidden windows.
- **Artifact feedback loop:** open generated HTML, SVG, and image artifacts;
  select text or regions, add comments or questions to the originating agent
  chat, and explicitly keep or revise useful results.
- **Human authority and writing:** agents gather evidence and propose graph
  changes while humans retain protected decisions and authorship of the paper.
- **Bring your own machines:** keep canonical state and repositories locally or
  on SSH hosts while RCP applies the same task and containment contract.
- **Team server — in progress:** run RCP from source on a lab Linux server so
  members can collaborate on the same projects through server-owned checkouts
  and provider credentials without sharing one human identity.

## Install from source

Requirements:

- Git;
- Node.js and npm;
- [`uv`](https://docs.astral.sh/uv/); and
- Codex CLI or Claude Code, installed and authenticated separately if you want
  to run agent tasks.

Clone and prepare RCP in this order:

```bash
git clone https://github.com/Zhi0467/RCP.git
cd RCP
npm --prefix web ci
npm --prefix web run build
uv sync
```

The order matters: the Python build includes the generated `web/dist` bundle.

## Run the local Web app

For source development with automatic backend restart and frontend rebuild:

```bash
uv run rcp serve --reload
```

Open <http://127.0.0.1:8421>. Refresh the page after React or CSS changes; the
source watcher rebuilds the bundle but does not provide Vite hot-module reload.

For a normal non-reloading launch that also opens your browser:

```bash
uv run rcp open
```

You can register and open a project at launch:

```bash
uv run rcp open /absolute/path/to/project
```

To serve without opening a browser:

```bash
uv run rcp serve --host 127.0.0.1 --port 8421
```

By default, local application data lives at:

```text
~/Library/Application Support/research-control-panel/
```

Set `RCP_DATA_DIR` before launch to use another data directory. Canonical
research history remains in each project's configured state repository.

## Run the macOS desktop app

Desktop development also requires Rust and the Xcode command-line tools.

Start the source desktop shell:

```bash
npm --prefix web run desktop:dev
```

To build a Finder-launchable development app from the current checkout:

```bash
npm --prefix web run desktop:build-dev
```

The bundle is written to:

```text
web/src-tauri/target/debug/bundle/macos/RCP Dev.app
```

Open that bundle through Finder for real desktop testing. Closing the red window
hides RCP; **Quit RCP** ends the desktop-owned backend. More native build and
verification commands are in [docs/desktop.md](docs/desktop.md).

## Install the team server from source

> **Not implemented yet:** the source-server CLI described below is the accepted
> team deployment target. Until that slice lands, use the local source commands
> above; do not expect `rcp server install` or `rcp server update` to exist.

The first supported team deployment is one Linux server running systemd and one
team space. The service binds to loopback and members connect with source-built
desktop apps over SSH.

An operator first creates a temporary bootstrap checkout under their ordinary
Linux account:

```bash
git clone https://github.com/Zhi0467/RCP.git rcp-bootstrap
cd rcp-bootstrap
npm --prefix web ci
npm --prefix web run build
uv sync
```

The first privileged RCP command is then run by that operator with `sudo`:

```bash
sudo /absolute/path/to/rcp-bootstrap/.venv/bin/rcp server install
```

The installer will:

1. create or validate the dedicated Linux `rcp` account;
2. create a separate managed Git checkout and clean release directory for the
   exact GitHub `main` commit in the configured service layout;
3. give that checkout its own source-fetch identity when the origin is private;
4. run Git, npm, the Web build, and `uv sync --frozen` as `rcp`, not as root;
5. install the stable CLI wrapper and non-reloading systemd service; and
6. print the exact command for initializing the team space and enrolling its
   first member.

The bootstrap checkout is not the production checkout and may be removed after
installation. Root is used only for operating-system work such as creating the
account, directories, and systemd service. Normal service execution and managed
source work run as `rcp`.

Later source updates are owned by:

```bash
sudo rcp server update
```

That command accepts only a clean fast-forward of the managed `main` checkout,
builds the target in a separate clean per-commit source directory as `rcp`, and
leaves the running release untouched until preflight passes. Its narrow root
portion switches the service's `current` release, restarts systemd, and verifies
the running commit. A failure never silently rolls back. `rcp server doctor`
reports the managed-main, candidate, current, and running commits. The `rcp`
account receives no general sudo or systemd-control permission.

## Project documentation

Current product and implementation contracts live in [docs/design.md](docs/design.md),
[docs/specs/](docs/specs/), and [docs/acceptance/](docs/acceptance/). Repository
instructions for coding agents are in [AGENTS.md](AGENTS.md).
