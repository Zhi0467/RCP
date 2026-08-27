# Dev team space and source server completion handoff

Date: 2026-08-27
Status: active; implementation scope is human-confirmed and final dispatch
grilling is in progress; the personal-to-team archive boundary is settled

## Objective

Finish the development team-space and server slice until RCP is genuinely usable
by one lab operating one Linux server:

- RCP server installation and updates are managed from a Git checkout of GitHub
  `main`; there is no RCP package or distribution channel;
- the server runs as a stable, non-reloading service under a dedicated Linux
  `rcp` account;
- researchers use source-built RCP desktop apps as distinct RCP members;
- every team project uses one team-controlled central checkout per declared
  repository on its configured local or SSH machine, with a distinct
  repository-scoped write deploy key;
- local and SSH provider calls retain one provider abstraction and execute only
  on their explicitly configured accounts;
- a human starts project setup or transfer in the app, while the server CLI owns
  machine work and may be invoked by the desktop over a separately proven SSH
  operator route;
- backup, restore, update, provider setup, project provisioning, and member
  removal are real console workflows; and
- one complete live lab drill proves install, connection, collaboration,
  execution, update, backup, restore, and transfer rather than stopping at unit
  tests.

Current product authority is in:

- [Research Control Panel design](../design.md);
- [Projects, spaces, and operations](../specs/projects-spaces-and-operations.md);
- [API, Web, and desktop projections](../specs/api-web-and-desktop-projections.md);
- [Providers and containment](../specs/providers-and-containment.md); and
- pending scenarios [S95](../acceptance/S95-durable-team-space.md),
  [S98](../acceptance/S98-move-a-project-into-a-team-space.md),
  [S102](../acceptance/S102-team-runs-execute-as-the-space-account.md),
  [S103](../acceptance/S103-server-operations-are-console-operations.md),
  [S104](../acceptance/S104-backups-never-pause-work.md),
  [S105](../acceptance/S105-move-between-spaces-in-one-window.md), and
  [S128](../acceptance/S128-provision-a-team-project-through-desktop-and-server-cli.md).

This handoff scopes implementation. It does not override those documents.

## Opening status: implemented and verified

The foundation already exists:

- durable `space_id`, immutable personal/team kind, and separate process/data
  identities;
- `rcp space init --team`, one-time bootstrap enrollment, invitations, permanent
  member credentials, server sessions, rotation, and revocation;
- durable human identities and attribution;
- per-project membership, invitations, leave, catalog filtering, and admission
  rechecks before Apply;
- one process per data directory and loopback-only serving for a team space;
- durable random `project_id` plus current `home_space_id` in canonical history;
- local and SSH launch plumbing with exact provider/runtime profiles and
  provider-owned Codex exec, Codex app-server, and Claude implementations; and
- source-mode desktop/backend launch plus current native navigation, window,
  update, and command infrastructure.

The focused existing team foundation was fact-checked before this handoff with:

```bash
uv run pytest \
  tests/test_main.py \
  tests/test_team_authentication.py \
  tests/test_project_membership.py \
  tests/test_project_invitations.py \
  tests/test_identity_api.py
```

Result: 93 passed. That proves the existing identity/authentication/membership
slice, not the pending server or desktop journeys.

The missing seams are also concrete:

- `src/rcp/__main__.py` has `serve`, `open`, and `space init`; it has no `server`
  command family;
- `default_data_dir()` still falls back to the macOS Application Support path;
  a Linux service works only through an explicit `RCP_DATA_DIR` today;
- no systemd unit, service-account installer, private server-control socket,
  server doctor, source-update workflow, backup, or restore exists;
- the Web UI still says “Team connections are not implemented in this build”;
- the Tauri shell trusts one current loopback backend and has no saved team
  connection, credential-store, SSH-tunnel, or operator-command owner;
- no durable project-provisioning request or personal-to-team transfer record
  exists; and
- canonical identity replay currently treats two differing identity payloads as
  corruption, so a home transfer cannot be represented by appending a second
  `ProjectIdentity` record.

At the time this handoff was written, the worktree also contained unrelated
in-progress artifact-viewer and episode/UI edits. They were preserved. Every
implementation session must begin with `git status --short` and must not reset,
overwrite, stage, or claim those changes as part of this handoff.

## What remains

Everything after the existing auth/membership foundation remains implementation
work:

1. source-server installation, service ownership, health, and update;
2. private machine-local CLI-to-server control;
3. durable project-provisioning state and API projections;
4. central Git checkout and write-deploy-key setup;
5. local/remote provider-account setup and readiness;
6. source-built desktop connections, credential storage, tunnels, navigation,
   and optional operator bridge;
7. app-visible project setup driven by the backend and prepared by the CLI;
8. encrypted online backup, scheduling, restore, and server status;
9. console member removal;
10. append-only personal-to-team home transfer and recovery; and
11. a live one-lab acceptance drill and operator documentation.

No item in that list is implemented merely because its design is now confirmed.

## Settled decisions

### Deployment and source update

- First target: one lab, one Linux server, one team space.
- Server and desktop are built from source. No Linux RCP package, container,
  release binary, or hosted deployment is required.
