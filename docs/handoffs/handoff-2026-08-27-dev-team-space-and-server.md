# Dev team space and source server completion handoff

Date: 2026-08-27
Status: active; design, grilling, and the final cross-document fact-check are
complete, while implementation remains pending. The packet plan is ready to
dispatch. G0 can begin immediately to restore the current `main` CI baseline;
G1 follows G0, and no external repository decision blocks the remaining
packets.

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
- backup, restore, update, provider readiness, project provisioning, and member
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
  [S105](../acceptance/S105-move-between-spaces-in-one-window.md),
  [S122](../acceptance/S122-project-invitations.md), and
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

The repository's current `AGENTS.md` still prescribes direct work on `main`, and
CI currently reports on PRs and post-push `main` but has neither an
old-data-to-candidate upgrade gate nor GitHub branch protection. Current `main`
also has a red baseline described in G0. Repair G0 on a short branch and merge it
through ordinary CI and explicit human review; then G1 completes the private
development workflow transition before dependent implementation packets are
dispatched.

### Resolved repository workflow boundary

A read-only GitHub fact-check on 2026-08-28 confirmed that `Zhi0467/RCP` is
private and its current plan rejects the branch-protection API with HTTP 403,
stating that private-repository protection requires a plan upgrade or a public
repository. The human chose not to change the repository's plan or visibility
during the private one-lab development phase.

G1 therefore establishes short-lived branches, pull requests, green named CI
checks, and explicit human merge as project policy and agent convention. GitHub
does not yet technically reject a direct push, unchecked merge, or stale merge;
the documentation and verification receipt must say so plainly rather than
claiming enforcement that does not exist.

Before RCP is shared publicly or with external users, make the repository public
and enable real `main` branch protection. Require pull requests and the named
build, test, and upgrade-compatibility checks, reject direct pushes and failed or
missing checks, and record a live enforcement proof. That public-sharing gate is
outside this one-lab team-server slice and does not block its implementation.

### Final planning-audit evidence

The 2026-08-28 final audit re-read the current design/spec/acceptance/decision
set and the complete handoff against the live tree. It reconfirmed the current
CLI/UI/data-directory seams above and reran the 93 focused foundation tests and
the eight documentation tests. It also found the current `c0909b6` baseline is
not green: the complete backend suite has two deterministic failures, and
`pre-commit --all-files` reformats seven tracked source/test files. G0 records
those exact repairs instead of letting the first implementation worker inherit
an unexplained red tree.

After adding G0, the audit checked that all 66 packet headings have exactly one
dependency-table entry with no duplicate id, missing/unknown predecessor, or
cycle. All 64 assignment packets have an explicit concrete `Own:` block; V1 and
V2 are deliberately integrator closure drives rather than worker assignments.
The audit also verified that every later owner of a not-yet-created shared file
depends on its creator; it added the missing F6a-to-O4a and O4a-to-T2c edges.
Repeated existing paths remain covered by the shared-file scheduling mutex below. The
provider-auth boundary, transfer/restore artifact decisions,
team-deletion boundary, restore journal, and shared-file scheduling mutexes are
explicit rather than left to worker interpretation. G0 is dispatchable now. The
repository workflow decision is settled, so no unresolved product or repository
decision blocks G1 or the feature lanes. Q10 and the later public branch-
protection gate are deliberately future work and do not block this plan.

## What remains

Everything after the existing auth/membership foundation remains implementation
work:

1. source-server installation, service ownership, health, and update;
2. private machine-local CLI-to-server control;
3. durable project-provisioning state and API projections;
4. central Git checkout and write-deploy-key setup;
5. local/remote provider readiness against authentication already present on
   each execution account;
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
- Supported server matrix: Ubuntu 22.04 LTS and Ubuntu 24.04 LTS on x86-64 with
  systemd. Other distributions and architectures are explicitly unverified.
- Server builds pin Node.js 24 and Python 3.12 through `uv`, with Git, OpenSSH,
  and a supported `age` CLI as prerequisites. The operator guide gives tested
  commands for both Ubuntu releases; `rcp server install` validates but does not
  modify apt repositories or install general OS software.
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
  release directory, `npm --prefix web ci`, `npm --prefix web run build`, `uv sync --frozen`,
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
  running release. Candidate rehearsal uses a consistent copy of actual server
  state while the old release remains online.
- Final cutover briefly closes mutation and machine-operation admission, waits
  for in-flight provider turns, mutations, and server-operation steps to reach a
  durable boundary, and takes a coherent local rollback checkpoint.
  Durable watchers do not have to finish. The candidate starts with normal work
  still closed and must pass commit, startup, ownership, replay/recovery, and
  representative API readback before service reopens.
- A failed post-switch verification automatically restores the checkpoint and
  previous release, verifies the restored service, and reports both commits in
  CLI output, server status, and a durable receipt. Never reset changes,
  force-pull, roll back silently, or switch to a package. This update checkpoint
  is not the encrypted off-server backup.
- Source-built service operation is stable and non-reloading. `--reload` remains
  a developer command, not the team service.

### Delivery workflow

- GitHub `origin/main` is the only server update channel and every commit on it
  must be deployable.
- All development uses short-lived feature/WIP branches and pull requests. Draft
  branches may be incomplete; the server never reads them.
- During private one-lab development, direct pushes to `main` and merges without
  green build, test, and upgrade-compatibility checks are forbidden by project
  policy, but GitHub does not technically reject them.
- Passing CI makes a PR eligible, but merge is an explicit human action. No
  second reviewer account is required. Agents may prepare a PR but never merge
  without direct human instruction.
- From the first team-server-capable commit onward, every earlier server-era
  persistence boundary remains directly upgradeable. CI retains one immutable,
  sanitized fixture bundle per distinct schema or migration-semantics boundary;
  fixtures do not expire merely because they are old.
- Local Web and desktop development may run any branch. Emergency fixes use the
  same PR gate.
- There is no permanent `dev` branch.
- Before public or external sharing, make the repository public and enable real
  branch protection that requires the named jobs and rejects direct pushes and
  failed or missing checks. This later gate is not part of the one-lab closure
  condition.

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
- The existing member-facing **Delete project** path remains available for a
  personal project only. A team project card publishes deletion unavailable and
  the API rechecks that decision: erasing its RCP rows while leaving its managed
  checkout and deploy key would orphan machine authority. Full team-project
  deprovisioning is outside this slice and must eventually be an operator-owned
  flow that names GitHub-key revocation and checkout disposition.
- RCP never asks for or stores a member's personal GitHub token.
- Remote execution transport uses the ordinary OpenSSH configuration already
  present for the server's `rcp` account. RCP checks the exact configured route
  but does not import a member's SSH key, collect one in the app, or silently
  choose another login.
- A local provider runs under `rcp` and uses whatever provider-native
  authentication is already present for that account. A remote provider runs
  under the exact configured SSH account and uses whatever authentication is
  already present there. The remote account need not be named `rcp`.
- RCP never logs into a provider, stores or refreshes its credentials, switches
  provider identities, or creates alternate provider homes. An operator runs
  provider-native login directly as the execution account; RCP only checks
  readiness afterward and reports the exact missing provider-native action.
- No provider call falls back to a member laptop, personal checkout, personal
  login, or different SSH account.

### Product authority versus machine authority

- RCP has equal members and no administrator product role.
- A member token cannot install, update, restore, configure machine credentials,
  provision a checkout, or remove another member.
- Those operations live under `rcp server ...` and require OS authority.
- A running-server CLI command never opens SQLite beside the lock owner. It uses
  a private Unix-domain control socket owned by `rcp`.
- `install`, `backup configure`, `restore`, and `update` are root-coordinator
  entrypoints because they change accounts, `/etc`, systemd, or stopped-service
  state; each drops to `rcp` for ordinary source/data work. `doctor`,
  `provider check`, `project provision`, `project transfer-import`, `backup run`,
  and `member remove` run as `rcp`, reached either directly or through the
  operator's narrow sudo route.
- The CLI has one concrete implementation with interactive output and bounded
  machine-readable progress. The desktop consumes the structured form; it does
  not get a second implementation.
- Do not add an application CLI for graph, chat, task, episode, or ordinary
  membership actions.

### UI, desktop, and CLI coordination

- A human starts **Create team project** in a team backend's Web UI or desktop.
  **Move to team space** is a source-built desktop flow because it coordinates
  two authenticated backends and the native archive relay.
- The backend persists the request before machine work and owns these displayed
  states: **waiting for server setup**, **setup in progress**, **operator action
  needed**, **ready for review**, **completed**, and **cancelled**.
- The Web UI renders backend decisions. It never infers readiness from Git files,
  subprocess output, or a zero CLI exit code.
- Machine preparation alone never creates or re-homes a canonical project. Final
  explicit human review performs that authority action. New-project creation
  records one target-space confirmation. Personal-to-team transfer records two
  independent confirmations behind one desktop review action: the authenticated
  team member admits the incoming project first, then the authenticated personal
  owner releases it. Each backend records its own actor and neither assumes that
  user ids match across spaces.
- A browser may create/review a single-space team-project request and copy its
  server command, but it cannot invoke server operations or coordinate a
  personal-to-team transfer.
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
- The first destination is one operator-chosen writable filesystem directory,
  local or mounted. RCP does not implement an upload/storage transport, infer
  whether the bytes are physically off-server, or warn about that topology.
- Destination, `age` public recipient, schedule, and retention live in one
  strict versioned installed-server config file, not SQLite. The file is
  root-owned, readable by `rcp`, contains no private recovery identity, and is
  atomically changed only by the CLI. The timer is rendered from the same
  resolved schedule rather than carrying a second editable value.
- `backup configure` proposes a daily 02:00 server-local run and the newest 30
  integrity-readback archives, while preserving the newest complete archive if
  it falls outside those 30. The operator must explicitly confirm or edit the
  values before the timer is enabled.
- SQLite uses a consistent online snapshot. Project-file capture separately
  records a head and includes the append-only main/branch state needed to replay
  through that head, canonical RCP chat JSONL, the optional human-authored Paper
  introduction, opaque regular files under `.research/facts/`, and only the
  repository artifacts or legacy result views referenced as kept by captured
  SQLite metadata.
- Those non-SQLite files are copied through their concrete chat, Paper, facts,
  and workspace owners. A new unclassified durable project root makes capture
  visibly partial until its lifecycle is decided; it is never silently dropped.
  Materialized outputs, source repositories, temporary input
  attachments, scratch, caches, Git keys, SSH keys, and provider
  authentication/configuration stores are excluded.
- Project-owned provider histories imported by transfer are included because
  they may be the team's only durable Seed/Refresh source. Live provider homes,
  authentication/configuration, and newly produced native logs remain excluded.
- Every captured team project also carries one nonsecret recovery descriptor,
  bound to the captured provisioning state: repository sources and aliases,
  resolved central paths and machine/SSH-route references, the canonical
  manifest configuration, and the old deploy-key labels/fingerprints. A project
  without enough verified metadata to reconstruct its checkout set is
  uncaptured, not a supposedly restorable project.
- Archives are encrypted to an `age` public recipient on the server. The private
  recovery identity remains off-server.
- The first backup format supports the upstream `age` CLI from `1.0.0` through
  the 1.x line and accepts only native X25519 `age1...` recipients. Plugin, SSH,
  passphrase, and post-quantum recipients are outside this compatibility target.
- One unreachable project makes the archive partial and visibly unprotected; it
  does not erase successful captures or get called complete.
- Restore is console-only, validates/decrypts/replays before serving, preserves
  `space_id`, marks captured active work interrupted, and requires the operator
  to affirm the old copy cannot resume.
- Before its first mutation, restore fsyncs a request journal outside the target
  data/checkouts. A crash keeps systemd stopped; re-entry resumes the same digest
  and phase idempotently, and only final service/project readback completes the
  journal.
- On a fresh installation, restore first uses the saved recovery descriptor and
  the existing P3/P4 Git helpers to create fresh repository-scoped keys and
  reconstruct every captured central checkout from Git. It never writes a bare
  `.research/` tree into an empty future checkout path. Only after the checkout
  exists does it publish the archived canonical and human file groups through
  their concrete owners. Existing retained history must be byte-identical and
  contain no archive-external canonical commits; any conflict stops restore.
- The replacement service starts only after every *captured* project has its
  checkout, canonical bytes, and replay verified. A project explicitly marked
  uncaptured remains visible but unavailable. Provider-native authentication is
  not a restore prerequisite: missing auth leaves new execution visibly
  unavailable until an operator logs in with the provider and RCP rechecks it;
  restored history remains readable meanwhile.
- Because provider homes, run stages, and native conversations are excluded,
  every pre-restore task becomes history-only, `writing_sessions` and
  `chat_session_contexts` are cleared, and old native-session ids are not
  projected as executable continuations. RCP chat text and task/Paper answers
  remain readable; continuing that chat starts a fresh checked provider session.
- Every nonterminal pre-restore episode, report attempt, watcher, recovery, and
  child admission is also stopped or terminally detached before normal startup.
  The replacement must prove startup schedules no old provider turn, watcher
  check/delivery, report retry, or automatic graph change.

### Transfer

- Only personal-to-team product transfer is in scope.
- One desktop review records team-side admission and personal-side release as
  separate space-scoped human receipts; the service-account import command
  cannot supply either one.
- The linked requests precommit to two independent random one-time proofs. The
  target can verify the source-release proof only after the source fence commits;
  the source can verify the target-activation proof only after target activation.
  Neither backend accepts a member-supplied or desktop-relayed serialized
  receipt, archive path, request id, or successful machine command as proof of
  the other boundary. The raw proof released by that boundary must also verify.
  This is fail-closed protocol evidence, not a claim that RCP can defend its
  database from root or the service account that owns it.
- The target uses a separate central checkout set. `rcp` owns server-local
  checkouts and each explicit remote execution account owns its SSH checkouts;
  the personal checkouts retain their paths and owners.
- The source must be fenced before the target becomes writable. A prior target
  human-admission receipt alone creates no project. Recovery may temporarily
  leave no writable home, never two writable homes.
- Complete provider conversations positively matched to the project transfer
  travel as read-only Seed/Refresh sources. Provider credentials, native-home
  installation, resumption authority, scratch, caches, and machine configuration
  do not.
- The durable `project_id`, canonical history, home change, and attribution do
  transfer.

### Transfer archive

- One versioned, checksummed archive is the sole personal-to-team transfer
  format. It carries main and graph-branch canonical history; transformed
  canonical RCP chat transcripts; the current Paper draft and canonical
  introduction; `.research/facts/`; all finished human-visible operational
  history; complete project-matched provider conversations; and the exact bytes
  of referenced kept artifacts and legacy kept result views. Immutable branch
  metadata, Patches, and merge receipts travel; branch materializations do not.
- The existing native conversation index automatically selects conversations by
  best-effort recorded-path matching on the source machine and includes the
  original complete file for each selected conversation. Configured provider
  profiles supply the native roots; transfer adds no second provider parser or
  SSH traversal path. Rewritten, unmatched, or unreadable sources produce a
  non-blocking summary; there is no human classification step or completeness
  claim.
  `last_refresh_at` is preserved as an overlap boundary and never used to
  truncate the archive.
- RCP chat JSONL is parsed and rewritten as typed project history rather than
  copied blindly: the stable RCP chat/messages, provider/model labels, graph
  receipts, and display-only attachment metadata remain, while native provider
  session ids, execution-machine/cwd fields, and source operation bindings are
  cleared or deliberately remapped. The current Paper draft and both sides of a
  behind/unsynced conflict remain, but `writing_sessions` does not transfer:
  that table is a bounded native-session Resume index, not the durable Paper
  content. Completed Paper-coach task answers remain in terminal task history.
