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

Run Steps 1–6 and 8–12 for every installation. Step 7 is required only while the
RCP source repository is private; the public-source transition removes that
pause and its read-only source key. Project repositories retain their separate
write deploy keys.

### 1. Connect as the ordinary server operator

The operator needs SSH access and `sudo`; do not log in as `rcp` or create that
account yourself. RCP's installer owns it.

```bash
ssh operator@server.example
sudo -v
```

### 2. Confirm the supported host

```bash
uname -m
. /etc/os-release
printf '%s %s\n' "$ID" "$VERSION_ID"
systemctl show --property=Version --value
```

Continue only for `x86_64`, Ubuntu `22.04` or `24.04`, and a nonempty systemd
version.

### 3. Install the Ubuntu prerequisites

The same command applies to both supported Ubuntu releases:

```bash
sudo apt-get update
sudo apt-get install --yes age ca-certificates curl git iproute2 libc-bin openssh-client openssh-server passwd sudo util-linux xz-utils
```

### 4. Install the pinned Node.js and `uv`

Install Node.js 24 from its checksummed upstream archive:

```bash
(
  RCP_NODE_VERSION="v24.20.0"
  RCP_NODE_ARCHIVE="node-${RCP_NODE_VERSION}-linux-x64.tar.xz"
  RCP_NODE_DOWNLOAD_DIR="$(mktemp -d)"
  cd "$RCP_NODE_DOWNLOAD_DIR"
  curl --fail --show-error --location --remote-name \
    "https://nodejs.org/dist/${RCP_NODE_VERSION}/${RCP_NODE_ARCHIVE}"
  curl --fail --show-error --location --remote-name \
    "https://nodejs.org/dist/${RCP_NODE_VERSION}/SHASUMS256.txt"
  grep " ${RCP_NODE_ARCHIVE}$" SHASUMS256.txt | sha256sum --check --strict
  sudo tar --extract --xz --file "$RCP_NODE_ARCHIVE" \
    --directory /usr/local --strip-components=1 --no-same-owner
)
node --version
npm --version
```

Success is Node.js `v24.20.0` and a nonempty npm version. Then install the pinned
system-wide `uv` binary:

```bash
(
  RCP_UV_VERSION="0.12.7"
  RCP_UV_ARCHIVE="uv-x86_64-unknown-linux-gnu.tar.gz"
  RCP_UV_SHA256="788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21"
  RCP_UV_DOWNLOAD_DIR="$(mktemp -d)"
  cd "$RCP_UV_DOWNLOAD_DIR"
  curl --fail --show-error --location --remote-name \
    "https://releases.astral.sh/github/uv/releases/download/${RCP_UV_VERSION}/${RCP_UV_ARCHIVE}"
  printf '%s  %s\n' "$RCP_UV_SHA256" "$RCP_UV_ARCHIVE" | \
    sha256sum --check --strict
  tar --extract --gzip --file "$RCP_UV_ARCHIVE"
  sudo install --owner=root --group=root --mode=0755 \
    "uv-x86_64-unknown-linux-gnu/uv" /usr/local/bin/uv
)
uv --version
```

Success begins with `uv 0.12.7`. Confirm the remaining executables:

```bash
git --version
ssh -V
age --version
command -v age curl getent git node npm runuser ssh ssh-keygen sudo systemctl useradd uv
```

### 5. Build a temporary bootstrap checkout

Run this as the ordinary operator, not with `sudo`:

```bash
git clone https://github.com/Zhi0467/RCP.git rcp-bootstrap
cd rcp-bootstrap
npm --prefix web ci
npm --prefix web run build
UV_MANAGED_PYTHON=1 UV_PYTHON=3.12 uv sync --frozen
```

The bootstrap uses the operator's existing GitHub access only for this clone;
credentials are not copied into the installed service account.

### 6. Start the installer

```bash
sudo /usr/bin/env PYTHONDONTWRITEBYTECODE=1 \
  "$PWD/.venv/bin/rcp" server install --team-name "My lab"
```

Replace `My lab` consistently throughout the remaining commands. The installer
prints all steps before mutation, creates the dedicated `rcp` Linux account, and
pauses whenever human action is required.

### 7. Private source only: grant read access and resume

Skip this step when the RCP source repository is public. While it is private,
the installer exits with status 3 and prints a source public key, fingerprint,
GitHub settings page, host-trust action, and exact resume command. Add that key
to the RCP source repository as a deploy key with **Allow write access** left
unchecked. Run the printed host-trust action, then rerun the exact installer
command it prints.

This source key is only for pulling RCP `main`. It is unrelated to the
write-enabled deploy key each team project receives. When RCP goes public, the
source-key pause, key material, and this README step are removed together.

### 8. Initialize the team space

After source installation, the CLI pauses again because only a human may create
the team space. Run the exact command it prints, equivalent to:

```bash
sudo -u rcp -H /usr/local/bin/rcp space init --team --name "My lab"
```

Save the one-time bootstrap enrollment code outside logs and shell history.

### 9. Finish installation and verify the service

Rerun the installer as instructed:

```bash
sudo /usr/local/bin/rcp server install --team-name "My lab"
curl --fail --silent http://127.0.0.1:8421/api/health
sudo -u rcp -H /usr/local/bin/rcp server doctor
```

The final installer run enables and starts the non-reloading systemd service.
Health must report `status: ok`, `space_kind: team`, and the selected team name;
doctor must report a healthy installed release. The temporary bootstrap checkout
is not the production checkout and may now be removed.

### 10. Authenticate providers as their execution account

RCP does not log in to providers. For server-local execution, enter the service
account and use each provider's own installation/login flow:

```bash
sudo -u rcp -H /bin/bash
```

Exit that shell after the provider's native login succeeds. RCP stores no
provider credential; project setup later checks whatever that account has
authenticated.

### 11. Configure the desktop's SSH operator route

Use the narrow named-operator route in [docs/server.md](docs/server.md#8-configure-one-operator-route).
The development-only alternative is direct public-key SSH to `rcp`. Do not open
RCP's loopback port 8421 publicly.

### 12. Add the team space in the desktop app

In the source-built desktop app, choose **Add team space**, select SSH, enter the
saved server route, and enroll with the one-time bootstrap code from Step 8. The
unified project wizard can then create a team project from GitHub or move an
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