- A normal operator creates the disposable bootstrap checkout and runs its
  source setup without privilege. The first privileged RCP command is that
  checkout's absolute `.venv/bin/rcp server install` path under `sudo`.
- Install creates a separate clean managed checkout owned by `rcp`; the
  bootstrap checkout never becomes production state and may be removed.
- Root performs only account, directory, systemd, and other OS changes. Managed
  Git/npm/Web/uv work runs as `rcp`.
- The installed server version is the exact commit in its current source
  release. The managed Git checkout tracks `main`; a separate clean release
  directory holds every built candidate/current commit.
- The configured update branch is GitHub `origin/main`.
- `rcp server update` owns fetch, managed-main fast-forward, a clean per-commit
  release directory, `npm ci`, Web build, `uv sync --frozen`,
  migration/readiness preflight, current-pointer switch, graceful restart, and
  running-commit readback.
- An operator invokes it as `sudo rcp server update`. The coordinator runs every
  managed Git/npm/Web/uv step as `rcp` and uses root only for systemd restart and
  readback. Do not grant `rcp` general sudo or systemd-control permission.
- The RCP source checkout has separate fetch access: no credential for a public
  origin or a dedicated read-only source deploy key for a private origin. It
  never uses an operator's personal SSH key or a project's write deploy key.
- Dirty, divergent, detached, non-`main`, inconsistent-release, failed-build, or
  failed-readiness state fails loudly. Candidate failure does not touch the
  running release. Never reset changes, force-pull, silently roll back, or switch
  to a package.
- Source-built service operation is stable and non-reloading. `--reload` remains
  a developer command, not the team service.

### Accounts and credentials

- RCP member identity, Linux service identity, Git identity, and provider
  identity are separate.
- The dedicated `rcp` account owns the service, data, runtime socket, local
  secrets, and server-local project checkouts. An explicit remote execution
  account owns its remote team checkout and credentials. Humans do not share
  either process identity as their RCP identity.
- Z and Alice may each keep personal checkouts. Those checkouts are not discovered
  or imported into the team project.
- Each central GitHub checkout uses its own repository-scoped SSH deploy key on
  the account that owns the local or remote checkout and expects write access.
  Because GitHub's UI defaults deploy keys to read-only and forbids reusing one
  deploy key across repositories, RCP explicitly instructs the operator to
  enable **Allow write access** and verifies a real request-scoped
  push/readback/cleanup with each key.
- RCP never asks for or stores a member's personal GitHub token.
- A local provider runs under `rcp` and uses that account's provider login. A
  remote provider runs under the exact configured SSH account and uses that
  remote account's login. The remote account need not be named `rcp`.
- No provider call falls back to a member laptop, personal checkout, personal
  login, or different SSH account.

### Product authority versus machine authority

- RCP has equal members and no administrator product role.
- A member token cannot install, update, restore, configure machine credentials,
  provision a checkout, or remove another member.
- Those operations live under `rcp server ...` and require OS authority.
- A running-server CLI command never opens SQLite beside the lock owner. It uses
  a private Unix-domain control socket owned by `rcp`.
- The CLI has one concrete implementation with interactive output and bounded
  machine-readable progress. The desktop consumes the structured form; it does
  not get a second implementation.
- Do not add an application CLI for graph, chat, task, episode, or ordinary
  membership actions.

### UI, desktop, and CLI coordination

- A human starts **Create team project** or **Move to team space** in the app.
- The backend persists the request before machine work and owns these displayed
  states: **waiting for server setup**, **setup in progress**, **operator action
  needed**, **ready for review**, **completed**, and **cancelled**.
- The Web UI renders backend decisions. It never infers readiness from Git files,
  subprocess output, or a zero CLI exit code.
- Machine preparation alone never creates or re-homes a canonical project. Final
  explicit human review performs that authority action.
- A browser may create/review a request and copy its server command, but it cannot
  invoke server operations.
- If the desktop proves a saved operator route can invoke the fixed CLI directly
  as `rcp` or through `sudo -n -u rcp -H`, it offers **Run setup now**.
- The shell uses system SSH configuration and the user's SSH agent. It never
  imports private keys or collects a `sudo` password. If interaction is needed,
  open/show the exact command in Terminal.
- A direct `rcp@server` route is allowed for this development target. A named
  operator account plus narrow `sudo` is preferred for independent audit and
  revocation.

### Desktop connection boundary

- Source-built RCP desktop is the supported member client.
- After one controlled entry/enrollment exchange, permanent RCP member tokens
  live only in the operating-system credential store. Secret UI/IPC state is
  cleared; nonsecret connection metadata lives separately.
- Each saved space receives a stable distinct loopback origin. Different ports
  on `127.0.0.1` are forbidden as isolation because cookies ignore ports.
- The native shell owns SSH tunnel lifetime, health/`space_id`/minimum-version
  handshake, token exchange, WebView session establishment, and origin
  navigation.
- The personal backend stays alive while the window views a team backend. App
  Quit stops only processes/tunnels owned by the desktop, never the remote team
  service.
- An unavailable team space does not block personal work and never reroutes team
  work locally.

### Backup and restore