- Export also removes reusable stages, live continuations, temporary input
  attachment bytes, scratch/cache pointers, credentials, and machine config.
  Imported provider histories live outside the target native provider home and
  are readable only as project sources.
- Imported terminal tasks are history-only and cannot Pause, Resume, or Retry;
  imported provider files receive no execution binding. Future work starts as a
  new ordinary task through team config.
- The target validates the complete archive before mutation, imports selected
  rows in one SQLite transaction, publishes files through existing atomic
  owners, and activates only after database and file readback.
- After the source home changes, the personal backend retains the one sealed,
  mode-0600 request archive under its own app data and serves every relay retry
  from those same verified bytes. It never regenerates a different archive for
  an already bound digest. Only the matching target-activation receipt permits
  exact-file cleanup and source-row retirement; ordinary project Delete is
  unavailable while that recovery copy is needed.
- The accepted rationale is recorded in the
  [personal-to-team transfer decision](../decisions/2026-08-27-personal-to-team-transfer-archive.md).

## Explicit non-goals

Do not add any of the following to finish this handoff:

- packaged Linux RCP, Docker, Kubernetes, a hosted service, or a binary release
  channel;
- public HTTPS, VPN configuration, reverse-proxy automation, or Internet-facing
  team serving;
- multi-server authority, automatic failover, replicated SQLite, or automatic
  detection of an old restored authority; the latter remains
  [Q10](../open-questions.md#q10--should-a-client-detect-rollback-of-a-familiar-space);
- per-member or per-project Linux service accounts;
- member-laptop team execution or checkout discovery;
- team-project deprovisioning or deletion; the unsafe ordinary Delete action is
  disabled for team projects rather than leaving managed keys/checkouts orphaned;
- team-to-team transfer, team-to-personal product transfer, or fresh-identity
  fork;
- GitHub OAuth, personal access-token custody, or a general secret manager;
- automatic source merges, force-pulls, branch repair, or rollback of server
  source;
- a browser route that can run machine commands;
- user-owned agent actors or cross-episode peer mail; the open multiplayer mail
  question remains outside this server/deployment slice;
- a generic admin HTTP API, plugin registry, event bus, or second orchestration
  layer; or
- backup claims that have not survived a real restore.

## Work-packet discipline

The human preference is file/module-level work, normally about ten minutes of
Luna-max agent work or roughly one hour of human engineering. A lettered
subpacket is the assignment unit; its parent heading is only a lane. Do not hand
an agent “build the server,” “finish desktop team mode,” or a combined range of
subpackets merely because they share a heading. Assign one packet below, with
the listed files as its ownership boundary.

Workers are not alone in the tree. They must inventory first, preserve unrelated
edits, avoid reverting other packets, and adapt to already-landed dependencies.
The integrating agent retains schema/API compatibility, full diff review, live
verification, and documentation lifecycle.

Some `Own` lists deliberately repeat a narrow composition or response-shape
file. An identical owned path is a scheduling mutex even when it is not a
semantic predecessor in the dependency table: land one packet before assigning
the other, then make the later worker adapt to the landed shape. Never dispatch
two workers concurrently against the same owned file or directory region.

A packet is an assignment unit, not necessarily a merge unit. If landing one
packet alone would expose a command or timer whose concrete owner is still
absent, keep that surface disabled and unadvertised or combine the adjacent
packets in one PR while retaining their separate file ownership and checks.
`main` must remain deployable after every merge.

V1 and V2 are integrator closure drives rather than normal worker assignments;
their breadth is deliberate because they prove the assembled system after every
file-sized implementation packet has landed.

New concrete server policy may live under `src/rcp/server_ops/`. Keep command
policy in its owning module; do not build a generic manager/facade. The top-level
CLI should parse and dispatch, while install, update, Git, provider, backup,
restore, and member-removal behavior remains separately navigable.

## Dependency map

The table names required predecessors, not merely lane-level suggestions. It may
repeat a transitive safety gate where the receiving packet must re-verify that
boundary. A packet may start when every entry in its second column has landed and
its live gate is available.

| Packet | Required predecessors | Additional live gate |
|---|---|---|
| G0 | none | none |
| G1 | G0 | none |
| G2 | G1 | none |
| F1, D1 | G1 | none |
| F2 | F1 | none |
| F3a | F2, G2 | none |
| F3b | F3a | disposable Ubuntu 22.04 and 24.04 x86-64 hosts |
| F4 | F3b | installed team service |
| F5 | F4 | installed team service |
| F6a | F5, G2 | fetchable disposable Git origin |
| F6b | F6a, O2b, P2 | copied real server state |
| F6c | F6b | recovery-critical local-state fixtures |
| F6d | F6c | disposable systemd host and forced candidate failure |
| P1 | F1, G2 | none |
| P2 | P1, F5 | none |
| P3 | F2, P1 | disposable GitHub repository |
| P4 | P1, P3 | local and reachable-SSH checkout targets |
| P5 | P1, F6a | authenticated and unauthenticated local/SSH provider accounts |
| P6a | P2, P3, P4, P5, F4 | disposable GitHub repository and team service |
| P6b | P6a | prepared request plus an enrolled human reviewer |
| P6c | P6b | activated team project with central keys and checkouts |
| D2 | D1 | real WKWebView with two local test servers |
| D3 | D2 | reachable SSH server |
| D4a | D3 | live team enrollment and Keychain |
| D4b | D4a | source-built desktop and live team server |
| D5 | D4b, P2, P6c | personal plus available/unavailable team spaces |
| D6 | D4b, P6a | direct-`rcp` and named-operator SSH routes |
| D7 | D5, D6, P6b | browser and source-built desktop |
| O1 | F4, P6c | registered local and remote projects |
| O2a | O1 | concurrent SQLite writers |
| O2b | O2a | concurrent project-file writers and one unreachable host |
| O3a | F3a | writable filesystem destination |
| O3b | O2b, O3a | `age` recipient plus off-server recovery identity |
| O3c | O2a, G2 | terminal and interrupted task/session fixtures |
| O3c-ui | O3c | kept and unavailable artifact response fixtures |
| O3d-a | O3c | active task, Experiment, report, session, and enrollment-code fixtures |
| O3d-b | O3d-a | active Auto-research, watcher, recovery, and child-admission fixtures |
| O4a | O3b, O3d-b, F6a | fresh stopped-service restore host and encrypted archive |
| O4b | O4a, P4 | reconstructible local and SSH Git sources |
| O4c | O4b, O3c-ui | captured canonical, chat, Paper, facts, and kept-file fixtures |
| O4d | O4c, O5b | old-authority and member-roster confirmation |
| O5a | P6a, F6d | second enrolled member and pending invitations |
| O5b | O5a | second member with active work plus crash injection |
| O6 | F5, F6d, P5, O4d, O5b, D5, D7 | browser against live team service |
| T1 | P6b, G2, O4d | two spaces |
| T2a | T1, P1 | linked personal and team request fixtures |
| T2b | T2a, P2 | authenticated personal and team spaces |
| T2c | T2b, O4a | stopped-service restore fixture with a nonterminal transfer request |
| T3a | T2b | representative project archive inventory |
| T3a-config | T3a, P6a | source history plus reviewed target execution configuration |
| T3b | T3a | finished database record corpus |
| T3b-export | T3b, O3c | terminal source database with all runnable work settled |
| T3b-files | T3a, T3b-export, O2b | canonical human files, facts, and kept-file fixtures |
| T3c | T3a | local and reachable-SSH native provider fixtures |
| T3d | T3c, T3b-files | imported-source fixture root |
| T3d-ssh | T3d | reachable SSH execution account and imported-source fixture |
| T3e | T3d, O2b, O4d, F6c | completed backup/restore/update owners |
| T3f | T3a-config, T3b-files, T3d-ssh, T3e | fresh target data copy |
| T4a | T1, T2c, T3b-files, T3e | both spaces and a source project with finished history |
| T4b | T4a | target team service and a bounded archive fixture |
| T4c | T4b, T3f | prepared central checkout and both human receipts |
| T5a | T4a, T4c, D6 | both spaces, a saved operator route, and a bounded archive fixture |
| T5b | T5a, D7, O6 | both spaces in one source-built desktop |
| V1 | F6d, P6b, D7, O4d, O5b, O6, T5b | genuine one-lab environment |
| V2 | V1 | every required local/remote baseline environment |

G0 can start immediately. G1 starts only after G0 is green and the external
GitHub choice is settled. After G1, F1 and D1 can proceed in parallel. After F1
and G2, P1 can proceed
alongside F2 and the desktop chain. T2a and T2b are deliberately ordered by
their storage and API boundaries; T2c also waits for O4a's concrete restore
owner. T3b and T3c may start in
parallel. T3a-config may also proceed once P6a and T3a exist. T3b-export follows
T3b and O3c; T3b-files then may continue alongside T3c and T3a-config. T3d starts after both so
its `service.py` integration incorporates the already-landed canonical-chat
read seam. Do not parallelize packets
that touch the same
`storage/base.py`, `core/models.py`, `history/manager.py`, Tauri navigation, or
systemd asset regions. Sequence any packets that both touch `web/src/types.ts`,
`web/src/api.ts`, `src/rcp/projects.py`, or `src/rcp/setup.py` even when their
logical prerequisites would otherwise permit parallel work.

Also sequence packets that share `src/rcp/api/app.py`, `src/rcp/background.py`,
`src/rcp/server_ops/cli.py`, `src/rcp/server_ops/control.py`,
`src/rcp/server_ops/doctor.py`, or `src/rcp/server_ops/install.py`; those are
explicit composition seams, not invitations to concurrent editing.

Any packet that adds a durable or recovery-critical non-SQLite file root must
classify it explicitly for backup, restore, update rehearsal/checkpoint, transfer,
and safe deletion. It updates the affected concrete owners and negative tests or
proves the root is rebuildable/excluded; do not add a generic file-root registry.

Any packet that changes a SQLite schema, canonical persisted shape, migration
semantics, or startup recovery boundary also owns the corresponding G2 migration
test and immutable old/new boundary fixture update. That obligation applies even
when the packet's file list does not repeat the shared CI paths.

## Delivery-gate packets

### G0 — Restore the current `main` CI baseline

Own:

- formatting-only normalization of `src/rcp/api/tasks.py`,
  `src/rcp/runs/chat.py`, `tests/test_api.py`, `tests/test_episode_api.py`,
  `tests/test_unified_artifacts.py`, `web/src/rootRecovery.tsx`, and
  `web/tests/rootRecovery.test.mjs`;
- `src/rcp/storage/rows.py` and the focused runtime regression in
  `tests/test_background.py`; and
- the active-handoff assertion in `tests/test_agent_instructions.py`.

Start from current `main` commit `c0909b6` on a short repair branch. This is a
baseline repair, not team-server feature work. Apply the repository's configured
formatters mechanically to the seven named files without changing their
behavior. Replace the stale documentation assertion that says there can be no
active implementation handoff with an assertion that distinguishes the archived
closed backend-refactor handoff from valid indexed active work.

Repair the real runtime projection regression rather than weakening its test.
`checkpoint_agent_task_runtime` currently writes the selected runtime and its
receipt, but `_agent_task_record` removes `runtime_id` before model validation;
the read model therefore silently reports the legacy Codex exec runtime even
after app-server was selected. Remove that obsolete pre-runtime compatibility
path and prove the runtime event is still read back before the provider-session
checkpoint. Do not change runtime selection, fallback, or provider-auth policy.

Before human merge, run the complete current lint/Python/Web CI-equivalent
baseline, including `uv run pytest` and `uv run pre-commit run --all-files`, and
read the diff to prove formatter output did not hide semantic edits. G0 must land
green before G1 changes repository workflow and CI policy.

### G1 — Convention-only PR workflow transition

Own:

- the direct-main rule in `AGENTS.md` and any contributor workflow text;
- current lint/Python/Web PR triggers and stable job names in
  `.github/workflows/`; and
- a short verification receipt in this handoff or its successor.

Begin after G0 is green. Deliver the short-lived branch and PR workflow before
any dependent packet is assigned. Forbid direct pushes to `main` by project
policy, require the current lint/Python/Web jobs to be green before human merge,
and require the tested result to be current. Agents cannot push directly or
merge absent direct human instruction. Do not create a permanent `dev` branch
or let the team server read feature branches. G2 adds the
upgrade-compatibility job to the same policy before F3a makes `main`
team-server-capable.

This packet is the deliberate transition point: before it lands, preserve the
current repository instructions; after it lands, no agent works directly on
`main`. The receipt must explicitly record that GitHub still permits the
forbidden direct-push and unchecked-merge paths in this private phase; G1 proves
the documented workflow and CI reporting, not nonexistent repository-setting
enforcement. Public branch protection is a later sharing gate outside this
slice.

### G2 — Old-data upgrade CI gate

Own:

- new `tests/test_server_upgrade.py` and its focused harness;
- upgrade/startup smoke helpers beside their concrete owners;
- a stable named job in `.github/workflows/ci.yml`; and
- immutable sanitized fixture bundles under
  `tests/fixtures/server_upgrade/<boundary>/` for every server-era persistence
  boundary, beginning with the first team-server-capable commit.

Build the PR base, create representative prior data, then build the candidate,
upgrade a copy, start the complete backend with external/provider effects
disabled, and verify health, replay, startup recovery, and key projections.
Exercise every historical boundary fixture as well as the exact PR base. A
fixture contains the small SQLite database and any canonical history needed for
realistic replay/recovery, is produced while its boundary is current, and is
never regenerated by newer code. New persistence changes add a boundary fixture
before the old shape leaves `main`. Fixtures have no rolling expiry; dropping a
boundary requires a separately approved migration path. The job must pass by
project policy before human merge and test the exact combined commit eligible to
land. The later public branch-protection gate makes it GitHub-required.

The on-server actual-data rehearsal and update-local restore boundary remain in
F6a–F6d, while disaster restore remains in O4; CI evidence never substitutes for
that server-specific preflight.

## Server-foundation packets

### F1 — Server CLI command and event contract

Own:

- new `src/rcp/server_ops/__init__.py` defining the package boundary;
- `src/rcp/__main__.py`;
- new `src/rcp/server_ops/cli.py` and `src/rcp/server_ops/models.py`; and
- focused parser/serialization tests in `tests/test_main.py` or
  `tests/test_server_cli.py`.

Deliver:

- `rcp server install`, `doctor`, `provider check`, `project provision`,
  `project transfer-import`, `backup configure`, `backup run`, `restore`,
  `member remove`, and `update`;
- exact provider-check selectors:
  `provider check (--request <request-id> | --project <project-id>)`, with one
  and only one selector and no arbitrary host/account/path override;
- a versioned bounded progress record with command, phase, state, message,
  timestamp, and optional nonsecret fields;
- interactive and `--machine-readable` renderers over the same command result;
  and
- strict argument validation, canonical UUID parsing, no shell string execution,
  and no command that exists only for desktop.

`restore` accepts an absolute archive path and an off-server recovery identity
only through a protected identity file (or inherited descriptor/stdin form
owned by O4a); it never accepts raw identity text in argv, environment, or
machine-readable progress. Its destination is the installed server's displayed
and configured `RCP_DATA_DIR`, which must be fresh/empty for this first restore
contract. It is not an arbitrary second data-root selector.

Enforce the settled entry-identity matrix: root coordinator for `install`,
`backup configure`, `restore`, and `update`; service account for `doctor`,
`provider check`, `project provision`, `project transfer-import`, `backup run`,
and `member remove`. Root entry never causes provider/Git/build work to inherit
root's home. A wrong calling identity fails before durable work.

`project transfer-import <request-id>` is also a service-account command. Its
contract accepts the archive only on stdin and accepts no archive path, host,
account, or destination override. This packet owns canonical request-id parsing,
bounded input/event shapes, and the concrete-handler seam only. T4b owns the
upload lease and protected inbox; T4c owns request revalidation, import, and
activation; T5a owns the one native caller. An arbitrary file, byte stream, or
request id grants no import authority.

Prove parser behavior, secret redaction, bounded output, and equal durable calls
from both renderers. Do not implement the concrete operations in this packet.

### F2 — Linux service layout and explicit paths

Own:

- new `src/rcp/server_ops/layout.py`;
- new `src/rcp/server_ops/config.py` for strict installed-server configuration;
- new `src/rcp/server_ops/assets/rcp.service`;
- `tests/test_server_layout.py`.

Deliver one validated layout for the managed Git checkout, per-commit release
directories and environments, root-owned `current` pointer, private service
home, `RCP_DATA_DIR`, central checkouts, credentials, backup config,
runtime/socket path, logs, and installed CLI wrapper. Paths are absolute,
non-overlapping, and recorded by install; Linux operation never relies on the
macOS `default_data_dir()` fallback.

Use these accepted paths:

- `/home/rcp/rcp-server/source` for the managed `main` checkout;
- `/home/rcp/rcp-server/releases/<commit>` for clean built releases;
- `/home/rcp/rcp-server/data` for `RCP_DATA_DIR`;
- `/home/rcp/rcp-server/projects/<project-id>/repositories/<alias>` for each
  server-local central checkout;
- `/home/rcp/rcp-server/credentials` for the source key and server-local project
  keys;
- `/home/rcp/rcp-server/update-checkpoints` for cutover rollback state;
- `/home/rcp/rcp-server/restore-operations` for crash-safe restore journals and
  protected temporary candidates;
- `/etc/rcp/server.toml` for root-owned versioned machine configuration;
- `/etc/rcp/current` for the root-owned current-release pointer;
- `/run/rcp/control.sock` for the private runtime socket; and
- `/usr/local/bin/rcp`, systemd units, and journald for system integration.

Leave every provider's native state at its ordinary per-account home path
(currently `/home/rcp/.codex` and `/home/rcp/.claude`) and SSH state at
`/home/rcp/.ssh`. A later provider keeps its own native path. RCP probes provider
authentication there but never relocates or manages it. The explicit backup
destination may be outside `/home/rcp/rcp-server`.

The layout includes one strict versioned installed-server config file, owned by
root and readable by `rcp`. Machine configuration that systemd, doctor, update,
backup, or restore must read without a healthy application database belongs
there. Write it by validated atomic replacement through its concrete CLI owner;
do not turn it into a second project manifest or store private keys in it.
Mint and retain one immutable nonsecret `installation_id` there. It identifies
this machine installation and its source-fetch grant; it is not a member,
`space_id`, credential, or replacement for either identity.

`restore-operations/` is machine recovery state, not project or backup content.
Exclude it from encrypted backup, transfer, update rehearsal, and update
checkpoints. An unfinished entry blocks install/update activation and is resumed
only by O4. After O4d records the durable restore receipt and final readback, the
restore owner may remove only that exact operation's candidate and completed
journal; no command recursively cleans the root or treats an unfinished entry as
disposable.

A project checkout on an SSH machine keeps its repository key on that same
configured execution account under the absolute home-derived root
`<remote-home>/.local/share/rcp/credentials/`. P3 resolves `<remote-home>` by a
fixed shipped helper running as the configured account and verifies its uid,
ownership, and modes; it does not assume `/home/<name>`, trust a shell `$HOME`,
accept a project-manifest override, or copy the private key back to the server.

Use conservative ownership/modes. Credentials may not be below a backup source
or project write root. Runtime paths must be recreated safely after reboot.
The dedicated account has fixed home `/home/rcp`, a real `/bin/bash` login shell,
and no usable password. To preserve the optional public-key SSH route, its
Ubuntu shadow entry must use an unusable non-locking value such as the
OpenSSH-documented `*NP*`, not a leading `!` account lock that `sshd` can reject
before public-key authentication. It is still a service identity, not a shared
human login.
The installer neither enables password SSH nor edits global `sshd_config`;
direct `rcp@server` access exists only when the operator deliberately installs a
public key for that route. Otherwise operators use their named SSH accounts and
the narrow sudo command. Validate that `rcp` has no general sudo or supplemental
privileged group membership.

### F3a — Idempotent installer and service unit

Own:

- new `src/rcp/server_ops/install.py`;
- `src/rcp/server_ops/assets/rcp.service`;
- `tests/test_server_install.py`; and
- the first immutable server-era fixture under
  `tests/fixtures/server_upgrade/<first-server-boundary>/`, produced by the
  exact first installable team-server commit through G2's harness.

Deliver an explicit root/operator installation that:

1. validates x86-64, systemd, Git, `uv`, Node.js 24/npm, the `uv`-managed Python
   3.12 service runtime, SSH, and `age >=1.0.0,<2.0.0`, without changing apt
   sources or installing general system tools;
2. creates or validates the dedicated no-usable-password `rcp` account with
   exact `/home/rcp` home, `/bin/bash` shell, a non-locking shadow value that
   permits public-key SSH, no general sudo/privileged groups, and the accepted
   layout, without changing global SSH policy;
3. creates a separate managed Git checkout plus a clean release directory for
   the exact commit rather than adopting the bootstrap checkout, records the
   configured GitHub origin and `main` branch, and proves `rcp` can fetch it
   without borrowing the invoking operator's credential;
4. for a private source origin, guides setup of a distinct read-only source
   deploy key labelled `rcp-source:<installation-id>` and records only its public
   fingerprint; for a public origin, stores no source credential;
5. runs managed Git/npm/Web/uv work as `rcp`, revalidates/rebuilds the managed
   checkout with `npm --prefix web ci`, `npm --prefix web run build`, then
   `uv sync --frozen` before service activation;
6. installs a stable CLI wrapper and non-reloading systemd unit, but on a fresh
   data directory leaves that unit stopped and disabled;
7. prints the existing interactive
   `sudo -u rcp -H /usr/local/bin/rcp space init --team --name ...` command and
   the later activation/readback commands, then exits with the fresh service
   stopped. The installed wrapper resolves the configured `RCP_DATA_DIR`; the
   operator runs initialization in that terminal so neither another process nor
   a service log receives the one-time bootstrap code; and
8. only after successful initialization does the operator run the printed
   systemd activation command. F3b's install drive reads back process and HTTP
   health without widening the loopback bind; F5 later makes the printed
   `server doctor` readback authoritative. A rerun against an already initialized
   owned team data directory may converge the service to running.

Root performs only the OS changes needed for the account, directories, wrapper,
and systemd. Re-running install must converge or refuse an exact incompatible
state. It must not replace a data directory, source checkout, or account it
cannot prove it owns. Removing the bootstrap checkout after success must not
affect doctor, update, service restart, or team-space operation. No install or
initialization path opens SQLite beside a running service.

### F3b — Ubuntu operator guide and live install proof

Own:

- new `docs/server.md`;
- the concise team-server install/run/update commands in `README.md`; and
- `tests/test_server_install_live.py`.

Document tested prerequisite commands separately for Ubuntu 22.04 and 24.04,
then the one fresh-clone bootstrap needed before the CLI exists: a normal
operator runs `npm --prefix web ci`, `npm --prefix web run build`, then `uv
sync` after prerequisites, followed by the absolute bootstrap `.venv/bin/rcp
server install` path under `sudo`.

Drive that exact sequence on disposable x86-64 hosts for both Ubuntu releases.
Initialize interactively as `rcp`, prove the bootstrap code never enters service
logs, activate systemd, read back process/HTTP health, remove the bootstrap
checkout, rerun the installer, and verify ownership, modes, loopback-only bind,
password authentication refusal, optional public-key `rcp` login with the
non-locking shadow value, and continued service. This packet may report a
focused F3a defect for repair;
it does not grow a second installer in test or documentation code.

The operator guide also gives two explicit, separately auditable access setups:
installing a key for optional direct `rcp@server`, and a root-owned,
`visudo`-validated narrow rule for a named operator to invoke only the installed
service-account command family through `sudo -n -u rcp -H`. RCP does not infer
an operator account or edit sudo policy silently. The live drive proves the
documented rule permits D6's fixed provisioning command and refuses an
unlisted command.

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
- dispatch in `src/rcp/server_ops/cli.py`; and
- `tests/test_server_doctor.py`.

Report source/release roots, configured origin/branch, managed-main HEAD,
upstream HEAD, candidate/current/running commits, service/reload state,
space/process/data identities, ownership and mode problems, control-socket
health, Web bundle build identity, and installed dependency readiness without
revealing secrets. P5 and O3b later add their concrete provider and backup
summaries; F5 does not invent placeholders or a generic status registry for
owners that have not landed.

Distinguish “checkout updated but old process still running” from corruption.
Doctor is read-only and works interactively and as one structured document.

### F6a — Update source and candidate build

Own:

- new `src/rcp/server_ops/update.py`;
- command dispatch in `src/rcp/server_ops/cli.py` plus read-only calls into
  `src/rcp/server_ops/doctor.py`; and
- `tests/test_server_update_prepare.py` plus a live local-origin Git fixture.

Implement the source/build half of the exact settled order:

1. require the authorized `sudo rcp server update` operator entrypoint, retain a
   narrow root coordinator, and run all remaining source/build steps as `rcp`;
2. acquire one update admission lock and inspect active maintenance;
3. require configured origin, checked-out `main`, clean tree, and fast-forward
   relationship, and prove fetch uses only the configured source identity;
4. fetch and show current/target commits; prompt unless explicitly confirmed;
5. fast-forward the managed `main` checkout to `origin/main`;
6. create or validate one clean release directory for the exact target commit;
7. run `npm --prefix web ci`, `npm --prefix web run build`, and `uv sync
   --frozen` inside that release as `rcp`, without changing the current release
   or environment.

If fetch/build/sync fails, report the exact managed-main,
candidate/current/running commits and leave the old release serving unchanged.
Never reset, force-pull, auto-stash, choose another branch, or call a packaged
updater. F6a produces one immutable built-candidate receipt consumed by F6b; it
cannot open live app data, switch `current`, or restart systemd.

### F6b — Candidate rehearsal against copied real state

Own:

- new `src/rcp/server_ops/rehearsal.py`;
- one explicit startup-effect fence in `src/rcp/api/app.py` and
  `src/rcp/background.py`, consumed by both rehearsal and F6d's cutover
  verification;
- narrow orchestration through `src/rcp/server_ops/update.py`; and
- `tests/test_server_update_rehearsal.py` with copied real-state fixtures and
  attempted-effect probes.

Consume F6a's built-candidate receipt and reuse O2a/O2b's concrete online SQLite and
project-file capture primitive; do not add a second copy implementation. Capture
a consistent rehearsal copy, then rehearse migration,
startup, ownership, replay/recovery planning, and representative API reads
without opening the live data directory from a second process.

Before the candidate starts, build one typed rehearsal overlay from that capture.
In the copied database, rebind every project locator and every RCP-owned local
stage/file pointer to request-owned temporary roots; construct inert temporary
repository roots where a normal read path requires one, while retaining the
original nonsecret descriptors only for comparison. An active task whose local
stage was not part of the backup capture is explicitly plan-only and points to a
known-absent overlay path, never back to its live absolute stage. Remote paths
remain data only while the effect fence is active. Validate the complete copied
locator/path inventory before launch and fail rehearsal if any candidate-resolved
app-data, canonical-state, checkout, attachment, or stage path escapes the
overlay. Candidate source/release reads are the sole intentional exception.
Tests place sentinels in the live data, checkout, stage, provider-home, and
remote-effect paths and prove not even a metadata read or cleanup reaches them.

Treat `transfer-inbox/` as another live effect surface, not as an incidental
file under app data. Rebind every copied incoming-transfer lease and path to a
request-owned known-absent overlay entry, including a request whose live upload
already reached a complete verified inbox file. Candidate recovery may plan or
render the missing-file disposition inside the copy, but it cannot read,
complete, import, rename, or clean up the live inbox. Sentinels prove both a
partial and a complete live inbox are untouched. F6c may later capture an exact
complete inbox entry only after F6d closes admission and the upload reaches its
durable boundary.

Inventory every project from the copied database. A project whose only capture
failure is an already-unreachable configured SSH host may remain explicitly
**not replay-verified for this update**: the candidate must preserve its catalog
identity, return the same unavailable/degraded projection as the current release,
and perform no remote or canonical effect. That one condition does not hold the
whole lab's source update hostage. A reachable project that cannot be captured
or replayed, a new candidate-only failure, an unsafe file, or an unknown reason
still fails rehearsal. The receipt names every verified and unavailable project;
it never turns partial coverage into a complete-replay claim.

The explicit fence starts no provider turn or capability warm, watcher poll or
delivery, scheduled operation, remote-stage cleanup, Git write, recovery
dispatch, or other external effect. Rehearsal treats an attempted effect as a
failure rather than letting it escape to the live world. F6d reuses the same
fence while the switched candidate is being verified; do not build a second
partial maintenance-mode list. A rehearsal failure leaves the old release
serving and reports the candidate/current/running commits. Success produces one
immutable verified-candidate receipt consumed by F6d; this packet cannot switch
`current` or restart systemd.

### F6c — Coherent update rollback checkpoint

Own:

- new `src/rcp/server_ops/update_checkpoint.py` for the explicit local rollback
  inventory;
- narrow recovery-critical-root inventory helpers in `src/rcp/runs/shared.py`
  and `src/rcp/attachments.py`, so the update owner never guesses their paths;
- narrow checkpoint orchestration through `src/rcp/server_ops/update.py`; and
- `tests/test_server_update_checkpoint.py` with crash-boundary and complete-root
  inventory fixtures.

Compose O2a/O2b's local SQLite/project-file capture with an explicit snapshot of
recovery-critical app-data roots, including local run stages and temporary
attachment sets, to make one coherent rollback checkpoint of every RCP-owned
state surface candidate startup may change. Exclude source/project checkouts,
credentials, provider homes, caches, locks, and runtime metadata. The
checkpoint is created only after the caller has closed admission and all
in-flight owners named by F6d have reached a durable boundary; this packet does
not itself close admission, switch a release, or restart systemd.

The checkpoint manifest records the exact database snapshot, per-project file
capture, recovery-critical roots, current release, and candidate receipt. A
failed or partial capture is unusable and cannot authorize a switch. Restoration
into a temporary verification root must reproduce every included byte before
F6d consumes it. Rollback is replacement, not an overlay: after the candidate is
stopped, its app-data root and each candidate-touched server-local `.research`
root are atomically moved to an operation-specific quarantine, then rebuilt from
the checkpoint's SQLite, retained canonical/chat/Paper/facts inputs,
project-owned imported sources, local stages, attachments, and completed
transfer inbox entries. Materialized outputs and caches are regenerated by the
previous release. This removes candidate-created unknown roots instead of
leaving them beside restored state; the quarantine remains for diagnosis until
explicit safe cleanup. The startup-effect fence forbids remote canonical writes,
so rollback never pretends to replace a remote root it did not snapshot.
Persist a small fsynced rollback journal beside the checkpoint, outside the
live app-data and project roots, before the first move. It records the exact
checkpoint, previous release, quarantine paths, and replacement phase. Every
move, restore, verification, and finalization is idempotent. `install`, `update`,
and `doctor` detect an unfinished journal; they keep the service stopped and
resume verification of the previous state rather than starting either release
or deleting an uncertain path. Inject a coordinator crash after every journaled
phase and prove re-entry restores the same pre-cutover bytes exactly once.
This update-local checkpoint is separate from O1-O4 encrypted backup and
disaster restore.

### F6d — Update cutover, verification, and loud rollback

Own:

- new `src/rcp/server_ops/update_cutover.py`;
- narrow dispatch in `src/rcp/server_ops/control.py` and the F3a system-service
  restart/readback seam in `src/rcp/server_ops/install.py`;
- the durable update receipt and read model in this update owner;
- narrow receipt integration in `src/rcp/server_ops/doctor.py`; and
- `tests/test_server_update_cutover.py` plus the live systemd failure drive.

Consume and revalidate F6b's verified-candidate receipt, then implement the
short maintenance half of the settled order. After admission closes and the
durable boundary is reached, invoke F6c and require its verified rollback
checkpoint before switching:

9. close mutation and machine-operation admission; wait for in-flight provider
   turns, mutations, backups, provisioning steps, and transfer uploads to reach
   their durable boundary; leave durable watchers recoverable; enter a short
   maintenance window; and require the switched process to start behind F6b's
   startup-effect fence;
10. invoke F6c after the durable boundary and require its complete verified
    rollback checkpoint before any release switch;
11. use the narrow root coordinator to atomically switch `current` and restart
    systemd with normal work still closed;
12. read back the running commit and repeat the startup, ownership,
    replay/recovery, and representative API checks, including the unchanged
    degraded projection for each explicitly unavailable project; and
13. only after those checks pass, release the one startup-effect fence, start
    the deferred background/maintenance owners, and reopen normal work.

The fenced candidate cannot touch remote run stages, provider homes, watchers,
or other external state before the rollback decision, so those surfaces are not
pretended into a local checkpoint. Local run stages and attachment sets still
belong in the checkpoint because they are RCP-owned recovery inputs and current
startup cleanup can mutate them. Cache and materialized snapshot roots remain
rebuildable exclusions.

If any post-switch check fails, stop the candidate, restore the checkpoint and
previous release pointer, start and verify the previous release, and only then
reopen service. Report the failed target and restored commit through CLI output,
server status, and a durable operation receipt. Never roll back silently, reset,
force-pull, auto-stash, choose another branch, call a packaged updater, or give
`rcp` general sudo/systemd control. F6c's local checkpoint remains separate from
O1-O4 encrypted backup and disaster restore.
The failure drive makes the candidate create an otherwise unknown app-data entry
and server-local `.research` entry before failing, and proves both survive only
inside quarantine while the restored old service reads the exact pre-cutover
state. A second failure drive kills the root coordinator after every rollback
journal phase and proves startup cannot bypass or duplicate the pending
restoration.

## Provisioning packets

### P1 — Durable provisioning records and state machine

Own:

- `src/rcp/storage/models.py`;
- schema/migration additions in `src/rcp/storage/base.py`;
- new `src/rcp/storage/provisioning.py` mixed into `AppStore`; and
- `tests/test_project_provisioning_storage.py`.

Model one request id, kind (`create_team_project` or incoming transfer), target
space, human authorizer, proposed canonical project id, repository sources,
the fixed local central root or one requested absolute SSH central root,
intended/resolved paths, Git and provider checks, timestamps, retryable
diagnostic, final-review digest, and explicit cancellation disposition. A new
project request mints one random proposed `project_id` when the request is
created; an incoming transfer uses the source project's existing id. This
reserves a collision-resistant path namespace only. It does not append project
identity, register a project, or establish a writable home before final human
review.

Persist the six backend display states exactly. State transitions are guarded in
one transaction and idempotent by step receipt. A CLI reconnect resumes; it does
not create a second request. A request id grants no machine authority. Do not put
private keys, provider tokens, SSH material, or arbitrary command text in the
record.

### P2 — Provisioning API and backend projection

Own:

- new `src/rcp/api/project_provisioning.py`;
- composition and existing member/project dependencies in
  `src/rcp/api/app.py`;
- the backend-owned project-creation control in `src/rcp/api/health.py` and the
  public ordinary-setup guard in `src/rcp/api/index.py`;
- response models in `web/src/types.ts` and calls in `web/src/api.ts`; and
- `tests/test_project_provisioning_api.py`, focused team-space cases in
  `tests/test_setup.py`, and response-shape Web tests.

Deliver member-authorized create/read/cancel routes plus the final-review
projection and request shape. P6b adds the one confirmation mutation only after
its concrete finalizer exists. These routes create or change only durable product
requests; they do not perform machine work. The
projection owns status label, exact next action, `can_run_setup`, `can_review`,
`can_cancel`, resolved paths, readiness summaries, and safe operator argv tokens.
Seal any complete lifecycle vocabulary in the Web response type so the browser
cannot branch on strings.

The health/index projection also owns one `project_creation` answer containing
the primary action label and `uses_provisioning` decision. Personal space
exports the existing direct Add-project path; team space exports durable
provisioning. Both the index action and a direct `#/projects/new` navigation
render from this answer rather than branching on `space_kind`, paths, or desktop
presence.

The existing `/api/project-setup/preflight` and `/api/project-setup/create`
routes are personal-space entry points. On a team backend, each must reject
before calling `ProjectSetupManager`, inspecting a submitted path, writing a
cache or filesystem entry, or mutating the catalog. Do not guard
`ProjectCatalog.register` globally: P6b's separately validated internal
finalizer and normal startup reopening still need the existing owner. P6b is the
only new team-project entrance into that owner.

### P3 — Repository-scoped deploy-key lifecycle

Own:

- new `src/rcp/server_ops/git_credentials.py`;
- secret-path resolution through `src/rcp/server_ops/layout.py`;
- `tests/test_git_credentials.py`; and
- a disposable GitHub repository live drive.

Generate one key per target GitHub repository on the account that owns its local
or remote checkout, show only the public key and fingerprint, derive the
protected private-key path without persisting its bytes, and give exact GitHub
instructions including one deterministic nonsecret
`rcp:<space-id>:<project-id>:<repository-alias>` label and **Allow write
access**. Verify both the execution host and GitHub host keys explicitly; do not
disable either check. The stable label and persisted public fingerprint let
restore name the old GitHub grant that an operator must revoke without backing
up key material.

For a server-local checkout, place the key below F2's server-local credential
root. For an SSH checkout, run key generation and Git only as the exact saved
remote execution account and place the key below its verified
`<remote-home>/.local/share/rcp/credentials/` root. Use one shipped remote helper
for uid/home/path/mode validation; never expand `~` in a shell string, use the
server's credential root for a remote checkout, or transfer a remote private key
through stdout, structured progress, a temporary server file, or SQLite.

Prove write using a request-scoped temporary ref that points to an existing
commit, read it back, and remove it. A failed cleanup remains **operator action
needed**. An empty repository remains **operator action needed** with the exact
instruction for the operator to create and push its first real commit; RCP does
not invent a hidden initialization commit in this slice. Never place a private
key in SQLite, the manifest, logs, structured output, prompts, or backups.

Both local and remote credential roots are explicit backup, restore, update
checkpoint, and transfer exclusions. A cancelled provisioning request may
delete only the exact request-owned key after P1's recorded cleanup disposition;
it never walks the credential root. If the public key was already installed at
GitHub, cancellation names its label/fingerprint and remains **operator action
needed** until the operator confirms that grant was revoked or explicitly keeps
the prepared request for reuse; deleting a private key is not presented as
GitHub cleanup. An activated team project's key remains
machine state and is protected from the ordinary member Delete-project path by
P6c until a future operator-owned deprovision workflow is designed.

### P4 — Central checkout preparation

Own:

- new `src/rcp/server_ops/project_checkout.py`;
- exact Git subprocess helpers local to that module;
- `tests/test_project_checkout.py`; and
- step receipts through `src/rcp/storage/provisioning.py`.

Resolve a project directory only under the configured central root on the
selected local or SSH machine, refuse symlinks/special files/unowned existing
directories, clone or verify the exact Git remote, bind the P3 key without
changing global SSH config, and prove the state and truth-scope repository paths.
Reuse the existing SSH transport construction rather than creating a local-only
provisioning path. The remote route must authenticate through the `rcp`
account's existing OpenSSH state; a missing or changed account/host key becomes
**operator action needed**, never a member-key prompt or alternate login. All
subprocesses use argv, bounded output, and timeouts.

The server-local root is F2's fixed path. For SSH, resolve the account home with
P3's shipped helper and default to
`<remote-home>/.local/share/rcp/projects`; P1 may instead carry one explicitly
requested absolute central root for lab storage. Treat it as untrusted
nonsecret input: require the exact remote account to own and write it, reject
symlink/special ancestry and `/`, and show it to the machine operator and final
human reviewer. The CLI does not accept a path override outside the durable
request.

Use P1's proposed canonical project id in the accepted
`projects/<project-id>/repositories/<alias>` path from the first preparation
step. Do not provision under a request-id path and rename a live checkout during
final confirmation.

Cancellation never recursively deletes an unproven directory. Record one
explicit reuse, operator-cleanup, or safe-created-empty disposition.

For `create_team_project`, inspect retained `.research` before declaring the
checkout prepared. Any existing canonical project identity or Patch history is
**operator action needed**, not material to adopt, overwrite, archive, or assign
the request's proposed id. If it belongs to a personal project, direct the human
to **Move to team space**; any other identity/history conflict requires an
explicitly cleaned or different repository outside this request. The later
transfer configuration packet owns the separate matching-history rule for an
incoming transfer.

### P5 — Provider readiness on the execution account

Own:

- new `src/rcp/server_ops/provider_readiness.py`;
- existing `src/rcp/providers.py`, `src/rcp/agents/launcher.py`, and
  `src/rcp/transport/ssh.py` only where the shared probe needs extension;
- narrow command/control registration in `src/rcp/server_ops/cli.py` and
  `src/rcp/server_ops/control.py`, plus its concrete summary in
  `src/rcp/server_ops/doctor.py`;
- `tests/test_server_provider_readiness.py`; and
- local plus reachable-SSH live probes.

Use the existing provider profile/runtime implementation to check executable,
version, provider-reported authentication status, model/runtime, and exact OS
execution account. `--request` resolves only the intended profiles in P1's
durable provisioning request; `--project` resolves only the existing project's
stored profiles. The CLI cannot construct an ad hoc provider target. RCP does
not invoke login, store credentials, refresh them, create an alternate provider
home, or choose among provider identities. When the check fails, publish
**operator action needed** with the provider-native command the operator must run
directly as that local or remote account, then recheck. Persist only nonsecret
readiness results and configuration references.

Codex exec, Codex app-server, and Claude retain their own provider specs behind
one call abstraction. Local and SSH use the same selected profile contract. A
failed account never falls back to a member laptop, different account, or other
runtime except the already specified pre-prompt Codex runtime fallback on the
same machine.

### P6a — Server preparation orchestration

Own:

- new `src/rcp/server_ops/project_provision.py`;
- narrow registration in `src/rcp/server_ops/cli.py` and
  `src/rcp/server_ops/control.py`; and
- `tests/test_team_project_provisioning.py` for machine preparation and recovery.

Run P3–P5 as resumable named steps and publish every result through P1. A zero
process exit cannot skip durable status readback. Crash at every preparation
boundary in a parameterized test. Before confirmation there is no canonical
project, and this service-account command has no route that can create one.

### P6b — Final human project creation

Own:

- final-review completion wiring in `src/rcp/api/project_provisioning.py`;
- project setup service seams in `src/rcp/setup.py` and `src/rcp/projects.py`;
  and
- final-creation and idempotency coverage in
  `tests/test_team_project_provisioning.py`.

The member-authorized P2 route revalidates the final-review digest, paths,
Git/provider readiness, current membership, human identity, and unchanged
request before using the existing setup/transition owners to create and register
the project. It never runs P3–P5 or another machine command.

Recheck that no retained canonical identity or Patch appeared after machine
preparation. A direct team-project creation never adopts or archives existing
research; it fails back to review with the transfer/clean-repository action.

Final creation extends the existing identity-claim owner to append exactly the
request's proposed id after revalidation; it must not mint a second id. Crash at
each product-state boundary, and prove repeated confirmation returns the one same
project and request.

### P6c — Team-project deletion guard

Own:

- the backend-owned project-card decision and catalog deletion guard in
  `src/rcp/projects.py`;
- the exact rejection response in `src/rcp/api/index.py`;
- response typing and action rendering in `web/src/types.ts` and
  `web/src/views/ProjectLanding.tsx`; and
- new `tests/test_team_project_deletion_guard.py` plus the existing S26
  regressions in `tests/test_project_deletion.py` and
  `tests/test_project_delete_api.py`, and the S122 last-member refusal copy and
  browser coverage.

Add `can_delete` and `delete_unavailable_reason` to every project card. A
personal project keeps `can_delete=true`. A team project publishes
`can_delete=false` with an exact operator-owned-deprovision reason; the Web omits
the ordinary **Delete project** action from that card and does not derive the
decision from space, path, or checkout state.

The API and `ProjectCatalog.delete` independently re-read the space kind and
refuse a direct deletion request for a team project before removing any task,
cache, snapshot, stage, or database record. Prove the managed checkout, deploy
key, canonical project, imported sources, and app records are unchanged after
the UI exposes no ordinary action and after a direct API attempt. Preserve
S26's personal-project behavior unchanged. Update the last-project-member
refusal to say another member must be added; it must not retain the now-invalid
team suggestion to delete the project, and S122 is re-driven.

This packet does not implement team-project deprovisioning. T4a's source
retirement is a request-bound transfer transition with its own fence and receipt,
not an alternate entrance to ordinary project deletion.

## Desktop packets

### D1 — Saved connection metadata and macOS credential storage

Own:

- new `web/src-tauri/src/team_connections.rs`;
- target-specific credential dependency/config in `web/src-tauri/Cargo.toml`;
- registration in `web/src-tauri/src/commands.rs` and
  `web/src-tauri/src/lib.rs`, with the matching
  `web/src-tauri/capabilities/main.json` and generated permission entries; and
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

Own:

- candidate loopback alias/address allocation in
  `web/src-tauri/src/team_connections.rs`;
- `web/src-tauri/src/navigation.rs`; and
- `web/src-tauri/capabilities/main.json` for the exact origins that survive the
  spike; and
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
- lifecycle integration in `web/src-tauri/src/lib.rs` and
  `web/src-tauri/src/backend.rs`;
- command and permission integration in `web/src-tauri/src/commands.rs` and
  `web/src-tauri/capabilities/main.json`; and
- Rust unit plus live SSH tests.

Launch system `ssh` with argv, configured host alias, explicit local bind, remote
`127.0.0.1:8421` target, exit-on-forward-failure, bounded readiness, and owned
child lifecycle. Reuse one healthy tunnel per connection, reconnect with backoff,
and stop only desktop-owned tunnels on Quit. Never kill a remote RCP service or
accept a tunnel that resolves to an unsaved origin.

### D4a — Team handshake and WebView session establishment

Own:

- new `web/src-tauri/src/team_session.rs`;
- narrow command registration in `web/src-tauri/src/commands.rs`;
- Keychain calls through `web/src-tauri/src/team_connections.rs`; and
- focused Rust tests plus one live enrollment/session test.

Through the tunnel, verify health, expected `space_id`, team kind, server/running
protocol, and minimum shell version. Support one native enrollment call for a
bootstrap/invitation code and one storage path for an existing permanent token;
capture any newly issued token directly into Keychain and clear the input. Then
establish the server-side HTTP-only session in the real WebView cookie store
without logging or otherwise persisting the permanent token. Return one
nonsecret established-session result to D4b. A mismatch blocks mutations and
requires explicit reconnect. This packet does not change the displayed origin.

### D4b — Multi-backend WebView navigation and lifecycle

Own:

- `web/src-tauri/src/navigation.rs`, `web/src-tauri/src/windows.rs`, and
  `web/src-tauri/src/backend.rs`;
- `web/src/desktopRuntime.ts`;
- command wiring in `web/src-tauri/src/commands.rs` and
  `web/src-tauri/src/lib.rs` for D4a's established-session result; and
- live source-built desktop navigation tests.

Keep the owned personal backend running and distinguish it from the currently
displayed team origin. Navigate only after D4a establishes the cookie at D2's
saved distinct origin. Return-to-index navigates home. Reconnect never converts
team work into local work, and Quit continues to stop only the local backend and
tunnels the shell owns.

### D5 — Local multi-space project index

Own:

- new `web/src/components/TeamSpaceGroups.tsx`;
- `web/src/App.tsx`, `web/src/components/LandingIdentityMenu.tsx`, and
  `web/src/types.ts` integration;
- bounded cached-card storage through
  `web/src-tauri/src/team_connections.rs`; and
- Web tests for grouping and unavailable state.

Replace the current “not implemented” seam with **Add team space**, saved space
groups, reachability, pending invitations, and team project cards. The Add flow
collects SSH target plus bootstrap/invitation code and name for a new member, or
an existing permanent token, and delegates all secret handling to D4a. Personal
space stays first. Team cards navigate through D4b and never submit a team
request to the local backend. An unavailable group is dimmed with last-known
cards and one reconnect action; it does not block personal work.

### D6 — Fixed operator CLI bridge

Own:

- new `web/src-tauri/src/server_commands.rs`;
- command registration in `web/src-tauri/src/commands.rs` and
  `web/src-tauri/src/lib.rs`, permission updates in
  `web/src-tauri/capabilities/main.json`, and
  `web/src/desktopRuntime.ts` bindings;
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
- `web/src/views/ProjectSetup.tsx` and `web/src/App.tsx` routing;
- P2 integration in `web/src/api.ts` and `web/src/types.ts`; and
- browser plus desktop tests.

Render the backend's six statuses, exact diagnostic/next action, resolved paths,
Git write and provider readiness, final-review digest, and human authority. The
request form shows the fixed server-local root and, for SSH, the backend-proposed
home-derived root with an explicit absolute-root field for intentional lab
storage; final review repeats the resolved value. Show
**Run setup now** only from the D6 probe; always show **Copy server command**.
CLI events are transient progress, never the state machine.

Use P2's `project_creation` answer for both the project-index primary action and
the `#/projects/new` deep link. A personal backend keeps the existing
`ProjectSetup` wizard. A team backend renders `ProjectProvisioning` and never
mounts or submits the ordinary path-based wizard. Do not derive this choice from
`space_kind`, repository paths, desktop runtime, or cached connection metadata;
the direct API rejection remains the independent backend fence.

Use one primary action and real error text. Do not add muted helper/commentary
lines beneath primary labels. Final creation requires an explicit human review
action. This packet does not add a transfer entry or half-built transfer state.

## Operations packets

### O1 — Versioned backup manifest and capture plan

Own:

- new `src/rcp/server_ops/backup_models.py`;
- backup-specific inventory, retry, and diagnostic bounds in
  `src/rcp/limits.py`;
- read-only project/head inventory helpers in `src/rcp/projects.py` and
  `src/rcp/transport/state.py`; and
- `tests/test_backup_manifest.py`.

Define a strict versioned archive manifest with space identity, RCP source
commit/schema, capture time, SQLite snapshot hash, encryption recipient
fingerprint, and per-project project/home ids, locator, recorded canonical head,
captured main and graph-branch canonical commit files/heads, RCP chat files,
optional canonical Paper introduction, `.research/facts/` files, referenced kept
artifact and legacy kept-result-view files/hashes, one nonsecret checkout
recovery descriptor, or unavailable reason/time. The recovery descriptor binds
the captured provisioning record's repository sources/aliases, resolved local or
SSH central paths, machine/route references, canonical manifest configuration,
and deploy-key labels/fingerprints. It carries no private key or provider/SSH
secret. A missing, stale, credential-bearing, or internally inconsistent
descriptor makes that project uncaptured.
Immutable branch metadata, Patches, and merge receipts are canonical inputs;
main or branch materialized outputs are explicitly forbidden. The SQLite
snapshot already carries the current Paper draft and same-space writing-session
rows; the manifest links that snapshot to the separately captured canonical
introduction rather than pretending Paper is only one file.

Record the old server's nonsecret `installation_id` and optional
`rcp-source:<installation-id>` public fingerprint in the manifest, plus the
project deploy-key labels/fingerprints already present in provisioning receipts.
These are revocation pointers only; no private key, SSH credential, or provider
auth material enters the capture.

T3e later adds the explicit imported-provider-history file group through this
same manifest owner; O1 does not guess at a future path.

The plan is read-only and does not pause dispatch. It must distinguish a project
captured through head N from a project merely present in SQLite. Enumerate the
known direct `.research` roots and the explicit app-data/repository groups:
future unclassified durable roots, unknown chat/Paper entries, or unsafe
symlink/special entries make that project uncaptured until policy is added.
The current app-data classification is exact: capture `rcp.sqlite3` only through
O2a's online snapshot; T3e captures `project-sources/`; exclude raw SQLite
WAL/shared-memory files, `rcp.lock`, `rcp-server.json`, `bootstrap-manifests/`,
`project-snapshots/`, `paper-snapshots/`, `state-cache/`, `project-caches/`, the
legacy `source-cache/` and `session-slices/`, `chat-attachments/`, `run-stage/`,
`transfer-inbox/`, and `transfer-exports/`. Bootstrap manifests are local
locator copies reconstructed from the captured recovery descriptor, project and
Paper snapshots plus source/state caches are derived, lock/server metadata is
one-process runtime state, attachment/run/inbox roots are temporary execution
state, and a sealed personal source export is protected by its transfer receipt
rather than copied into a team-server backup. Known `.publish`, local mirror
quarantine, materialized outputs, and other explicitly rebuildable/temporary
groups remain named exclusions rather than accidental omissions. Any unknown
direct app-data child makes the archive partial until its concrete owner
classifies it; do not silently generalize that list into a root registry.

Record exact per-entry and total byte counts before archive streaming. Archive
content is not silently truncated or given an arbitrary product-size cutoff:
copying uses fixed-size buffers, while retry counts and diagnostic count/text
bounds are fixed code policy in `limits.py`. Insufficient staging or destination
capacity fails or makes the affected capture partial before any archive is
called protected; no implementation may load an entire project archive into
memory.

### O2a — Online SQLite snapshot and typed project inventory

Own:

- new `src/rcp/server_ops/backup_capture.py`;
- a narrow SQLite online-snapshot method in `src/rcp/storage/base.py` invoked
  through `src/rcp/server_ops/control.py`;
- read-only snapshot queries through O1's concrete project/storage owners; and
- `tests/test_backup_sqlite_capture.py` with concurrent writers and
  registration/provisioning boundary fixtures.

Use SQLite's online backup API in the lock-owning process. For each project,
derive the project id, home, and locator inventory from that captured database,
so a later registration is absent rather than half-added. Resolve its completed
provisioning record, project-linked task set, and referenced kept-file names from
that same snapshot; do not infer a clone source from a path or a member checkout.
Open the copied database read-only, validate task artifact descriptors with
`AgentArtifactDescriptor` and kept view rows with the existing storage model,
and bind that typed inventory into O1's manifest. A malformed or cross-project
reference makes that project uncaptured.

Publish one immutable database-snapshot/inventory receipt for O2b. This packet
does not open a canonical project repository, copy project files, or acquire a
project lock.

### O2b — Optimistic canonical and project-file capture

Own:

- O2a's `src/rcp/server_ops/backup_capture.py` for file-capture orchestration;
- retained canonical-history export helpers in `src/rcp/transport/state.py` and
  `src/rcp/history/manager.py`;
- narrow typed readers beside the chat owner in `src/rcp/service.py` and Paper
  owner in `src/rcp/paper/service.py`; and
- `tests/test_backup_capture.py` with concurrent file writers and remote
  unavailable hosts.

Consume and revalidate O2a's receipt. Verify each live checkout's repository
identities against the captured nonsecret recovery descriptor. Record the
accepted main head and each retained branch head, then copy only the
bounded retained-history inputs required to replay them: main manifest/Patches,
branch metadata/Patches/merge receipts. Reuse the existing retained-history
inventory in `src/rcp/transport/state.py`; do not create a second branch walker.
Separately capture every valid canonical RCP chat JSONL file through the chat
owner, the optional canonical Paper introduction through `PaperService`, opaque
safe regular `.research/facts/` files through a bounded facts inventory, and
only kept artifact/result-view names referenced by the SQLite snapshot through
the existing workspace readers. Use only O2a's typed names, never a later live
store query. Do not walk all of repository `artifacts/` or `views/`.

Do not acquire the canonical publication, append, chat, or remote refresh lock:
backup must not delay dispatch or Apply. Use bounded optimistic stable reads.
For an append-only chat, record an observed byte boundary and accept only a
complete, typed-valid JSONL prefix through that boundary. A non-null operation
binding must resolve to the same project's captured SQLite task set; the first
record whose operation was created after the database snapshot, and its suffix,
are absent rather than dangling. Legacy null operation bindings remain readable
but never prove native Resume. For mutable Paper, facts, or kept files, validate safe regular-file
identity and stable bytes/metadata across the read, retry a bounded number of
times, then mark the project uncaptured on continued churn. Atomic replacement
therefore yields the old or new whole file, never a mixed claim. An
unknown/malformed/symlink/special required entry or missing referenced kept file
makes that project uncaptured rather than silently omitted. Remote failure
marks that project uncaptured while preserving other captures. T3e later adds
its explicit imported-provider-history file group through this same capture
owner; O2b does not guess at a future directory. Never walk or copy the live
execution account's provider home.

Never walk whole repository roots or include source, credentials, `.git`,
materialization, temporary attachment/stage/transfer-inbox data, caches, or
arbitrary symlink targets.

### O3a — Backup configuration and systemd timer

Own:

- new `src/rcp/server_ops/backup_config.py`;
- the backup section of `src/rcp/server_ops/config.py`;
- new `src/rcp/server_ops/assets/rcp-backup.timer` and
  `src/rcp/server_ops/assets/rcp-backup.service`;
- narrow root-command and unit-installation wiring in
  `src/rcp/server_ops/cli.py` and `src/rcp/server_ops/install.py`; and
- `tests/test_backup_configuration.py`.

`backup configure` interactively records an explicit destination, schedule,
retention, and `age` public recipient. It never accepts or stores the private
identity.

Persist those four values in F2's versioned installed-server config, not SQLite,
using root-owned atomic replacement. Propose daily at 02:00 server local time,
retain the newest 30 integrity-readback archives, and additionally retain the
newest complete archive when it is older than that window. Require explicit
confirmation or edited values before enabling the systemd timer. Render and
read back the timer from the same resolved schedule so configuration cannot
drift from execution.

Until O3b's concrete `backup run` owner is present, installation may render the
unit and persist validated configuration but must leave the timer disabled and
the feature unadvertised. O3b owns the final readback that makes enabling the
  configured timer safe; an intermediate `main` commit never schedules a
missing command.

The destination is one writable filesystem directory and may be local or
mounted. Do not add S3, SSH upload, cloud-sync, filesystem-topology detection, or
an on-server/off-server warning.

Scheduled execution invokes O3b's same `backup run` command. This packet stores
retention policy but does not delete an archive.

### O3b — `age` encryption, readback, retention, and status

Own:

- new `src/rcp/server_ops/backup.py`;
- the durable backup outcome receipt and read model in that module;
- narrow `backup run` dispatch through `src/rcp/server_ops/cli.py` and
  `src/rcp/server_ops/control.py`;
- narrow backup-summary integration in `src/rcp/server_ops/doctor.py`; and
- `tests/test_backup_encryption.py` and `tests/test_backup_retention.py` plus a
  real encrypt/decrypt drive.

`backup run` consumes O1/O2a/O2b's capture and O3a's resolved configuration, streams a
deterministic archive through the version-checked upstream `age` CLI
(`>=1.0.0,<2.0.0`) with one validated native X25519 `age1...` recipient into an
atomic destination filename, then read-checks metadata and records
protected/partial/failure status. It never accepts or stores the private
identity. Do not accept plugin, SSH, passphrase, or post-quantum recipients in
this first format; that would make the Ubuntu 22.04 and 24.04 restore contract
version-dependent. Retention deletes only archives whose format, destination,
ownership, and successful readback are proven; preview exact targets before any
manual destructive cleanup.

Status describes captured bytes and projects, not the physical durability of
the operator's storage.

After its command and readback are installed, converge the O3a timer to the
confirmed enabled state. A failed first run or unit readback leaves it disabled
with an exact diagnostic rather than installing a schedule that cannot produce
an archive.

### O3c — History-only task and native-session fence

Own:

- the durable `history_only` task marker and migration in
  `src/rcp/storage/base.py`;
- the bounded native-session-detachment transaction and control admission in
  the existing task/session owner `src/rcp/storage/agent_tasks.py`, with lifecycle
  projection in `src/rcp/storage/rows.py`;
- an API-only artifact response projection and matching route admission in
  `src/rcp/api/tasks.py`, leaving the stored `AgentArtifactDescriptor` as
  provenance rather than persisting derived availability;
- the canonical-chat response seam in `src/rcp/service.py`; and
- `tests/test_history_only_tasks.py` with chat, Paper-coach, Resume, Retry, and
  graph-repair coverage.

Keep a history-only task's honest status, answer, receipts, usage, attribution,
and native-session id in durable history, but export no executable continuation
binding from it. Its API projection publishes `history_only=true`, returns
`native_session_id=null`, and forces `can_pause = can_resume = can_retry = false`.
The backend rejects every direct control or repair attempt and excludes
history-only rows from native-chat-origin proof. Canonical chat reads preserve the stored JSONL
bytes but expose a message's native-session id only when its operation still has
a non-history-only continuation binding. The Web therefore starts a fresh
provider session in the same RCP chat after restore or import without deriving
that decision or rewriting the old transcript.

Task/artifact projection also separates historical metadata from readable
bytes. Publish `available`, `unavailable_reason`, `can_open`, `can_download`,
`can_keep`, and `can_revise` for every task artifact. A referenced kept artifact
is available through its repository owner with Open and Download enabled, but
Keep and revision through the detached native session disabled. An unkept
artifact whose excluded stage bytes no longer exist is unavailable with every
`can_*` false. Content, viewer, download, Keep, and artifact-context admission
recheck those facts and never resolve an unavailable stage. The generated viewer
receives no Keep or chat-context action that admission would reject.

Provide one transaction that marks selected existing tasks history-only and
deletes their `writing_sessions` and `chat_session_contexts` rows. Those two
tables are provider-native Resume/prompt indexes, not the Paper draft, task
answer, or RCP chat text. O3d-a applies that transaction to restored history;
T3b-export uses the same marker at import. This packet adds no restore or transfer
orchestration. Its schema change owns the corresponding G2 upgrade fixture
before it lands.

### O3c-ui — Render backend-owned historical artifact decisions

Own:

- the task-artifact response restatement in `web/src/types.ts`;
- artifact URL construction and transcript reconciliation in
  `web/src/agentTasks.ts`;
- artifact-card actions in `web/src/components/NodeChat.tsx`; and
- focused Web tests for kept and unavailable history-only artifacts.

Render only the decisions O3c publishes. A kept history-only artifact shows Open
and Download, but neither Keep nor native-session revision. An unavailable
artifact shows its backend reason and no action. Do not derive availability or
an action from `history_only`, `kept_filename`, media type, or a failed request;
do not construct, preflight, image-load, or probe a content/viewer/download URL
when its corresponding `can_*` answer is false. Keep the existing runtime error
surface for a route that becomes unavailable after projection, but do not use
that failure as ordinary lifecycle discovery.

### O3d-a — Restored task, Experiment, session, and code detachment

Own:

- narrow connection-scoped transition helpers beside the concrete owners in
  `src/rcp/storage/agent_tasks.py`, `src/rcp/storage/episodes.py`, and
  `src/rcp/storage/spaces.py`; and
- `tests/test_restore_task_episode_detachment.py` with idempotent task,
  Experiment, report, browser-session, and enrollment-code fixtures.

Provide idempotent connection-scoped helpers for the parts of a restored
database owned by tasks, Experiment episodes, reports, and space
authentication. Use O3c to interrupt and mark every pre-restore task
history-only. Stop every nonterminal Experiment-loop episode with a restore
diagnostic and skipped wrap-up; fail any queued/running report attempt; and
clear native session/stage bindings from `experiment_episode_state` and
unfinished `episode_wrapups`. Preserve completed tasks, episodes, reports,
answers, receipts, messages, and attribution as history.

The same helper deletes every restored `team_sessions` row and revokes every
unconsumed bootstrap or team-enrollment invitation code. Those are ephemeral
browser/enrollment capabilities and must not come back from an old snapshot.
Preserve the snapshot's active permanent member-token hashes: they are durable
RCP reconnect credentials already held in member Keychains, not provider
credentials. This packet supplies helpers only; O3d-b composes the one offline
transaction and O4d owns the roster review before serving.

### O3d-b — Restored Auto-research, watcher, and recovery detachment

Own:

- new `src/rcp/storage/restore_detachment.py`, mixed into `AppStore` through
  `src/rcp/storage/__init__.py`, as the one offline restore transaction;
- narrow connection-scoped transition helpers beside the concrete owners in
  `src/rcp/storage/auto_research.py`,
  `src/rcp/storage/auto_research_children.py`, and
  `src/rcp/storage/watchers.py`; and
- `tests/test_restore_lifecycle_detachment.py` with full startup-effect
  assertions.

A database snapshot can contain far more resumable work than a running task.
Before a restored database is eligible for ordinary startup, perform one
idempotent offline detachment over every pre-restore row. Compose O3d-a's
helpers in that transaction. Stop every active/degraded or
completed-undelivered external or graph watcher, mark it notified, and clear
its next check without launching a delivery. Block pending Auto-research
recovery records, cancel pending/running Auto-research episodes, child
experiments, and accepted child admissions, and acknowledge or cancel any
pending lifecycle wake that could create another task. Preserve completed
episodes, watcher checks, answers, receipts, messages, and attribution as
history.

The operator's restore confirmation is the human stop authority for these
continuations; record that fact and the restore reason rather than pretending
the provider, watcher, or loop ended normally. Re-running the transaction must
change nothing. Then invoke the real startup recovery/reconciliation path under
the existing external-effect-disabled harness and prove it creates no provider
turn, watcher check/delivery, report retry, child admission, or automatic graph
mutation from pre-restore state. This packet owns lifecycle detachment only;
O4a-O4d own archive validation, checkout reconstruction, project publication,
authority review, and service activation.

### O4a — Archive validation and offline restored-state candidate

Own:

- new `src/rcp/server_ops/restore.py`;
- root-entry and stopped-service integration in `src/rcp/server_ops/cli.py` and
  `src/rcp/server_ops/install.py`, with running-service coordination through
  `src/rcp/server_ops/control.py`;
- unfinished-restore detection in `src/rcp/server_ops/doctor.py` and
  `src/rcp/server_ops/update.py`;
- restored machine-step invalidation in
  `src/rcp/storage/provisioning.py` and `src/rcp/server_ops/backup.py`; and
- `tests/test_server_restore_state.py` with archive, service-ownership,
  detachment, and machine-lease fixtures.

Require an explicit archive, an off-server `age` identity supplied for this run
through a protected file/descriptor rather than raw argv or environment text,
and the installed server's configured `RCP_DATA_DIR` in fresh/empty state.
Display and confirm that destination; this first contract does not redirect
systemd to an arbitrary alternate data root. Record that this is a replacement restore,
but leave the concrete old-authority and member-roster confirmations to O4d
before serving. Decrypt to a protected temporary directory and verify every
hash/schema before changing the target. Restore the SQLite candidate and apply O3d-b's
complete task/episode/watcher/recovery detachment, including O3c's history-only
session fence, but do not publish project files into an empty future checkout
path or start the service.

The running restore release must explicitly support the archive format and
recorded persistence boundary. An archive from unknown newer code fails before
target mutation and names the required update/compatible commit; restore never
tries a best-effort downgrade or asks an older binary to interpret future rows.

Before the first target-data or checkout mutation, fsync one request journal
under F2's `restore-operations/` root, outside every target named by the restore.
Bind the exact archive digest, configured target, candidate hashes,
checkout/publication inventory, durable human-confirmation receipts, and current
phase; never persist the recovery identity. Every O4a-O4d step is idempotent.
`install`, `update`, and `doctor` detect an unfinished journal and keep systemd
stopped; only re-entering `restore` may advance it. If a protected temporary
candidate disappeared, require the same archive and identity again, reverify it,
and resume the recorded phase. Crash after every journal transition and prove no
partial project or database becomes serveable.

Invalidate every snapshotted in-progress provisioning or server-operation lease
before startup. Preserve completed receipts as history, but move unfinished P1
requests to **operator action needed**, clear their old machine-step claims, and
require explicit CLI re-entry against the replacement paths and keys. An update
or backup operation captured mid-step is recorded interrupted; it never resumes
automatically or treats an old process receipt as replacement-machine authority.

This packet produces one stopped-service, validated restored-state candidate.
It cannot create repository credentials, reconstruct checkouts, publish project
files, make a project visible, or activate the service.

### O4b — Fresh keys and central-checkout reconstruction

Own:

- narrow orchestration in `src/rcp/server_ops/restore.py`;
- explicit recovery-mode seams in P3/P4's
  `src/rcp/server_ops/git_credentials.py` and
  `src/rcp/server_ops/project_checkout.py`;
- catalog rebinding and locator regeneration through `src/rcp/projects.py`; and
- `tests/test_server_restore_checkouts.py` with local, SSH, empty-root, and
  conflict fixtures.

For every captured project, read its recovery descriptor and reuse P3/P4 in an
explicit restore mode to generate a fresh repository-scoped key and reconstruct
the exact local or SSH central checkout from Git. This is machine recovery, not
a new product provisioning request or another human project-creation review. A
remote target requires the same configured OS account and a re-established,
verified SSH route. If clone/fetch produces retained `.research` input, accept
only files that are byte-identical to archive entries and no canonical commit
beyond the captured heads; an unknown or conflicting durable entry fails
without overwriting it. Regenerate any local `bootstrap-manifests/` locator from
the validated descriptor and atomically rebind the restored catalog row to that
replacement checkout; never restore a stale locator file or trust an old
absolute path merely because SQLite named it.

This packet stops after every captured checkout has been reconstructed and
rebound. It does not publish archived `.research` or project-owned files into
those checkouts, make a project visible, or start the service.

### O4c — Canonical publication and replay verification

Own:

- narrow orchestration in `src/rcp/server_ops/restore.py`;
- canonical replay and branch verification through
  `src/rcp/history/manager.py`;
- canonical RCP chat publication through `src/rcp/service.py`;
- Paper publication through `src/rcp/paper/service.py`;
- facts and referenced kept-file publication through
  `src/rcp/transport/state.py`;
- project visibility/readback integration in `src/rcp/projects.py`; and
- `tests/test_server_restore_projects.py` with main, branch, chat, Paper, facts,
  kept-file, unavailable-project, and byte-readback fixtures.

Publish captured main/branch histories, canonical RCP chat JSONL, Paper
introduction, facts, and referenced kept artifact/result-view files through
their concrete atomic owners. Replay each captured main head, validate every
retained branch head and merge receipt, and verify every manifest byte before
making the project visible. Restore explicitly kept artifacts and legacy kept
result views through their workspace owner; canonical chats, Paper, and facts
stay under their canonical owners. Projects that the archive explicitly marked
uncaptured remain visible and unavailable rather than blocking restoration of
protected projects.

Do not extract source checkouts, other materialized outputs, temporary input
attachments, live provider homes/logs, old Git/provider/SSH credentials, or
caches from the archive. Source checkouts are reconstructed from Git and fresh
machine credentials. This packet proves restored project data and replay; it
does not activate the server.

### O4d — Old-authority review and replacement activation

Own:

- final review, activation, and readback orchestration in
  `src/rcp/server_ops/restore.py`;
- interactive and machine-readable confirmation in
  `src/rcp/server_ops/cli.py`;
- stopped-to-running service coordination through
  `src/rcp/server_ops/control.py`; and
- `tests/test_server_restore.py` plus the fresh-host live restore drill.

Before serving, render and require confirmation of the concrete old-authority
checklist: destroy or fence the old server data; revoke the old source and
per-repository Git deploy-key labels/fingerprints; revoke or replace any old
server-to-remote SSH authorization; and revoke old provider-native login state
if the old machine could still use it. RCP does not perform those provider/SSH
revocations or accept their secrets. A proven-destroyed old machine may satisfy
the machine-local items, but the operator must record which disposition applies.

Also show the archive capture time and exact active-member roster whose
permanent token hashes will remain valid. The operator confirms that this
snapshot-time credential state is acceptable; restored HTTP sessions and unused
enrollment codes are always invalidated regardless. If a known token was
revoked or rotated after the captured snapshot, do not expose the restored
service until the operator has selected a safe newer archive or, when another
active enrolled member remains, explicitly removed the affected member through
restore's offline console step. That step reuses O5a's transaction and O5b's
completion check under the stopped-service ownership lock after O3d-b has
detached all live work; it is not a second member-removal policy. If the
known-stale token belongs to the only active member, member removal is correctly
refused and restore remains stopped for a
separate human-identity recovery design outside this slice. The machine
operator cannot mint a replacement member credential or impersonate that
person. Never silently claim that a point-in-time archive contains revocations
made later.

T3e later adds the explicit imported-provider-history entry and byte-for-byte
readback through O4c's publication owner and this packet's final readback. The
restored RCP chats, task answers, and Paper content remain readable, but no
pre-restore task, chat turn, or writing-session row can Resume, Retry, repair,
or claim that an excluded native provider session still exists. Continuing an
old RCP chat starts a fresh checked provider session after readiness succeeds.
The replacement may serve restored history after every captured checkout and
canonical replay plus the old-authority and member-roster reviews, even when
provider-native authentication is absent. In that case backend readiness keeps
dispatch and chat continuation unavailable with the exact provider-native login
action; RCP neither performs that login nor treats it as failed data restore.
Only after every O4a-O4c check and both explicit reviews pass may this packet
start the replacement service, verify its space/commit/project readback, and
open admission. That readback is the only transition that completes the restore
journal. A crash before it leaves the service stopped and the same operation
resumable; a retry cannot skip either review, duplicate publication, or create a
second space.

### O5a — Durable member-removal fence and identity tombstone

Own:

- the additive `removal_started_at` and `removed_at` member fields,
  bootstrap/space-invite `revoked_at`, and project-invite `revoked` response
  migration in
  `src/rcp/storage/base.py`, `src/rcp/storage/models.py`, and
  `src/rcp/storage/rows.py`;
- member-removal transaction and active-member queries in
  `src/rcp/storage/spaces.py`;
- the existing self-service credential endpoint in `src/rcp/api/team.py` only
  for the last-credential and sole-project-member guards;
- pending project-invitation invalidation through its concrete owner in
  `src/rcp/storage/projects.py`; and
- `tests/test_server_member_removal_storage.py`, focused cases in
  `tests/test_team_authentication.py`, and the corresponding G2 schema-boundary
  fixture.

Preview the exact member, active tasks plus Auto-research and Experiment
episodes, project memberships, member token, browser sessions, and unconsumed
space/project invitations before confirmation. A row with no issued member
credential is preprovisioned, not an enrolled replacement. Refuse before
mutation if removing the target would leave no other active member who has
completed enrollment; a pending invitation or preprovisioned name is not a
replacement. Also refuse if the target is the only active member of any project,
name each project, and require a current project member to add another enrolled
member through the ordinary product flow first. Machine authority never assigns
project membership as a side effect of removal.
The existing self-service permanent-token revocation also refuses if it would
remove the sole live token of the last active enrolled member or leave any
project with no member who can still authenticate. Atomic rotation remains
available because it commits the replacement token before invalidating the old
one. This prevents RCP's own UI/API from stranding the space or a project without
granting the machine operator a member-impersonation or credential-reset path.

On confirmation, re-read the preview and perform one transaction that sets
`removal_started_at`, revokes every member token, ends every browser session,
marks every unconsumed space invitation authored by the target revoked, marks
every pending project invitation authored by or addressed to the target revoked
without pretending the invitee declined it, and removes all active project
memberships. Every admission and identity lookup treats
`removal_started_at` as inactive immediately. Keep the immutable `space_users`
row, display name, creation time, and user id as a tombstone so old tasks,
canonical receipts, invitations, and attribution remain intelligible. Never
physically delete the durable human identity or reuse that id for a later
enrollment.

The transaction is idempotent. `removed_at` is set only after O5b proves the
member's active work has reached its required stop boundary. A crash between
those states leaves an explicit removal-in-progress row that startup and CLI
re-entry can reconcile; it never silently restores access.

### O5b — Member work stop and crash-safe reconciliation

Own:

- new `src/rcp/server_ops/members.py`;
- task pausing through `src/rcp/background.py`, episode fencing through
  `src/rcp/runs/membership_fence.py`, and startup reconciliation composition in
  `src/rcp/api/app.py`;
- command dispatch through `src/rcp/server_ops/cli.py` and
  `src/rcp/server_ops/control.py`; and
- `tests/test_server_member_removal.py`.

After O5a's access fence commits, use the existing graceful task, Auto-research,
and Experiment-loop stop owners for every still-live operation authorized by the
target. Do not kill a provider turn already in flight: it settles honestly, its
Apply rechecks the removed project membership and is refused, and then the
normal stop owner terminalizes the enclosing work. Once readback proves no live
authorized work remains, set `removed_at` idempotently and publish the completed
CLI result.

Startup and command re-entry scan only members with `removal_started_at` and no
`removed_at`, repeat the same named stop operations, and complete the tombstone.
Inject a crash after the access fence, after each stop request, and before final
completion; none may restore a token/session/membership, spend through a lost
episode fence indefinitely, or duplicate a stop/receipt. A persistent stop
failure remains visible to `doctor` and CLI readback as removal in progress with
the exact work that has not reached its boundary.

Do not reuse self-service token revocation as fake removal and do not invent a
member administrator rank or operator-issued member credential.

### O6 — Read-only Server Settings projection

Own:

- new `src/rcp/api/server_status.py` plus composition in
  `src/rcp/api/app.py`;
- new `web/src/components/ServerSettings.tsx` plus the required
  `web/src/types.ts`, `web/src/api.ts`, and `web/src/App.tsx` routing additions;
  and
- browser tests.

Show service/running/upstream commits, update readiness, last backup and failure,
protected/uncaptured projects, restore drill age, provider/machine readiness, and
operator command names. Compose those answers from F5/F6d, P5, O3b, and O4d's
concrete read models; do not create a parallel generic server-status store.
Expose no HTTP mutation for update, backup, restore, Git credential setup,
provisioning execution, or member removal. The D6 desktop bridge remains a
native SSH action, not an admin API.

## Transfer packets

### T1 — Append-only canonical home-transfer schema and replay

Own:

- `src/rcp/core/models.py`;
- `src/rcp/core/validation/patch.py`,
  `src/rcp/core/transition_models.py`, and `src/rcp/core/transitions.py`;
- `src/rcp/history/manager.py` and `src/rcp/history/delta.py`; and
- persisted Patch compatibility/replay coverage plus new
  `tests/test_project_home_transfer.py`.

Do not append a second `ProjectIdentity`: current replay correctly calls two
different nameplates a conflict. Keep the initial `project_id` nameplate
immutable and add one system-produced, human-authorized ordered home-transfer
record containing project id, previous home, new home, and source human
attribution. Replay reduces accepted transfers in order and halts if project id
or previous home does not match the current derived home.

The record carries the source-release and target-admission actors with their
respective space ids; it never treats their local user ids as one namespace.
Agents cannot author this record. One synchronous backend transition appends it
or nothing. Historical replay never consults current membership. Existing old
identity Patches remain byte-compatible.

### T2a — Linked transfer request storage and protocol state

Own:

- transfer-kind records in `src/rcp/storage/provisioning.py`; and
- `tests/test_project_transfer_request_storage.py`.

Model the linked personal-source and team-target requests, independent human
confirmation receipts, source-configuration/version negotiation, archive digest
binding, and one-time transition-proof lifecycle. The personal source records
intent and target `space_id`; the team target records the linked incoming
request and can later run ordinary P3–P5 preparation.

Before target preparation, the source request binds one checksummed nonsecret
configuration summary: source RCP/schema and supported transfer-codec versions,
repository source URLs and repository/machine aliases, state repository,
truth-scope provenance, and the source-manifest digest. The target records the
exact accepted version and reviewed target-preparation revision without treating
source paths, provider homes, or credentials as target configuration. No source
fence can become admissible unless a common codec/schema path was recorded and
the later source owner revalidates that summary.

At link creation the source stores one random 256-bit source-release proof and
the target stores an independent random 256-bit target-activation proof. Each
protected request row retains its own raw value and only the other side's SHA-256
commitment. Model exact states for unexposed, exposed at its legal transition,
acknowledged, and consumed; after consumption retain only the hash/receipt.
Every state change is idempotent and bound to the exact spaces, requests,
project, preparation revision/head, proof commitments, and eventual archive
digest. A stale or mismatched identity fails closed. These are request-scoped
transition proofs, not RCP member, provider, Git, or SSH credentials.

The target-admission receipt records the authenticated actor, reviewed
preparation revision, resolved central paths, and both proof commitments without
creating a canonical project or granting machine authority. The source-release
receipt remains a distinct source-space actor record. Storage never assumes the
personal and team user ids share a namespace.

### T2b — Authenticated transfer APIs and proof exchange

Own:

- source/target request, link, confirmation, proof-retrieval, and cleanup-ack
  endpoints in `src/rcp/api/project_provisioning.py`; and
- `tests/test_project_transfer_request_api.py`.

Expose T2a only through each space's ordinary authenticated request API. The
desktop relays nonsecret configuration/version summaries and proof commitments,
then completes the idempotent cross-link before either final confirmation. Raw
proofs never enter Web state, URLs, logs, command arguments, or the other backend
before their committed boundary.

One final desktop review action calls target admission first and source release
second through two separate authenticated sessions. Each backend records its own
actor and receives no credential for the other space. A crash between calls
leaves target admission visible and resumable while the source remains writable;
the target is not a canonical project yet. The linked requests identify exactly
one T3a archive. After T4a exports it, both APIs bind the same concrete digest and
source/target/project identities rather than defining another archive format.

The raw source-release proof appears only inside T4a's already-fenced sealed
archive and T4c must verify its commitment before import. The raw
target-activation proof becomes retrievable only after T4c commits activation.
Its fixed native-only route requires the saved permanent team-member token, the
exact target confirmer, and the completed linked request; a cookie-only Web
session, another member, or a pre-activation caller cannot read it. T5a passes
that value directly to the pinned source backend, which verifies its stored
commitment before T4a retires the source row or sealed archive. Only the public
cleanup acknowledgment lets the target erase its raw proof; retry beforehand
returns the same request-bound value to the same member. API progress and errors
remain bounded and never reveal either raw value.

### T2c — Restored transfer-request classification

Own:

- restored-request classification in `src/rcp/server_ops/restore.py`; and
- `tests/test_project_transfer_request_restore.py`.

Classify every T2a state under O4's stopped-service restore path. A restored
nonterminal target request loses any old upload lease and in-progress machine
step, becomes **operator action needed**, and accepts only a fresh relay of its
already bound request/digest after both backends are revalidated. A source
request whose canonical home change already committed remains fenced; restore
never fabricates a release reversal or a second writable home. Re-entry is
idempotent and cannot expose a proof earlier than the original protocol state.

### T3a — Transfer archive manifest and explicit inventory

Own:

- new `src/rcp/transfer/__init__.py` defining the package boundary;
- new `src/rcp/transfer/archive.py` containing the versioned manifest
  and checksummed envelope models;
- transfer-specific diagnostic and streaming bounds in `src/rcp/limits.py`;
- a read-only inventory over current project/canonical/storage owners; and
- `tests/test_transfer_archive_manifest.py`.

Name every included record/file group and every excluded live binding before
copying bytes. The manifest carries project/source/target/request identity,
main and retained graph-branch canonical heads, schema/version, per-entry
size/hash, attribution mapping, and bounded diagnostics. Include immutable main
scope provenance/Patches and branch metadata/Patches/merge receipts through the
existing retained-history inventory. Carry the source `manifest.toml` as a
checksummed, non-published configuration-provenance entry; it is not immutable
canonical history and must never become the target execution manifest. Define
separate manifest groups for transformed RCP
chat JSONL, canonical Paper introduction, opaque `.research/facts/`, referenced
kept artifacts, and referenced legacy kept result views; exclude main and branch
materializations. Strip live resumption bindings, reusable stages, host/root
bindings, live continuations, temporary input attachment bytes, scratch/cache
pointers, credentials, and machine configuration. A test enumerates
project-scoped tables and durable file roots so a later schema addition fails
visibly until transfer policy classifies it.

The envelope has one control entry for T2a's raw source-release proof, bound to
the same source/target/request/project identity and included in the archive
digest. It is available only after T4a's source fence, is consumed by T4c before
project publication, and never becomes imported project history. The target's
raw activation proof is never an archive entry.

The manifest fixes an exact total size before transport, so upload and retained
failure copies are request-bounded without truncating scientific history or
inventing an arbitrary archive-size ceiling. Use fixed-size streaming buffers
and bounded diagnostic/query output from `limits.py`; preflight staging and
destination capacity, and never assemble the archive in browser memory or one
Python byte string.

This packet defines the codec and inventory only. It does not query provider
homes, copy operational records, or mutate the target.

### T3a-config — Rebuild target manifest without carrying source execution

Own:

- new `src/rcp/transfer/configuration.py` as the transfer-specific
  source/target configuration validator;
- narrow target-manifest construction through `src/rcp/setup.py` and existing
  `src/rcp/config.py` models; and
- `tests/test_transfer_project_configuration.py` with alias, path, provider,
  retained-history, and replay fixtures.

The source manifest is evidence about the history being transferred, not a file
to publish on the team checkout. Preserve every repository and machine alias
needed by historical Patches and `SourceRef`s, plus the canonical state
repository and initial/current truth-scope provenance. Rebind repository paths,
machine hosts/accounts, provider executable and native-source roots, profile
`run_on` choices, and other execution settings exclusively from the reviewed
target provisioning request. Source absolute paths and provider homes may appear
only in the non-published provenance entry and bounded transfer diagnostics.

For an incoming transfer, inspect any retained `.research` already present in
the prepared Git checkout. Accept it only when its project id, source home,
canonical heads, scope provenance, Patches, branch metadata, and merge receipts
are byte-identical to a prefix or the exact entries in the bound archive; no
archive-external canonical commit is permitted. An empty checkout is valid. A
different identity, renamed/missing historical alias, later head, unknown
durable entry, or byte conflict stops before mutation and is never archived,
overwritten, or treated as a new project.

Build the final target `manifest.toml` from that validated history plus the
reviewed target configuration and replay main and every retained branch against
it. Require the archive's source manifest and schema/codec to match T2a's bound
summary exactly. Produce one immutable configuration/readback receipt for T3f. This packet
does not publish files, insert rows, or activate the target.

### T3b — Finished operational record schema

Own:

- new `src/rcp/transfer/records.py`;
- the explicit positive/excluded table classification consumed by T3a's schema
  inventory check; and
- `tests/test_transfer_record_models.py`.

Define typed transfer records rather than a format that mirrors raw SQLite rows.
Map project id, human
attribution, attachment and kept-file references, timestamps, attempt lineage,
and foreign keys deliberately. Preserve immutable ids when globally safe and
record an explicit mapping where space-local ids can collide. Preserve the
current Paper draft, its base/ancestor content, and completed Paper-coach task
answers. Do not export `writing_sessions`: those bounded rows are native-session
Resume shortcuts, not the human-authored Paper or durable coach answer. Do not
export `chat_session_contexts` or another prompt/session checkpoint. No active
task, episode, watcher, provider stage, or executable retry/resume binding
enters the projection.

For the schema present when this handoff was fact-checked, the positive database
inventory is explicit:

- terminal `graph_runs` plus their `agent_usage`, events, receipts, outputs, and
  prompt/contract records;
- stopped or completed `watchers` and their displayed diagnostics, with no next
  check, delivery, continuation, or other executable wake binding;
- stopped `episodes`, sanitized `experiment_episode_state`, invocation rows,
  terminal report attempts, sanitized wrap-up rows, and completed reports;
- finished Auto-research episode metadata, invocations, messages, terminal
  recoveries, child Work attempts, terminal/cancelled child Experiments,
  experiment invocations, reflected/cancelled child admissions, acknowledged
  lifecycle notices, inbox/finish receipts, Apply results, and inert command
  records; and
- the current `paper_drafts` row, including ancestor/base conflict content.

Every later project-linked table fails T3a's schema-inventory test until this
packet classifies it as represented by a typed record or explicitly excluded.
This packet defines the format and classification only; it does not query a live
store, settle work, mark a target row history-only, or import anything.

### T3b-export — Finished operational database export

Own:

- new `src/rcp/storage/transfer.py`, mixed into `AppStore` through
  `src/rcp/storage/__init__.py`, as the one typed read-only project export over
  T3b's records;
- O3c's persisted `history_only` task marker and native-session fence; and
- `tests/test_transfer_records.py`.

Source transfer must first settle or terminalize any pending delivery,
recovery, child admission, report attempt, or watcher; it cannot omit the row or
copy it as runnable. Native-session ids, stage host/roots, execution authority,
pending wake fields, and wrap-up output paths are cleared even when the remaining
text is kept as historical display. Space users/tokens/sessions/invitations,
project membership/invitations/catalog aliases, provider-skill inventories,
`writing_sessions`, `chat_session_contexts`, disposable `result_views`, graph
watcher reconciliation watermarks, and any source-space provisioning or
machine-operation lease are not project history and do not transfer. The query
must match T3b's closed positive/excluded classification exactly.

Preserve safe artifact name/type/size/expiry metadata in the terminal task
record, but keep bytes only for descriptors with a validated `kept_filename`.
O3c's backend projection makes every other imported artifact visibly unavailable
without a stage URL or Open/Download/Keep/Revise action. Do not retain a source
stage binding merely to keep an artifact card looking live.

Imported terminal tasks are marked `history_only` in the target database through
O3c. The task projection publishes no executable continuation decision, and
every control endpoint rechecks the durable marker instead of trusting a client
or stale projection. Keep their honest succeeded/failed/interrupted status and
answers; do not relabel failure as success or synthesize an abandonment receipt.
Starting a new target task or a fresh provider session creates an ordinary new
row.
Keep the compound project query in the transfer storage owner rather than
teaching every existing lifecycle mixin a one-off archive API.

### T3b-files — Canonical human files, facts, and kept bytes

Own:

- new `src/rcp/transfer/project_files.py` as the transfer-only capture
  orchestrator;
- narrow typed export/read seams beside the existing chat owner in
  `src/rcp/service.py`, Paper owner in `src/rcp/paper/service.py`, and facts
  layout owner in `src/rcp/history/manager.py`;
- the existing named kept-artifact and kept-result-view readers in
  `src/rcp/transport/state.py` without adding a repository-directory walker;
  and
- `tests/test_transfer_project_files.py` with local and reachable-SSH state
  fixtures.

Parse each recognized canonical `.research/chat/*.jsonl` transcript and emit a
typed target transcript. Preserve stable RCP chat/message ids, human and agent
text, timestamps, provider/model/reasoning labels, graph-update receipts, and
display-only attachment name/type/size/expiry. Clear native provider session
ids and execution-machine/cwd fields, and deliberately remap only operation ids
whose terminal records come from T3b-export. The imported chat remains readable
and can start a fresh target-account provider session, but it cannot resume the
source session. Temporary attachment bytes and `chat_session_contexts` remain
absent.

Copy the optional canonical `paper/introduction.md` byte-for-byte, including a
version that differs from T3b-export's preserved draft. Copy every bounded safe regular
file under `.research/facts/` byte-for-byte as opaque project input. Resolve
kept filenames only from T3b-export's captured descriptors/rows, then read exactly
those artifacts and legacy result views through the workspace's named readers;
unreferenced human repository files remain ordinary checkout content and do not
enter the archive. A malformed canonical chat, unknown Paper entry, unsafe
facts entry, or missing/unsafe referenced kept file blocks transfer rather than
silently losing selected project history.

This packet captures transfer entries only. T3f owns target publication through
the same concrete owners.

### T3c — Provider-native selection and archive capture

Own:

- new `src/rcp/transfer/provider_history.py`;
- a narrow public original-source read seam and registry-driven root enumeration
  in `src/rcp/sources/indexer.py`, consuming the existing
  `ProviderProfile.session_roots` contract in `src/rcp/providers.py`; and
- `tests/test_transfer_provider_history_selection.py` with Codex, Claude, and
  local/reachable-SSH fixture provider samples, plus focused source-indexer
  regression coverage.

Reuse `ConversationIndexer` as the one existing owner of native-session
discovery, project-path matching, and local/SSH original retrieval. Select its
positively matched Codex/Claude sessions for the project's declared repository
aliases, explicitly excluding `app_chat` because T3b-files already carries RCP chats.
Copy each original native transcript file byte-for-byte into T3a's archive
entries and revalidate the copied file's recorded working path against the same
declared repository before admitting it, so a source rewrite between inventory
and copy cannot smuggle in another project. The transfer module must not
duplicate provider parsing, root traversal, SSH/rsync construction, or add a
provider-name branch.

Record bounded selected, skipped, unreadable, and byte counts. Unmatched,
rewritten, or unreadable files do not block transfer, and this packet adds
neither historical-checkout inference nor a human conversation-classification
workflow. Never slice at `last_refresh_at`; preserve that value only as the
target agent's overlap boundary. Do not feed raw files through the existing
lossy record normalizer or invent a provider-neutral transcript schema. This
packet reads provider homes for export but does not publish target files or
change Seed/Refresh. Discovery and copying execute on each saved source
profile's exact local or SSH machine/account through the indexer's existing
transport; they never read a different account's provider home, accept an ad
hoc host/path, or fall back to the desktop.

### T3d — Imported provider-source owner and local discovery

Own:

- new `src/rcp/sources/imported.py` as the sole safe-path, atomic-publication,
  and read-only discovery owner for durable imported provider sources;
- typed native-versus-imported source-root assembly in
  `src/rcp/agents/context.py` and `src/rcp/service.py`; and
- `tests/test_imported_provider_sources.py` with publication, local discovery,
  and negative Resume/Retry coverage.

The target stores imported bytes under the RCP-owned app-data root
`<RCP_DATA_DIR>/project-sources/<project-id>/provider-history/<provider>/`, with
content-addressed filenames and read-only modes. This is durable project source
data, not canonical `.research`, a Git checkout, a rebuildable cache, or a native
provider home. Seed and Refresh receive this root alongside the configured live
provider roots when they execute locally. Preserve a typed distinction between
native roots and imported project-owned roots inside the run context; do not
collapse them into an unlabelled list that T3d-ssh could accidentally copy from
a live provider home. T3f and lifecycle owners call this concrete owner rather
than walking the directory independently. Passive session ids, original paths,
and tool output inside the raw file remain historical bytes; prove that no file
is discoverable by provider Resume or Retry. This packet owns publication and
read-only discovery primitives, not transfer import orchestration or remote
staging.

Best-effort applies only to source-side selection before the archive is sealed.
Once a selected file is imported, a missing file, content-address mismatch,
symlink, special file, or unreadable owned root is durable project-source
corruption: Seed/Refresh must fail visibly rather than omit that source and
continue with a falsely incomplete corpus.

### T3d-ssh — Imported provider-source staging for remote Seed/Refresh

Own:

- narrow imported-source staging and effective-context rebinding in
  `src/rcp/runs/tasks/graph.py`;
- `src/rcp/transport/run_stage.py` only for the bounded immutable-directory
  fingerprint/readback seam the current stage owner lacks; and
- `tests/test_imported_provider_source_remote_staging.py` with fresh,
  interrupted, resumed, and clean-retry SSH drives.

When Seed or Refresh executes over SSH, copy only T3d's validated project-owned
imported-source inventory into that task's existing remote `inputs` stage before
the prompt and read-scope are finalized, then bind the effective imported roots
to those remote stage paths. Keep the configured remote provider-native roots in
place; never copy a local or remote live provider home, accept an arbitrary
source path, or create another SSH transport. Local execution continues to read
T3d's durable roots directly.

The staged directory is immutable task input, is fingerprinted against T3d's
bounded regular-file inventory, and follows the existing stage checkpoint and
retention lifecycle. Resume reuses and verifies the same staged bytes. A missing
or changed checkpoint fails visibly into the existing clean-retry path rather
than silently dropping imported history or recopying a different source. Record
the staged file/byte digest in the task receipt without exposing conversation
content or native provider paths. Preflight native and imported roots through
their separate typed owners; never send a server-local imported path to a remote
native-root probe or downgrade imported-source corruption into a provider-root
warning.

### T3e — Imported provider-source lifecycle integration

Own:

- explicit manifest/capture/readback integration in
  `src/rcp/server_ops/backup_models.py`,
  `src/rcp/server_ops/backup_capture.py`, and `src/rcp/server_ops/restore.py`;
- explicit rehearsal/checkpoint classification in
  `src/rcp/server_ops/update_checkpoint.py`;
- bounded cancelled/import-failure cleanup integration plus P6c guard
  preservation in `src/rcp/projects.py`; and
- `tests/test_imported_provider_source_lifecycle.py`.

Use T3d's concrete owner for every read, restore publication, and deletion. Add
the imported source root explicitly to encrypted backup/restore and the local
update checkpoint policy; do not walk a guessed directory or add a generic root
registry. Prove byte-for-byte backup/restore readback, candidate rehearsal and
rollback preservation, exact request-owned cleanup before activation, continued
ordinary-Delete refusal after team activation, and rejection of symlink or
special-file entries. Live provider homes remain excluded. This packet never
turns failure cleanup into a team-project deprovision path.

### T3f — Validated atomic target import and readback

Own:

- new `src/rcp/transfer/importer.py`;
- T3b-export's `src/rcp/storage/transfer.py` for the compound import transaction;
- atomic publication through `src/rcp/transport/state.py` and
  `src/rcp/sources/imported.py`; and
- `tests/test_transfer_import.py` with crash injection at every boundary.

Validate the complete T3a manifest, all T3b/T3b-export/T3b-files/T3c bytes and references,
T3a-config's target-manifest/readback receipt, canonical replay, and
excluded-field rules before target mutation. Stage files,
insert all selected rows in one SQLite transaction, publish each canonical,
kept, and provider-history group through its concrete atomic owner, including transformed RCP chats,
Paper introduction, facts, kept artifacts, legacy kept result views, and T3d's
imported provider sources. Publish only T3a-config's reviewed target manifest;
retain the source manifest as archive provenance, never as live configuration.
Never publish a raw source chat or create a target
`writing_sessions` row from transferred metadata. Record idempotent receipts,
and permit T4c activation only after database and every file-group readback.
Failure leaves one non-active repairable request and no partially visible
project.

### T4a — Source fence, exact export, and retirement receipt

Own:

- new `src/rcp/transfer/source.py` plus narrow calls into
  `src/rcp/projects.py` concrete owners;
- source-confirmation, request-bound export, and target-receipt routes in
  `src/rcp/api/project_provisioning.py`;
- T1 transition invocation and T3a, T3b-export, T3b-files, and T3c export orchestration;
  and
- `tests/test_transfer_source.py` with crash injection at every source boundary.

At source release confirmation, recheck source membership, linked request
identity, the target's bound human admission confirmation and unchanged reviewed
preparation revision, both proof commitments, the negotiated schema/codec, the
source manifest/configuration digest, and the source head. A changed source
configuration or incompatible target fails before the human release receipt,
home transition, or write fence and returns to preparation. First persist one idempotent human
source-release receipt. Then fence new source admission, settle
already-authorized work, append T1's home transfer with both human actors, and
export that exact accepted history/head. Publish the resulting head, archive
digest, and both confirmation identities in a separate idempotent source-fence
receipt. The export response is available only for that doubly confirmed request
and bounded archive described by its manifest.

Atomically seal that exact archive as a mode-0600 regular file at the
request-derived app-data path
`<RCP_DATA_DIR>/transfer-exports/<request-id>.rcp-transfer` before publishing the
digest receipt. Include T2a's raw source-release proof only in that sealed
post-fence control envelope. Once the receipt exists, every browser/native relay retry reads
and re-hashes this same file; a missing or corrupt sealed export is a loud
repair state, never permission to regenerate a potentially different provider
selection under the old digest. A pre-seal temporary file is request-owned and
may be replaced on idempotent retry only before any digest receipt exists.

Keep the retired source catalog row visible as transfer-in-progress until it
receives the matching durable T4c target-activation receipt, then retire it
idempotently and unlink only that verified request export. Ordinary project
Delete remains unavailable while the source fence/export is needed. A crash
after the home transition may leave no writable home and must resume the same
request and sealed bytes; never restore source write admission as a fallback.
This packet can prove its receipt boundary with a fixed target fixture and does
not own SSH transport or target mutation.

### T4b — Protected target upload lease and inbox

Own:

- new `src/rcp/transfer/target.py` for the bounded upload owner;
- F1's `project transfer-import` implementation in
  `src/rcp/server_ops/cli.py` and `src/rcp/server_ops/control.py`;
- explicit transfer-inbox classification in
  `src/rcp/server_ops/update_checkpoint.py`;
- and `tests/test_transfer_target_upload.py` with lease, size/hash, restart, and
  partial-file crash injection.

The service-account CLI asks the lock-owning server for the request's expected
digest, size, and one bounded upload lease. It accepts bytes only on stdin,
writes the request-derived mode-0600 same-directory `.partial` under
`<RCP_DATA_DIR>/transfer-inbox/`, and atomically renames it only after size/hash
verification. It accepts no archive path. Lease recovery is request/digest
specific; it may discard only that request's known incomplete `.partial`, never
walk or clean the inbox generally. The CLI never opens SQLite and a successful
upload is not project authority. This packet stops at one verified durable inbox
file and cannot import records, register a checkout, or activate a project.

### T4c — Target import, activation, and source receipt

Own:

- T4b's `src/rcp/transfer/target.py` for request revalidation and activation;
- narrow finalization calls into `src/rcp/setup.py` and
  `src/rcp/projects.py` concrete owners;
- T3f import/readback orchestration; and
- `tests/test_transfer_target.py` with confirmation, import, activation, and
  receipt crash injection.

Before import, the server revalidates the target member's admission
confirmation, the source owner's human release receipt, the later source-fence
receipt, their common linked request and archive identities, unchanged target
readiness, ownership, mode, both proof commitments, and T4b's complete upload.
It hashes the archive's raw source-release proof and requires the target's
precommitted value before any import mutation, then excludes the raw proof from
project publication. A successful CLI invocation is never a substitute for
either human confirmation.

After T3f readback, replay under the target `space_id`, register the prepared
central checkout, seat the target member, activate the project, and publish one
durable receipt bound to source/target/request/project/archive identities for
T4a. Only after that transaction commits may the target disclose its raw
activation proof through T2b's permanent-member-token-authenticated native route
to the exact target confirmer. It never writes the proof to CLI progress or a
browser-session response. The source accepts the receipt and retires its recovery
copy only when that proof hashes to its precommitted target value. Never activate
before the source home transfer commits. An arbitrary file, byte stream,
request id, serialized receipt, or CLI exit grants no import authority.

### T5a — Native transfer relay

Own:

- new `web/src-tauri/src/project_transfer.rs` for the fixed native relay;
- D6's `web/src-tauri/src/server_commands.rs` and
  `web/src/desktopRuntime.ts` only to admit the fixed
  `project transfer-import` command through its saved route;
- command registration in `web/src-tauri/src/commands.rs` and
  `web/src-tauri/src/lib.rs`, with the matching
  `web/src-tauri/capabilities/main.json` and generated permission entries; and
- Rust and source-built desktop/SSH relay tests.

After both human confirmations and the source-fence receipt, the personal
backend exposes the one request-bound export stream; it never exposes another
project's app-data path. The Tauri shell streams
those bytes into the stdin of one system-SSH child whose fixed remote argv is
`rcp server project transfer-import <validated-request-id> --machine-readable`.
That remote CLI alone owns the target's derived mode-0600 inbox path and atomic
`.partial` rename; Tauri never constructs an `scp`, `mv`, archive path, or remote
shell pipeline.
In the automated relay, archive bytes, provider records, and credentials never
enter a shell string, command argument, Web storage, or log, and no target
archive path is supplied. The target never receives a personal-space credential,
and the source never receives a team credential.
Keep both raw transition proofs inside Rust-owned streaming/response state. The
source proof reaches the target only inside the post-fence archive; the target
proof is fetched after committed activation through the team tunnel using the
saved permanent member token already held in Keychain, then posted directly to
the pinned personal backend for commitment verification. After source cleanup,
relay its public acknowledgment back so the target can erase the raw value.
Neither proof crosses Web JavaScript or Tauri IPC, appears in CLI progress, or
survives after its consumed receipt and required recovery copy are settled.

Reuse the existing native artifact-download shape: Rust re-verifies the pinned
personal backend instance, requests the confirmed export itself, and pipes
bounded response chunks directly to SSH stdin. The Web command supplies only the
validated request id and receives only progress/result metadata; archive bytes
never cross Tauri IPC or enter browser memory.

If the saved operator route cannot perform that fixed relay, export one
mode-0600 local file and show exact bounded Terminal commands after a human
action; never collect a password or private key. An interrupted copy resumes or
restarts against the same digest and request. Success removes the protected local
export and target staging bytes only after target readback; failure retains one
bounded diagnosable copy at each side, and cancellation uses explicit safe
cleanup rather than recursive guessing.

That protected local export is the desktop's manual-relay copy. T5a never
deletes T4a's personal-backend sealed source archive; T4a alone removes it after
the matching durable target-activation receipt.

This packet owns only the native byte relay and its protected local staging
behavior. It may be driven with a fixed test request and archive; it does not
own the transfer screen or decide transfer lifecycle state.

### T5b — Transfer UI and crash-recovery drive

Own:

- new `web/src/components/ProjectTransfer.tsx`;
- the **Move to team space** entry in `web/src/views/ProjectSettings.tsx`,
  `web/src/components/ProjectProvisioning.tsx`, `web/src/api.ts`,
  `web/src/types.ts`, and `web/src/App.tsx` integration; and
- browser and source-built desktop recovery tests.

Show source and target absolute paths, what stays owned by the person, central
ownership, active work to settle, execution settings to re-establish, and the
settled archive contents/exclusions. Provider matching is automatic; keep its
bounded selected/skipped summary in the transfer details and do not add a
transcript-selection UI. No confirmation before target **ready for review**.
One final review action records the target-space admission first and the
personal-space release second through the two existing authenticated sessions;
it neither shares credentials nor conflates their actor ids. If interruption
leaves only target admission confirmed, show that exact state and resume the same
request instead of asking the target to confirm again. The UI may invoke only
T5a's fixed native relay. It renders the durable backend request state and next
action, never derives transfer success from a CLI exit or native-process output.

Drive interruption between the two human confirmations and after every T4a,
T4b, and T4c boundary, reload both spaces, resume the same request, and prove there is
never more than one writable home and never target activation with only one
confirmation.
Team-to-personal and team-to-team remain absent.

## Closure packets

### V1 — Genuine one-lab live drill

Use a fresh Linux server/VM, two distinct human desktop identities, a disposable
GitHub repository, one local provider, one reachable SSH provider target where
available, an off-server `age` recovery identity, and a fresh restore host/data
directory.

Run the full lab drill on one supported Ubuntu release and retain separate
install, service-start, doctor, update, and restore evidence on both Ubuntu 22.04
and 24.04 x86-64. A generic `ubuntu-latest` result alone does not establish the
two-release support claim.

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
8. decryption and restore on the fresh target, fresh-key Git reconstruction
   before canonical publication, complete task/episode/watcher/recovery
   detachment, replayed heads, readable history before provider login, visibly
   blocked execution until native login/recheck, and explicit old-authority
   exclusion;
9. console member removal with preserved history; and
10. personal-to-team transfer and crash recovery after every transfer packet
    lands, including restart from the same digest-bound sealed source export;
    read the transformed RCP chats, both Paper draft/canonical sides,
    facts, kept artifact, and legacy kept result view; prove no source chat or
    Paper native session can resume; then run local and reachable-SSH target
    Refresh drives that read imported provider history and new target-account
    logs without copying a native provider home. Back up and restore that project
    and repeat the file/read/imported-history checks.

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

- S95, S102, S103, S104, S105, S122's refined team boundary, and S128 are
  implemented with current evidence;
- S98 is implemented with current evidence;
- a fresh source checkout can install and update the Linux service entirely
  through the documented CLI/bootstrap path;
- two source-built desktop members can use personal and team spaces without
  session collision or local team fallback;
- a team project can be prepared, reviewed, created, executed locally/remotely,
  backed up during work, restored, and transferred without losing its canonical
  chats, Paper draft/introduction, facts, referenced kept files, or the complete
  provider histories that transfer positively selected and imported;
- machine-only operations are absent from member HTTP authority;
- every secret and account boundary above has a negative verification; and
- the genuine live lab drill passes and is documented without credentials.

## Suggested skills for pickup

- The design grilling and final cross-document fact-check are complete. Dispatch
  G0, then G1; all decisions are settled. Implement the remaining packets
  without reopening product boundaries unless current code contradicts their
  authority.
- Use `computer-use:computer-use` for the real source-built desktop drives in
  D2, D4a, D4b, D6, D7, T5a, T5b, and V1; browser tests cannot prove native SSH,
  Keychain, cookie-store, or navigation behavior.
- Use `codex-security:security-diff-scan` after the credential/control-socket,
  SSH bridge, backup, and restore packets, scoped to their actual diffs.
