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
npm --prefix web run build
uv sync
```

The order matters because the Python build includes `web/dist`.

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

Closing the red window hides RCP. Use **Quit RCP** or Cmd+Q to end the
desktop-owned backend. See [docs/desktop.md](docs/desktop.md) for native build,
logging, and verification details.

## Install a team server from source

The server workflow below has passed source install, update/rollback, protected
backup, and fresh-host restore on disposable Ubuntu 22.04 and 24.04 hosts. The
complete desktop/provider/collaboration one-lab qualification is still pending.

The supported server is Ubuntu 22.04 or 24.04 LTS on x86-64 with systemd. RCP is
built from a GitHub `main` checkout; there is no Linux package or binary release
channel. The service runs under a dedicated `rcp` Linux account and listens only
on server loopback. Source-built desktop apps connect through SSH.

Install the pinned Node.js 24, `uv`, Git, OpenSSH, `age`, and other prerequisites
from the exact commands in [docs/server.md](docs/server.md). Then create a
temporary bootstrap checkout under the ordinary operator account:

```bash
git clone https://github.com/Zhi0467/RCP.git rcp-bootstrap
cd rcp-bootstrap
npm --prefix web ci
npm --prefix web run build
UV_MANAGED_PYTHON=1 UV_PYTHON=3.12 uv sync --frozen
```

Run the first RCP command as root from that checkout:

```bash
sudo /usr/bin/env PYTHONDONTWRITEBYTECODE=1 \
  "$PWD/.venv/bin/rcp" server install --team-name "My lab"
```

The CLI prints its complete plan and pauses whenever a human must act, and you
resume each time by rerunning the same command. While this repository is
private, the first pause asks for a read-only deploy key so the `rcp` account
can pull `main` for later updates; a public origin skips that pause entirely.
The next pause creates the team space, because only a human may create one:

```bash
sudo -u rcp -H /usr/local/bin/rcp space init --team --name "My lab"
sudo /usr/local/bin/rcp server install --team-name "My lab"
```

The final installer run enables and starts the non-reloading systemd service and
verifies its team-space health. The bootstrap checkout is not the production
checkout and may then be removed.

RCP does not log in to providers. For server-local execution, enter the service
account and use each provider's own installation/login flow:

```bash
sudo -u rcp -H /bin/bash
```

Configure either the documented narrow named-operator sudo route or the
development-only direct key route, then use **Add team space** in the desktop
app. The unified project wizard can create a team project from GitHub or move an
existing personal RCP project into the team space.

## Update and operate the team server

The CLI owns server updates from GitHub `main`:

```bash
sudo /usr/local/bin/rcp server update
```

Review the fetched immutable commit, then run the exact
`--confirm-target <40-character-commit>` command RCP prints. The confirmed
update builds and rehearses a detached candidate before systemd cutover, and
keeps a durable rollback/recovery boundary.

The same console interface owns health, project provisioning, provider checks,
backup, restore, and member removal:

```bash
sudo -u rcp -H /usr/local/bin/rcp server doctor
sudo -u rcp -H /usr/local/bin/rcp server project provision <request-id>
sudo -u rcp -H /usr/local/bin/rcp server provider check --project <project-id>
sudo /usr/local/bin/rcp server backup configure \
  --destination /absolute/path/to/backups \
  --recipient <age1-public-recipient> \
  --confirm
sudo -u rcp -H /usr/local/bin/rcp server backup run
sudo /usr/local/bin/rcp server restore /absolute/path/lab.tar.age \
  --identity-file /absolute/protected/path/age-identity.txt
sudo -u rcp -H /usr/local/bin/rcp server member remove <member-id>
```

Each command prints its full plan, human-action boundaries, success checks, and
exact resume command. Use [docs/server.md](docs/server.md) for prerequisite,
deploy-key, operator-route, backup/restore, update, and recovery procedures.

## Verify a checkout

```bash
uv run pytest
uv run ruff check src tests
npm --prefix web test
npm --prefix web run build
uv run pre-commit run --all-files
```