- Online backup never pauses dispatch or Apply.
- SQLite uses a consistent online snapshot. Canonical capture records a head and
  includes only append-only state needed to replay through that head.
- Materialized outputs, source repositories, scratch, caches, Git keys, SSH
  keys, and provider credentials are excluded.
- Archives are encrypted to an `age` public recipient on the server. The private
  recovery identity remains off-server.
- One unreachable project makes the archive partial and visibly unprotected; it
  does not erase successful captures or get called complete.
- Restore is console-only, validates/decrypts/replays before serving, preserves
  `space_id`, marks captured active work interrupted, and requires the operator
  to affirm the old copy cannot resume.

### Transfer

- Only personal-to-team product transfer is in scope.
- The target uses a separate central checkout set. `rcp` owns server-local
  checkouts and each explicit remote execution account owns its SSH checkouts;
  the personal checkouts retain their paths and owners.
- The source must be fenced before target admission. Recovery may temporarily
  leave no writable home, never two writable homes.
- Provider sessions, scratch, caches, credentials, and machine configuration do
  not transfer.
- The durable `project_id`, canonical history, home change, and attribution do
  transfer.

### Transfer archive

- One versioned, checksummed archive is the sole personal-to-team transfer
  format. It carries canonical history, all finished human-visible operational
  history, and explicitly kept artifact bytes.
- Export removes provider-native sessions, reusable stages, host/root bindings,
  live continuations, scratch/cache pointers, credentials, and machine config.
- Imported records are readable historical evidence and cannot Resume or Retry
  through source execution bindings. Future work starts through team config.
- The target validates the complete archive before mutation, imports selected
  rows in one SQLite transaction, publishes files through existing atomic
  owners, and activates only after database and file readback.
- The accepted rationale is recorded in the
  [personal-to-team transfer decision](../decisions/2026-08-27-personal-to-team-transfer-archive.md).

## Explicit non-goals

Do not add any of the following to finish this handoff:

- packaged Linux RCP, Docker, Kubernetes, a hosted service, or a binary release
  channel;
- public HTTPS, VPN configuration, reverse-proxy automation, or Internet-facing
  team serving;
- multi-server authority, automatic failover, replicated SQLite, or automatic
  detection of an old restored authority;
- per-member or per-project Linux service accounts;
- member-laptop team execution or checkout discovery;
- team-to-team transfer, team-to-personal product transfer, or fresh-identity
  fork;
- GitHub OAuth, personal access-token custody, or a general secret manager;
- automatic source merges, force-pulls, branch repair, or rollback of server
  source;
- a browser route that can run machine commands;
- a generic admin HTTP API, plugin registry, event bus, or second orchestration
  layer; or
- backup claims that have not survived a real restore.

## Work-packet discipline

The human preference is file/module-level work, normally about one focused agent
turn or roughly one hour of human engineering. Do not hand a Luna-max agent
“build the server” or “finish desktop team mode.” Assign one packet below, with
the listed files as its ownership boundary.

Workers are not alone in the tree. They must inventory first, preserve unrelated
edits, avoid reverting other packets, and adapt to already-landed dependencies.
The integrating agent retains schema/API compatibility, full diff review, live
verification, and documentation lifecycle.

New concrete server policy may live under `src/rcp/server_ops/`. Keep command
policy in its owning module; do not build a generic manager/facade. The top-level
CLI should parse and dispatch, while install, update, Git, provider, backup,
restore, and member-removal behavior remains separately navigable.

## Dependency map

| Lane | Packets | May begin after | Gate |
|---|---|---|---|
| Server foundation | F1–F6 | immediately, in order | live Linux host for F3/F6 |
| Provisioning | P1–P6 | F1 and F4 | disposable GitHub repository for P3/P4 |
| Desktop | D1–D7 | D1 immediately; D3+ after D2; D7 after P2 | live WKWebView proof at D2/D4 |
| Operations | O1–O6 | F4; O4 after O1–O3 | off-server `age` identity and restore host |
| Transfer | T1–T5 | P6 | two spaces and two writable-root candidates |
| Closure | V1–V2 | all applicable packets | genuine one-lab live drive |

F4, P1, D1, and O1 may be assigned in parallel once their dependencies are
landed because they own different modules. Do not parallelize schema/reducer
changes that touch the same `storage/base.py`, `core/models.py`, or
`history/manager.py` regions.

## Server-foundation packets

### F1 — Server CLI command and event contract

Own:

- `src/rcp/__main__.py`;
- new `src/rcp/server_ops/cli.py` and `models.py`; and
- focused parser/serialization tests in `tests/test_main.py` or
  `tests/test_server_cli.py`.

Deliver:

- `rcp server install`, `doctor`, `provider configure`, `project provision`,
  `backup configure`, `backup run`, `restore`, `member remove`, and `update`;
- a versioned bounded progress record with command, phase, state, message,
  timestamp, and optional nonsecret fields;
- interactive and `--machine-readable` renderers over the same command result;
  and
- strict argument validation, canonical UUID parsing, no shell string execution,
  and no command that exists only for desktop.

Prove parser behavior, secret redaction, bounded output, and equal durable calls
from both renderers. Do not implement the concrete operations in this packet.

### F2 — Linux service layout and explicit paths

Own:

- new `src/rcp/server_ops/layout.py`;
- server-install configuration models;
- a systemd unit template under a new source-controlled server asset directory;
  and
- `tests/test_server_layout.py`.

Deliver one validated layout for the managed Git checkout, per-commit release
directories and environments, root-owned `current` pointer, private service
home, `RCP_DATA_DIR`, central checkouts, credentials, backup config,
runtime/socket path, logs, and installed CLI wrapper. Paths are absolute,
non-overlapping, and recorded by install; Linux operation never relies on the
macOS `default_data_dir()` fallback.

Use conservative ownership/modes. Credentials may not be below a backup source
or project write root. Runtime paths must be recreated safely after reboot.

### F3 — Idempotent source-service installation

Own:

- new `src/rcp/server_ops/install.py`;
- the F2 systemd asset;
- source-install operator documentation; and
- `tests/test_server_install.py` plus a live disposable Linux drive.

Deliver an explicit root/operator installation that:

1. documents the one fresh-clone bootstrap needed before the CLI exists:
   a normal operator runs `npm ci`, Web build, then `uv sync` after system
   prerequisites, followed by the absolute bootstrap `.venv/bin/rcp server
   install` path under `sudo`;
2. validates Git, `uv`, Node/npm, systemd, SSH, and supported `age`;
3. creates or validates the dedicated `rcp` account and layout;
4. creates a separate managed Git checkout plus a clean release directory for
   the exact commit rather than adopting the bootstrap checkout, records the
   configured GitHub origin and `main` branch, and proves `rcp` can fetch it
   without borrowing the invoking operator's credential;
5. for a private source origin, guides setup of a distinct read-only source
   deploy key; for a public origin, stores no source credential;
6. runs managed Git/npm/Web/uv work as `rcp`, revalidates/rebuilds the managed
   checkout in that same order before service activation, and uses `uv sync
   --frozen` once the lock is established;
7. installs a stable CLI wrapper and non-reloading systemd service;
8. leaves team-space initialization on the existing interactive
   `rcp space init --team --name ...` path so the bootstrap code never reaches a
   service log; and
9. starts and reads back health without widening the loopback bind.

Root performs only the OS changes needed for the account, directories, wrapper,
and systemd. Re-running install must converge or refuse an exact incompatible
state. It must not replace a data directory, source checkout, or account it
cannot prove it owns. Removing the bootstrap checkout after success must not
affect doctor, update, service restart, or team-space operation.

### F4 — Private machine-local control socket

Own:

- new `src/rcp/server_ops/control.py`;
- `src/rcp/api/app.py` lifespan/composition wiring;
- `src/rcp/server_runtime.py` metadata needed to locate the socket; and
- `tests/test_server_control.py`.

Deliver a versioned Unix-domain request/response protocol available only for a
team service installation. The socket is owned by `rcp`, mode-restricted,
size-bounded, validates peer/request shape, exposes only named server operations,
and is removed only by its owning process. Commands that mutate durable state
call the existing concrete owners in-process; they do not create a second
`AppStore` or a generic admin HTTP router.

Prove a second process cannot open SQLite, an unauthorized OS account cannot use
the socket, malformed/oversized requests fail, restart recovers the socket, and
root/`rcp` authority does not become an RCP member identity.

### F5 — Commit identity and `server doctor`

Own:

- new `src/rcp/server_ops/doctor.py`;
- health/server metadata projections in `src/rcp/api/health.py` and
  `src/rcp/server_runtime.py`;
- F1 dispatch; and
- `tests/test_server_doctor.py`.

Report source/release roots, configured origin/branch, managed-main HEAD,
upstream HEAD, candidate/current/running commits, service/reload state,
space/process/data identities, ownership and mode problems, control-socket
health, Web bundle build identity, dependency readiness, backup summary, and
provider/machine readiness without revealing secrets.

Distinguish “checkout updated but old process still running” from corruption.
Doctor is read-only and works interactively and as one structured document.

### F6 — CLI-managed `origin/main` update

Own:

- new `src/rcp/server_ops/update.py`;
- F1/F4 dispatch;
- system-service restart/readback seam; and
- `tests/test_server_update.py` plus a live local-origin Git fixture.

Implement the exact settled order:

1. require the authorized `sudo rcp server update` operator entrypoint, retain a
   narrow root coordinator, and run all remaining source/build steps as `rcp`;
2. acquire one update admission lock and inspect active maintenance;
3. require configured origin, checked-out `main`, clean tree, and fast-forward
   relationship, and prove fetch uses only the configured source identity;
4. fetch and show current/target commits; prompt unless explicitly confirmed;
5. fast-forward the managed `main` checkout to `origin/main`;
6. create or validate one clean release directory for the exact target commit;
7. run `npm ci`, clean Web build, and `uv sync --frozen` inside that release as
   `rcp`, without changing the current release or environment;
8. run migration/startup readiness without touching the live data directory from
   a second process;
9. use the narrow root coordinator to atomically switch `current` and ask
   systemd for a graceful restart; and
10. read back health until the running commit equals the target.

If fetch/build/sync/readiness fails, report the exact managed-main,
candidate/current/running commits and leave the old release serving unchanged.
If startup fails after switching, report loudly and do not roll back silently.
Never reset, force-pull, auto-stash, choose another branch, call a packaged
updater, or give `rcp` general sudo/systemd control.

## Provisioning packets

### P1 — Durable provisioning records and state machine

Own:

- `src/rcp/storage/models.py`;
- schema/migration additions in `src/rcp/storage/base.py`;
- new `src/rcp/storage/provisioning.py` mixed into `AppStore`; and
- `tests/test_project_provisioning_storage.py`.

Model one request id, kind (`create_team_project` or incoming transfer), target
space, human authorizer, repository sources, intended/resolved paths, Git and
provider checks, timestamps, retryable diagnostic, final-review digest, and
explicit cancellation disposition.

Persist the six backend display states exactly. State transitions are guarded in
one transaction and idempotent by step receipt. A CLI reconnect resumes; it does
not create a second request. A request id grants no machine authority. Do not put
private keys, provider tokens, SSH material, or arbitrary command text in the
record.

### P2 — Provisioning API and backend projection

Own:

- new `src/rcp/api/project_provisioning.py`;
- composition in `src/rcp/api/app.py` and existing project-setup dependencies;
- response models in `web/src/types.ts` and calls in `web/src/api.ts`; and
- `tests/test_project_provisioning_api.py` plus response-shape Web tests.

Deliver member-authorized create/read/cancel/final-review routes. They create or
change only durable product requests; they do not perform machine work. The
projection owns status label, exact next action, `can_run_setup`, `can_review`,
`can_cancel`, resolved paths, readiness summaries, and safe operator argv tokens.
Seal any complete lifecycle vocabulary in the Web response type so the browser
cannot branch on strings.

### P3 — Repository-scoped deploy-key lifecycle

Own:

- new `src/rcp/server_ops/git_credentials.py`;
- its secret-path integration with F2;
- `tests/test_git_credentials.py`; and
- a disposable GitHub repository live drive.

Generate one key per target GitHub repository on the account that owns its local
or remote checkout, show only the public key and fingerprint, derive the
protected private-key path without persisting its bytes, and give exact GitHub
instructions including **Allow write access**. Verify both the execution host
and GitHub host keys explicitly; do not disable either check.

Prove write using a request-scoped temporary ref that points to an existing
commit, read it back, and remove it. A failed cleanup remains **operator action
needed**. Empty repositories need an explicit separate initialization path; do
not invent a hidden commit. Never place a private key in SQLite, the manifest,
logs, structured output, prompts, or backups.

### P4 — Central checkout preparation

Own:

- new `src/rcp/server_ops/project_checkout.py`;
- exact Git subprocess helpers local to that module;
- `tests/test_project_checkout.py`; and
- P1 step receipts.

Resolve a project directory only under the configured central root on the
selected local or SSH machine, refuse symlinks/special files/unowned existing
directories, clone or verify the exact Git remote, bind the P3 key without
changing global SSH config, and prove the state and truth-scope repository paths.
Reuse the existing SSH transport construction rather than creating a local-only
provisioning path. All subprocesses use argv, bounded output, and timeouts.

Cancellation never recursively deletes an unproven directory. Record one
explicit reuse, operator-cleanup, or safe-created-empty disposition.

### P5 — Provider-account configuration and readiness

Own:

- new `src/rcp/server_ops/provider_setup.py`;
- existing `src/rcp/providers.py`, `src/rcp/agents/launcher.py`, and
  `src/rcp/transport/ssh.py` only where the shared probe needs extension;
- `tests/test_server_provider_setup.py`; and
- local plus reachable-SSH live probes.

Guide the operator through provider-native login on the actual local or remote
execution account. Use the existing provider profile/runtime implementation to
verify executable, version, authentication, model/runtime, and exact account.
Persist only nonsecret readiness and config references.

Codex exec, Codex app-server, and Claude retain their own provider specs behind
one call abstraction. Local and SSH use the same selected profile contract. A
failed account never falls back to a member laptop, different account, or other
runtime except the already specified pre-prompt Codex runtime fallback on the
same machine.

### P6 — Provisioning execution and final creation

Own:

- new `src/rcp/server_ops/project_provision.py`;
- project setup service seams in `src/rcp/setup.py` and `src/rcp/projects.py`;
- F4 command registration; and
- `tests/test_team_project_provisioning.py`.

Run P3–P5 as resumable named steps and publish every result through P1. A zero
process exit cannot skip durable status readback. Final human confirmation
revalidates the final-review digest, paths, Git/provider readiness, current
membership, and unchanged request before using the existing setup/transition
owners to create and register the project.

Crash at every boundary in a parameterized test. Before confirmation there is no
canonical project. After confirmation repeated calls return the one same project
and request.

## Desktop packets

### D1 — Saved connection metadata and macOS credential storage

Own:

- new `web/src-tauri/src/team_connections.rs`;
- target-specific credential dependency/config in `web/src-tauri/Cargo.toml`;
- Tauri commands/permissions; and
- Rust tests for serialization and credential references.

Store nonsecret connection id, display name, SSH target, remote loopback port,
expected `space_id`, stable assigned local origin, minimum shell protocol, and
last-known cards in the app config directory. Store the permanent token in macOS
Keychain under a stable service/account key. The one controlled secret input may
cross the Tauri IPC needed to store or enroll it, then must be cleared. No
localStorage, sessionStorage, retained Web state, Rust log, command result, URL,
or connection file contains the token.

Removing metadata and removing a credential are explicit, reconcilable actions.
Do not claim Linux desktop credential support in this slice.

### D2 — Distinct-loopback-origin and cookie proof

Own a focused Tauri/WKWebView spike and tests before building navigation:

- candidate loopback alias/address allocation in `team_connections.rs`;
- `web/src-tauri/src/navigation.rs`; and
- a small live two-server harness under desktop tests/scripts.

Prove two simultaneous tunnel origins have different cookie hosts, each server's
`__Host-` session remains isolated, Secure-cookie behavior works in the real
WKWebView, the origin is stable across restart, and arbitrary loopback origins
remain rejected.

Do not proceed by assigning two ports on `127.0.0.1`; cookies ignore ports. If
neither verified loopback aliases nor loopback addresses work with WKWebView's
Secure-cookie rules, stop this packet with evidence and request a design decision
instead of weakening session security.

### D3 — SSH tunnel lifecycle

Own:

- new `web/src-tauri/src/team_tunnel.rs`;
- lifecycle integration in `web/src-tauri/src/lib.rs` and `backend.rs`;
- Tauri commands/permissions; and
- Rust unit plus live SSH tests.

Launch system `ssh` with argv, configured host alias, explicit local bind, remote
`127.0.0.1:8421` target, exit-on-forward-failure, bounded readiness, and owned
child lifecycle. Reuse one healthy tunnel per connection, reconnect with backoff,
and stop only desktop-owned tunnels on Quit. Never kill a remote RCP service or
accept a tunnel that resolves to an unsaved origin.

### D4 — Health, token exchange, and WebView navigation

Own:

- Tauri connection commands in `web/src-tauri/src/commands.rs`;
- `navigation.rs`, `windows.rs`, and backend state separation;
- `web/src/desktopRuntime.ts`; and
- live desktop tests.

Through the tunnel, verify health, expected `space_id`, team kind, server/running
protocol, and minimum shell version. Support one native enrollment call for a
bootstrap/invitation code and one storage path for an existing permanent token;
capture any newly issued token directly into Keychain and clear the input. Then
establish the server-side HTTP-only session in the real WebView cookie store
before navigation, without logging or otherwise persisting the permanent token.
A mismatch blocks mutations and requires explicit reconnect.

Keep the owned personal backend running and distinguish it from the currently
displayed team origin. Return-to-index navigates home. Quit behavior continues to
stop only the local backend the shell owns.

### D5 — Local multi-space project index

Own:

- a new focused Web module/component for team connection groups;
- `web/src/App.tsx`, `LandingIdentityMenu.tsx`, and `types.ts` integration;
- bounded cached-card storage owned through D1; and
- Web tests for grouping and unavailable state.

Replace the current “not implemented” seam with **Add team space**, saved space
groups, reachability, pending invitations, and team project cards. The Add flow
collects SSH target plus bootstrap/invitation code and name for a new member, or
an existing permanent token, and delegates all secret handling to D4. Personal
space stays first. Team cards navigate through D4 and never submit a team request
to the local backend. An unavailable group is dimmed with last-known cards and
one reconnect action; it does not block personal work.

### D6 — Fixed operator CLI bridge

Own:

- new `web/src-tauri/src/server_commands.rs`;
- commands/permissions and `desktopRuntime.ts` bindings;
- Rust command-construction tests; and
- live direct-`rcp` plus named-operator SSH drives.

Probe only the configured direct `rcp` command or fixed `sudo -n -u rcp -H`
form. Invoke only the installed `rcp server project provision <validated-uuid>
--machine-readable` argv. Do not execute server-returned shell text. Stream
bounded structured events for display, then require backend request readback.

If SSH or `sudo` needs interaction, produce the exact quoted Terminal argv and
open Terminal only after a human action. Never collect a password or private key.

### D7 — Provisioning request UI

Own:

- new `web/src/components/ProjectProvisioning.tsx` or equivalent focused view;
- `web/src/views/ProjectSetup.tsx`, Project Settings transfer entry, and
  `web/src/App.tsx` routing;
- P2 API/type integration; and
- browser plus desktop tests.

Render the backend's six statuses, exact diagnostic/next action, resolved paths,
Git write and provider readiness, final-review digest, and human authority. Show
**Run setup now** only from the D6 probe; always show **Copy server command**.
CLI events are transient progress, never the state machine.

Use one primary action and real error text. Do not add muted helper/commentary
lines beneath primary labels. Final creation/transfer requires an explicit human
review action.

## Operations packets

### O1 — Versioned backup manifest and capture plan

Own:

- new `src/rcp/server_ops/backup_models.py`;
- project/head inventory helpers using current catalog/workspace APIs; and
- `tests/test_backup_manifest.py`.

Define a strict versioned archive manifest with space identity, RCP source
commit/schema, capture time, SQLite snapshot hash, encryption recipient
fingerprint, and per-project project/home ids, locator, recorded canonical head,
captured files/hashes, or unavailable reason/time. Materialized outputs are
explicitly forbidden.

The plan is read-only and does not pause dispatch. It must distinguish a project
captured through head N from a project merely present in SQLite.

### O2 — Online SQLite and canonical capture

Own:

- new `src/rcp/server_ops/backup_capture.py`;
- F4 in-process snapshot command;
- exact canonical export helpers beside their existing `StateWorkspace` owner;
  and
- `tests/test_backup_capture.py` including concurrent writers and remote
  unavailable hosts.

Use SQLite's online backup API in the lock-owning process. For each project,
record one accepted head and copy only the manifest/Patch content required to
replay through it. A later append is absent, never half-copied. Remote failure
marks that project uncaptured while preserving other captures.

Never walk whole repository roots or include source, credentials, `.git`,
materialization, scratch, caches, or arbitrary symlink targets.

### O3 — `age` encryption, scheduling, retention, and status

Own:

- new `src/rcp/server_ops/backup.py`;
- backup config and systemd timer asset;
- server-status storage/projection; and
- `tests/test_backup_encryption.py` plus a real decrypt drive.

`backup configure` interactively records an explicit destination, schedule,
retention, and `age` public recipient. It never accepts or stores the private
identity. `backup run` streams a deterministic archive through a supported,
version-checked `age` implementation into an atomic destination filename, then
read-checks metadata and records protected/partial/failure status.

Scheduled execution uses the same command. Retention deletes only archives whose
format, destination, and ownership are proven; preview exact targets before any
manual destructive cleanup.

### O4 — Restore and replay verification

Own:

- new `src/rcp/server_ops/restore.py`;
- F1/F3 service-stop/maintenance integration;
- existing replay/project-registration APIs only through their owners; and
- `tests/test_server_restore.py` plus a fresh-host restore drill.

Require an explicit archive, off-server `age` identity supplied for this run,
fresh target data directory, and confirmation that the old authority cannot
resume. Decrypt to a protected temporary directory, verify every hash/schema,
restore SQLite and captured canonical histories, replay each captured head, mark
captured active work interrupted, and report uncaptured/reconnection needs.

Do not restore source checkouts, Git/provider/SSH credentials, or caches. Team
projects remain visibly not ready until central paths and credentials are
re-provisioned. Serve only after all mandatory validation succeeds.

### O5 — Console member removal

Own:

- new `src/rcp/server_ops/members.py`;
- member-removal transaction methods in `src/rcp/storage/spaces.py`;
- named task/episode stop owners invoked through F4; and
- `tests/test_server_member_removal.py`.

Preview the exact member, active tasks/episodes/campaigns, project memberships,
token, and sessions before confirmation. On confirmation, re-read current state,
stop that member's active authorized work, revoke tokens/end sessions, and remove
project/space membership atomically where possible. Preserve completed external
effects, repository writes, canonical history, and attribution.

Do not reuse self-service token revocation as fake removal and do not invent a
member administrator rank.

### O6 — Read-only Server Settings projection

Own:

- a narrow read-only API projection;
- Server Settings Web component and types; and
- browser tests.

Show service/running/upstream commits, update readiness, last backup and failure,
protected/uncaptured projects, restore drill age, provider/machine readiness, and
operator command names. Expose no HTTP mutation for update, backup, restore,
credential setup, provisioning execution, or member removal. The D6 desktop
bridge remains a native SSH action, not an admin API.

## Transfer packets

### T1 — Append-only canonical home-transfer schema and replay

Own:

- `src/rcp/core/models.py`;
- `src/rcp/core/validation/patch.py` and transition models;
- `src/rcp/history/manager.py` and delta rendering;
- persisted Patch compatibility/replay tests; and
- a dedicated transfer-transition test module.

Do not append a second `ProjectIdentity`: current replay correctly calls two
different nameplates a conflict. Keep the initial `project_id` nameplate
immutable and add one system-produced, human-authorized ordered home-transfer
record containing project id, previous home, new home, and source human
attribution. Replay reduces accepted transfers in order and halts if project id
or previous home does not match the current derived home.

Agents cannot author this record. One synchronous backend transition appends it
or nothing. Historical replay never consults current membership. Existing old
identity Patches remain byte-compatible.

### T2 — Linked source/target transfer requests

Own:

- P1 transfer-kind records;
- source and target transfer API endpoints in the P2 module;
- a bounded hashed canonical-transfer bundle model; and
- `tests/test_project_transfer_requests.py`.

The personal source records intent and target `space_id`; the authenticated team
target records the linked incoming request and runs ordinary P3–P5 preparation.
The desktop coordinates two authenticated spaces but grants neither backend the
other's credential. Export one versioned, checksummed archive containing
canonical state, all finished human-visible records, and kept artifact bytes; no
provider session, scratch, key, or machine configuration enters the bundle.

Every cross-space step has an idempotent receipt and digest. A stale or mismatched
space/project/request fails closed.

### T3 — Source fence, canonical export, and target activation

Own:

- transfer service code beside `src/rcp/projects.py`/`setup.py` concrete owners;
- T1 transition invocation;
- target `StateWorkspace` import/publication; and
- crash-boundary tests.

At final human confirmation:

1. recheck both memberships, request digests, target readiness, and source head;
2. fence new source admission and settle already-authorized work;
3. append T1's home transfer to the source canonical history;
4. export that exact accepted history/head into the prepared central checkout;
5. replay under the target `space_id` and register/seat the target member; and
6. retire the source catalog row only after target activation readback.

Crash after step 3 may leave the project unavailable and visibly repairable.
Never restore source write admission as a fallback and never activate target
before the source home transfer commits.

### T4 — Sanitized project archive export/import

Own one versioned/checksummed archive codec/service plus the precise storage and
file owners for canonical history, every finished human-visible record group,
and kept artifacts. Map project id, user attribution, attachment/artifact files,
timestamps, attempt lineage, and foreign keys deliberately. Preserve immutable
ids when globally safe; use a recorded mapping where space-local ids can collide.

Strip provider-native sessions, reusable stages, host/root bindings, live
continuations, scratch/cache pointers, credentials, and machine config. Validate
the entire archive before mutation. Stage files, insert all selected rows in one
SQLite transaction, publish through existing atomic owners, record idempotent
receipts, and permit activation only after database/file readback. Add negative
tests enumerating every excluded table/path so future schema additions fail
visibly instead of leaking into transfer.

### T5 — Transfer UI and recovery drive

Own Project Settings entry, D7 transfer presentation, local/team navigation, and
browser/desktop tests. Show source and target absolute paths, what stays owned by
the person, central ownership, active work to settle, execution settings to
re-establish, and the settled archive contents/exclusions. No confirmation before
target **ready for review**.

Drive interruption after every T3 boundary, reload both spaces, resume the same
request, and prove there is never more than one writable home. Team-to-personal
and team-to-team remain absent.

## Closure packets

### V1 — Genuine one-lab live drill

Use a fresh Linux server/VM, two distinct human desktop identities, a disposable
GitHub repository, one local provider, one reachable SSH provider target where
available, an off-server `age` recovery identity, and a fresh restore host/data
directory.

Drive, record, and retain nonsecret evidence for:

1. source clone, `rcp server install`, team init, and non-reloading service;
2. Z and Alice enrollment, saved desktop connections, distinct tunnel origins,
   cookie isolation, and multi-space switching;
3. new team project request, deploy-key write setup, central checkout, provider
   readiness, final review, and first task;
4. both members collaborating with correct attribution and project permissions;
5. local and SSH provider calls using the configured accounts with no laptop
   fallback;
6. `rcp server update` pulling GitHub `main`, rebuilding, restarting, and reading
   back the target commit;
7. scheduled/manual backup while a task and canonical write run, including a
   deliberately unreachable project;
8. decryption and restore on the fresh target, interrupted active work, replayed
   heads, and explicit old-authority exclusion;
9. console member removal with preserved history; and
10. personal-to-team transfer and crash recovery after T1–T5 land.

Tests, builds, or a browser against the local personal backend do not substitute
for this drive. Inspect server logs, systemd state, process/file owners and modes,
network/console errors, Git refs, Keychain/connection stores, and both source and
restored RCP projections.

### V2 — Baselines, docs, and handoff closure

Run at minimum:

```bash
npm --prefix web ci
npm --prefix web run build
uv sync
uv run pytest
uv run ruff check src tests
npm --prefix web test
cargo test --manifest-path web/src-tauri/Cargo.toml
uv run pre-commit run --all-files
```

Also run every new untracked path directly because `pre-commit --all-files` sees
tracked files only. Run remote/live tests only against disposable data or a copy
of real app data; never write to a researcher's real data directory.

Update current specs and acceptance evidence/status as each promise lands. Remove
the current unimplemented UI seam when D5 lands. When all closure conditions are
met, archive this handoff in the same commit and change
[`docs/handoffs/README.md`](README.md) back to no active handoffs.

## Exact closure condition

Close this handoff only when all of the following are true:

- S95, S102, S103, S104, S105, and S128 are implemented with current evidence;
- S98 is implemented with current evidence;
- a fresh source checkout can install and update the Linux service entirely
  through the documented CLI/bootstrap path;
- two source-built desktop members can use personal and team spaces without
  session collision or local team fallback;
- a team project can be prepared, reviewed, created, executed locally/remotely,
  backed up during work, restored, and transferred;
- machine-only operations are absent from member HTTP authority;
- every secret and account boundary above has a negative verification; and
- the genuine live lab drill passes and is documented without credentials.

## Suggested skills for pickup

- Complete the current `grilling` pass before dispatch so every packet boundary
  is understood; do not reopen the settled transfer archive opportunistically.
- Use `computer-use:computer-use` for the real source-built desktop drives in
  D2, D4, D6, D7, T5, and V1; browser tests cannot prove native SSH, Keychain,
  cookie-store, or navigation behavior.
- Use `codex-security:security-diff-scan` after the credential/control-socket,
  SSH bridge, backup, and restore packets, scoped to their actual diffs.
