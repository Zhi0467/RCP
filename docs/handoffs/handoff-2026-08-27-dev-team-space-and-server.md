# Dev team space and source server completion handoff

Date: 2026-08-27
Status: active. Design, grilling, and all planned implementation packets are
complete. The two-release disposable server lifecycle and fresh-host restore
qualification pass. A persistent Ubuntu 22.04 lab server now passes clean
source install, continuous update, member enrollment, provider readiness, and
real GitHub-backed project creation. The two-member desktop switching, first
task, SSH execution target, backup/restore, transfer, and complete one-lab
closure drive remain.

### Packet status

"Done" means implemented and verified hermetically. A packet in the third column
is written and passing its own tests, but still owes a drive on real hardware.

| Lane | Done | Implemented, drive still open | Not started |
| --- | --- | --- | --- |
| Gates | G0, G2 | — | — |
| Server foundation | F1, F2, F3a, F3b, F4, F5, F6a, F6b, F6c, F6d | — | — |
| Provisioning | P1, P2, P3, P4, P5, P6b, P6c | P6a | — |
| Desktop | D1, D2, D3 | D4a, D4b, D5, D6, D7, D8 | — |
| Backup and restore | O1, O2a, O2b, O3a, O3b, O3c, O3c-ui, O3d-a, O3d-b, O4a, O4b, O4c, O4d | — | — |
| Member removal | O5a, O5b | — | — |
| Server settings | O6 | — | — |
| Transfer | T1, T2a, T2b, T2c, T3a, T3a-config, T3b, T3b-export, T3b-files, T3c, T3d, T3d-ssh, T3e, T3f, T4a, T4b, T4c | T5a, T5b | — |
| Closure | — | — | V1, V2 |

What each open drive is waiting on:

- **P6a** — the reachable-SSH checkout/provider half of the composed live
  qualification; the server-local GitHub path now passes on the persistent lab
  server.
- **D4a, D4b, D5, D8** — the integrated source-built two-space desktop drive,
  including the coordinated protocol-1 cutover.
- **D6** — the exact source-built desktop click through both fixed operator
  routes. The persistent lab host now has the installed `rcp` account and CLI;
  direct operator-terminal provisioning passes, but that is not evidence for
  the native desktop launcher.
- **T5a, T5b** — the native relay, unified move wizard, and restart recovery are
  implemented and hermetically verified. They still need the S98 source-built
  desktop drive against two real spaces and a real SSH operator route.

The dated implementation log and the fifty-five completed packet sections moved
to [the evidence archive](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server-evidence.md)
on 2026-09-01; only packets with an open drive remain below. The table above and
this opening status are the authoritative current summary.

The previously planned G1 pull-request transition before implementation was
rejected by the human. Direct commits remain in force through this still-open,
unstable server-stabilization slice. Archiving this handoff ends that exception;
the next change uses a short-lived branch, PR CI, and explicit human merge.

#### 2026-09-01 — persistent lab install and update qualified

- The first manual install on Ubuntu 22.04 x86-64 at `wth-gpu-01` completed from
  the private source repository. It created the dedicated `rcp` account, managed
  checkout and immutable release, initialized team space `WTH UCSD`, enabled the
  loopback service, and passed HTTP health plus `server doctor` with zero
  projects and no active work.
- Updating that fresh server from `e1ffb9a` to `fba88c4` failed safely during
  copied-state candidate rehearsal. The retained result said the copied database
  was not a usable enrolled team space. The old release remained active and
  unchanged.
- This was an RCP defect, not bad server data. Team initialization intentionally
  creates a valid space before the first member enrolls, while rehearsal
  incorrectly required an enrolled user. Candidate rehearsal now accepts this
  state, proves health, proves unauthenticated project access remains forbidden,
  and verifies the empty project inventory. It still refuses a non-team space or
  any captured project without a usable member principal.
- The ordinary terminal renderer no longer dumps the complete plan or one block
  for every running and completed step. A TTY keeps one colored current-step
  line and expands only a stop/final result; child build/source output is
  captured. `--machine-readable` retains the complete bounded JSON event stream.
- Operator boundaries now form one continuous CLI wizard. Enter runs declared
  command actions and exact re-entry in the same terminal; `q` or EOF pauses
  safely with the printed Continue command. Team initialization has a distinct
  second pause so the one-time enrollment code is saved before activation.
  Structured mode never prompts or executes those actions.
- Install/update failures now carry explicit correction guidance, an exact
  diagnostic rerun, and the exact normal retry. Failed candidate rehearsal also
  resolves only the new retained rehearsal root, surfaces its bounded diagnostic
  and result path, and prints exact inspection and cleanup commands. A stale
  update confirmation restarts unconfirmed target review instead of reusing the
  stale commit.
- Hermetic focused coverage passes. The warning-containment correction was
  shipped and live-updated before the replacement bootstrap member enrolled.
- The old empty installation was stopped, captured in the operator-owned mode
  `0600` archive
  `/home/zhiwang/rcp-server-pre-clean-reinstall-20260901T0430EDT.tar.gz` with
  SHA-256
  `af8848541ec7ff1b2df7bf49f6ea88ae6e2d1bcfd315acfcceca4ca5171f4190`,
  and removed. A clean real-TTY install at `792f14f` then completed on
  `wth-gpu-01`: the continuous wizard paused for the read-only private-source
  grant, verified GitHub host trust, resumed in place, built the immutable
  release, initialized the new zero-member `WTH UCSD` team space, enabled the
  service, and read back HTTP health with zero projects and zero active tasks.
- That drive also exposed a documentation seam rather than a server-runtime
  defect: a newly opened operator tmux shell restored NVM Node 18 ahead of the
  qualified system Node 24 before the disposable bootstrap build. The setup
  guide now renews the fixed system PATH again at the build step and verifies
  `node --version` before npm runs.
- The same zero-member team then updated from `792f14f` to `2a13e0d` through
  the real TTY flow. Exact-target confirmation resumed in place; the copied-state
  rehearsal passed with no enrolled user and no project, the rollback boundary
  was verified, systemd cut over, and normal `server doctor` reported healthy
  while keeping its final field list bounded. This closes the original
  fresh-team rehearsal defect on persistent hardware.
- One Starlette test-client deprecation warning still escaped while the update
  coordinator imported the rehearsal module. The test client is now imported
  only inside the already-captured candidate child, with a subprocess regression
  proving the operator-facing coordinator import does not load it. The later
  live update kept the rotating operator line clean.

#### 2026-09-01 — persistent lab server and first project qualified

- The clean source installation on `wth-gpu-01` is a healthy enabled team
  service named `WTH UCSD`. The persistent source, immutable release, running
  process, Web build, private control socket, and upstream `main` identities
  read back aligned through `server doctor`.
- One local desktop member named `Zhi` is enrolled through saved connection
  `7032a85b-b1bb-4014-b820-6704fa6d219a`. Its permanent member token is in the
  versioned Keychain service, not the connection registry, URL, logs, or server
  source tree. The one-time bootstrap inputs were removed after exchange.
- The server-local `rcp` account has provider-native Codex authentication and
  all six configured provider profiles pass readiness. RCP neither created nor
  retained a provider credential in its database; the operator used the
  provider's native headless-account mechanism.
- The real request `acfb2ef4-b8de-4022-842e-91ab428abdb5` completed as project
  `dark matter denoising` (`1c2e93b5-7639-4206-afee-8d582e7f993c`) from
  `git@github.com:Zhi0467/TIDMAD-denoising.git`. The repository was genuinely
  empty, so the authorized human workflow created and pushed its visible root
  commit `45fbc8056c30b1ab4e3995babbded0098e073e0a` before RCP resumed. GitHub
  then passed the request-scoped write, readback, and cleanup proof through a
  distinct write-enabled deploy key; RCP cloned the exact commit into its
  `rcp`-owned central checkout and completed final member review.
- Project finalization created canonical `.research/` state after Git readiness.
  That state is intentionally not an RCP-authored hidden Git commit. Its
  untracked appearance in the central checkout is therefore current project
  state, not clone divergence and not permission to reset or delete it.
- The drive found four RCP defects and fixed them on `main`: server-local steps
  no longer try to `runuser` from `rcp` to `rcp`; the continuous wizard strips
  an impossible same-user sudo prefix; an empty-repository pause retains the
  planned machine target and reads the one briefly shipped compatible action
  shape; and installed control failures now reach the operator log with their
  classified error. Focused provisioning, Git-credential, CLI, and control
  regressions pass.
- The local HTTPS source client now uses one-label wildcard hosts below
  `rcp.localhost`, migrates the exact previously shipped saved origin, records a
  365-day certificate lifetime, and rotates during the last seven days. The
  version-4 leaf explicitly carries the server-certificate extensions WKWebView
  requires; startup atomically reissues the earlier extension-less version-3
  identity. The installed bundle grants manual trust only for `rcp.localhost`
  subdomains; exact saved-origin navigation and the evaluated certificate pin
  remain separate fences. The retained real WKWebView probe passes with two
  isolated team origins and an unpinned-certificate refusal, and the installed
  source-built app visibly loads the enrolled team card and opens its real
  project. S105's exact two-team-space switching drive remains open.
- This closes the first persistent server/member/project slice, not V1. A first
  real provider task, second human, reachable SSH execution account,
  backup/restore under project state, transfer, member removal, and the exact
  visible multi-space drive remain the next live closure work.

#### 2026-09-02 — provider maintenance uses the server CLI

- Server operators no longer need a direct `rcp` login to update Codex or
  Claude. `sudo rcp server provider update <codex|claude>` runs the provider's
  supported native update under the service account, bounds diagnostics, and
  verifies the resulting executable, version, and native authentication.
- The service and maintenance subprocess PATH now prefer
  `/home/rcp/.local/bin`. Local provider discovery preserves a stable command
  symlink instead of pinning a project to its current versioned target. Projects
  that already stored a versioned path need one normal **Resolve** in Settings;
  future updates retain the stable path.
- The persistent lab server still needs the new source commit installed and the
  real Codex/Claude wrapper commands driven before this live qualification is
  closed.

### Notes carried from earlier status updates

D2 promotes the proved local-HTTPS mechanism into the production source-built
desktop: one sealed identity with its short key in Keychain, canonical
connection-bound origins, an app-scoped WKWebView pin, and exact saved-origin
navigation are implemented and live-verified on this host. D3 adds the real
system-SSH forward, local TLS proxy, reuse/backoff, and owned-child cleanup.
The passing live Ubuntu 22.04/24.04 install, doctor, update, backup, and
fresh-host restore drives are recorded below.
F6a is pushed at `fff75c3` with exact-target confirmation, an immutable
built-candidate receipt, and an unchanged live-service boundary. P2 now
provides the member-authorized provisioning API, backend-owned project-creation
and lifecycle answers, sealed Web response vocabularies, and a
fail-before-input guard on all three ordinary existing-checkout entry routes.
It performs no machine work and does not create a team project. P4 now supplies
the exact-account local/SSH central-checkout primitive, path receipts,
retained-research refusal, and backward-compatible project-configuration
persistence. P6a now implements the next provisioning boundary by durably
composing P3-P5 through the installed service and stops at **ready for review**
without creating a project; P6b now owns the exact final human confirmation,
reserved identity append, registration, and crash recovery without rerunning
machine preparation. F6b now consumes O2a/O2b to rehearse the candidate against
copied real state under one reusable startup-effect fence. F6c's current-owner
checkpoint core now publishes and temp-restores one exact local rollback
boundary with crash-safe replacement journals. T3e preserves imported provider
histories through that checkpoint. T4b/T4c preserve only receipt-backed complete
transfer inbox files and ignore already-consumed uploads, while refusing any
leftover untyped bytes. P6c now publishes and independently enforces the
ordinary team-project deletion guard through the card, Web, API, and catalog.

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
  repository-scoped write deploy key and a canonical GitHub.com repository
  identity;
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
- [Projects, spaces, and operations](../specs/projects-spaces-and-operations.md)
  and [Server and machine operations](../specs/server-and-machine-operations.md);
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

The remaining seams are also concrete:

- the strict `rcp server` shell, Linux layout, source installer, private control
  socket, installed doctor, source-update candidate builder, protected backup
  run/configuration/status/retention path, online SQLite capture, optimistic
  project-file capture, copied-state update rehearsal, and the coherent local
  update rollback checkpoint now exist; cutover/post-switch rollback
  coordination and recovery passed their live Ubuntu drive;
  restore now validates and installs one stopped, detached SQLite candidate,
  creates fresh per-repository keys, reconstructs exact local or SSH checkouts,
  rebinds the stopped catalog, and publishes/replay-verifies protected project
  state, requires digest-bound old-authority and retained-member reviews, and
  activates only through a root-only private handshake behind closed HTTP,
  background, and startup-effect gates. Its fresh-host Ubuntu 22.04/24.04 drive
  passes at the exact current implementation boundary recorded below.
  Member removal now provides an exact confirmed inventory, one atomic access
  fence, graceful task/episode draining, durable tombstones, and startup/CLI
  reconciliation;
- `default_data_dir()` still falls back to the macOS Application Support path;
  a Linux service works only through an explicit `RCP_DATA_DIR` today;
- the desktop now implements distinct pinned HTTPS origins, exact SSH tunnel
  ownership, native enrollment/session exchange, permanent-token storage,
  multi-backend navigation, a personal-first multi-space index, and the fixed
  project-provisioning operator bridge; its combined D4-D5 path still needs the
  pending visible two-team-space drive recorded in S105, while D6 still needs a
  live server with an installed `rcp` account;
- the durable project-provisioning request, complete finalizer, member API,
  repository-credential primitive, exact-account provider check, checkout
  preparation, machine-step orchestration, and unified personal/new-team
  project wizard exist. The transfer archive, machine relay/decode, activation,
  cleanup orchestration, unified move wizard, and restart coordinator also
  exist. Post-setup cancellation and complete live team qualification remain
  open, including the real source-built transfer desktop/SSH drive.

The repository's current `AGENTS.md` prescribes direct work on `main` through
this handoff's exact closure. G0 restored the baseline and G2 added the old-data-
to-candidate upgrade gate. CI reports post-push `main`, but GitHub branch
protection remains the explicitly deferred public-sharing gate described below.

### Resolved repository workflow boundary

A read-only GitHub fact-check on 2026-08-28 confirmed that `Zhi0467/RCP` is
private and its current plan rejects the branch-protection API with HTTP 403,
stating that private-repository protection requires a plan upgrade or a public
repository. The human chose not to change the repository's plan or visibility
and explicitly retained direct work on `main` through this first server's
stabilization and closure. Each packet receives focused tests, pre-commit, and
code review; full desktop/live drives occur at meaningful milestones. CI reports
pushed failures but does not technically reject a direct push, and the evidence
must not imply otherwise.

Archiving this handoff is the workflow transition. The next change uses a short-
lived branch, PR CI, and explicit human merge even if the repository remains
private and the rule is still convention-only. There is no permanent `dev`
branch, and the server continues to consume only merged `origin/main`.

Before RCP is shared publicly or with external users, make the repository public
and enable real `main` branch protection. Make the already-adopted PR workflow
technically require the named build, test, and upgrade-compatibility checks,
reject direct pushes and failed or missing checks, and record a live enforcement
proof. That enforcement gate is outside this one-lab team-server slice.

### Final planning-audit evidence

The 2026-08-28 final audit re-read the current design/spec/acceptance/decision
set and the complete handoff against the live tree. It reconfirmed the current
CLI/UI/data-directory seams above and reran the 93 focused foundation tests and
the eight documentation tests. It also found the current `c0909b6` baseline is
not green: the complete backend suite has two deterministic failures, and
`pre-commit --all-files` reformats seven tracked source/test files. G0 records
those exact repairs instead of letting the first implementation worker inherit
an unexplained red tree.

After adding G0, the audit checked that the original 66 packet headings had
exactly one dependency-table entry with no duplicate id, missing/unknown
predecessor, or cycle. The human later rejected G1; the active plan now has 65
packet headings, 63 assignment packets with concrete `Own:` blocks, and V1/V2 as
integrator closure drives. The dependency table has 64 rows because one row
covers both F1 and D1.
The audit also verified that every later owner of a not-yet-created shared file
depends on its creator; it added the missing F6a-to-O4a and O4a-to-T2c edges.
Repeated existing paths remain covered by the shared-file scheduling mutex below. The
provider-auth boundary, transfer/restore artifact decisions,
team-deletion boundary, restore journal, and shared-file scheduling mutexes are
explicit rather than left to worker interpretation. G0 is dispatchable now. The
repository workflow decision is settled, so no unresolved product or repository
decision blocks the feature lanes. Q10 and the later public branch-protection
gate are deliberately future work and do not block this plan.

## What remains

The planned code paths are implemented, and the disposable two-release server
lifecycle plus fresh-host restore now have live evidence. Closure still
requires the remaining real-environment evidence:

1. extend retained live evidence through the complete provisioning command,
   S104's concurrent/partial SSH backup cases, and both fixed operator routes;
2. drive the unlocked rebuilt source desktop through two distinct member
   identities, personal/team switching, Keychain readback, SSH/TLS isolation,
   new team project setup, and personal-to-team transfer/restart recovery; and
3. complete V1 end to end with the disposable GitHub repository, local and
   reachable-SSH provider accounts, then update acceptance evidence, rerun the
   final clean-tree baselines, and archive this handoff through V2.

The current shared lab host cannot substitute for item 1 without installing the
dedicated account and service, and the previously proposed privileged container
would expose host cgroups and a temporary public SSH port. Do not use that
workaround without explicit human approval.

## Running the remaining live work

### Order

1. **Hosted CI first — complete at `112e0dc`.** The Ubuntu 22.04/24.04 matrix in
   [`server-install-live.yml`](../../.github/workflows/server-install-live.yml)
   now proves the Linux source install, service, update, forced rollback, deploy
   keys, protected backup, and O4d fresh-host restore without using anyone's
   hardware. Run 33456906376 is green on both supported releases. Do not stack
   new live work on a later red matrix.
2. **Then one named persistent disposable or dedicated Linux host.** Hosted
   runners cannot stay reachable from the source-built Mac desktop long enough
   to qualify D6, D7, D5, or P6a's real SSH/provider half. Bootstrap that host
   before V1, not during.
3. **Then V1** as one coherent drill, then **V2** baselines and archive.

### Authorization during live work

The hosted workflow is part of this goal and can be driven without another
pause. Before first touching a human-owned host, name the exact host and confirm
that it is disposable. That approval permits the agreed installation rehearsal,
including its required SSH and sudo commands; it is not limited to read-only
inspection and does not extend to another host or unrelated machine work. The
shared production lab host is not the first target because V1 deliberately
mutates and restores its host.

At that pause, also confirm the bootstrap clone's origin:
`discover_bootstrap_repository` records the update source from that checkout,
so `rcp server update` will pull the same GitHub repository afterwards.

If a host has to be torn down, RCP has no uninstall operation; the five-command
operating-system sequence is in
[the operations spec](../specs/server-and-machine-operations.md). On a
disposable host, restoring the snapshot is faster and needs no teardown.

### Human-authority steps inside the drive

The agent can drive the approved host setup and the surrounding CLI/UI checks.
The human still performs team-space initialization and any provider's native
login, because RCP never accepts provider credentials. The CLI pauses with the
exact account and resume command; after the human finishes the native action,
the agent can run the recheck and continue. Provider login is not required to
install or enroll in the server, only to qualify server-side agent execution.

Two deliberately separate RCP member identities on one test Mac can prove
credential, origin, cookie, and attribution separation. That is a technical
two-member qualification, not evidence that two different people participated.
`CodexProfile.is_authenticated` already explicitly rejects `Not logged in` and
has a focused regression; do not carry the earlier substring concern forward.

## Settled decisions

These record the drive-specific commitments for this slice. Where a dedicated
decision record exists it owns the rationale and wins on any conflict:
[source update channel](../decisions/2026-08-27-main-is-the-server-update-channel.md),
[install and update privilege](../decisions/2026-08-27-source-server-install-and-update-privilege.md),
[schema compatibility](../decisions/2026-08-27-server-schema-compatibility.md),
[transfer archive](../decisions/2026-08-27-personal-to-team-transfer-archive.md),
[desktop local HTTPS origins](../decisions/2026-08-30-desktop-local-https-origins.md),
and [team-shell handshake compatibility](../decisions/2026-09-01-team-shell-handshake-compatibility.md).
Keep the concrete matrix, account, and drive facts here; do not restate rationale
that a record already owns.

### Deployment and source update

- First target: one lab, one Linux server, one team space.
- Supported server matrix: Ubuntu 22.04 LTS and Ubuntu 24.04 LTS on x86-64 with
  systemd. Other distributions and architectures are explicitly unverified.
- Server builds pin Node.js 24 and Python 3.12 through `uv`, with Git, OpenSSH,
  and a supported `age` CLI as prerequisites. The operator guide gives tested
  commands for both Ubuntu releases; `rcp server install --team-name "<team
  name>"` validates but does not modify apt repositories or install general OS
  software.
- Server and desktop are built from source. No Linux RCP package, container,
  release binary, or hosted deployment is required.
- A normal operator creates the disposable bootstrap checkout and runs its
  source setup without privilege. The first privileged RCP command is that
  checkout's absolute `.venv/bin/rcp server install --team-name "<team name>"`
  path under `sudo`.
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
- The configured `origin/main` commit is trusted host code. Build steps run as
  `rcp` and therefore are not isolated from other state owned by that account.
  Candidate rehearsal is a functional and accidental-effect fence, not a
  defense against a malicious or compromised source commit. Before wider
  sharing, protected human-reviewed `main` is therefore part of this trust
  boundary.
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
- This first-team-server stabilization and closure stays directly on local
  `main`; archiving this handoff ends that bounded exception.
- Each scoped packet receives focused tests, pre-commit, and code review for
  coverage, edge cases, and stale docs. Full source-built desktop and machine
  drives run at meaningful integration milestones rather than after every
  file-sized packet. Surprises, unrun checks, and confidence gaps are recorded
  in the implementation log instead of being hidden.
- CI reports pushed `main` but GitHub does not technically reject a bad direct
  push. Commit and push remain separate human-authorized actions under
  `AGENTS.md`; test success is not permission to push.
- From the first team-server-capable commit onward, every earlier server-era
  persistence boundary remains directly upgradeable. CI retains one immutable,
  sanitized fixture bundle per distinct schema or migration-semantics boundary;
  fixtures do not expire merely because they are old.
- Local Web and desktop development may run any branch, but this stabilization
  remains on `main`. The next change after closure uses a short-lived branch,
  PR CI, and explicit human merge. Emergency fixes use the workflow then active.
- There is no permanent `dev` branch.
- Before public or external sharing, make the repository public and enable real
  branch protection that technically requires the already-adopted PR workflow's
  named jobs and rejects direct pushes and failed or missing checks. This later
  enforcement gate is not part of the one-lab closure condition.

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
- The CLI prints a numbered, plain-language plan before machine work. Every step
  names its purpose, `performed_by` responsibility, typed target, state, and
  expected success. Machine targets name host and OS account; external-service
  targets name service, resource, destination URL, and required authority role
  without inventing a human identity. An operator-action result additionally
  carries ordered safe commands or external UI actions, nonsecret values, plain
  success signals, and the exact recheck or resume command. System-owned steps
  execute their internal commands themselves; a human never has to infer an
  omitted action from status prose. Interactive and machine-readable modes carry
  the same information; the wizard never owns a machine instruction absent from
  the CLI or parses CLI prose to reconstruct one.
- Do not add an application CLI for graph, chat, task, episode, or ordinary
  membership actions.

### UI, desktop, and CLI coordination

- One visible project wizard owns three plainly named intents: **Use an existing
  checkout personally**, **Create a shared team project**, and **Move an existing
  personal project to a team**. Context may preselect an intent; Project Settings
  opens the same wizard in move mode. Separate backend authority paths remain
  behind that shared presentation.
- New-team mode accepts the two documented GitHub.com URL forms and execution
  placement, not a member checkout to move or upload. A local-only codebase must
  first be pushed by the human through their ordinary GitHub workflow to a
  repository with a real commit. RCP creates neither the GitHub repository nor a
  user login/token.
- **Move an existing personal project to a team** is available only in the
  source-built desktop because it coordinates two authenticated backends and the
  native archive relay.
- The backend persists the request before machine work and owns these displayed
  states: **waiting for server setup**, **setup in progress**, **operator action
  needed**, **ready for review**, **completed**, and **cancelled**.
- The Web UI renders backend decisions. It never infers readiness from Git files,
  subprocess output, or a zero CLI exit code.
- The CLI owns the exhaustive machine workflow and prints its numbered plan up
  front. For team machine preparation, the wizard is the graphical presentation
  of that same CLI-owned operation: it may invoke the fixed command and render
  the same structured steps and progress, while a browser shows the same
  copyable command and operator actions. The CLI remains complete without the
  wizard, and neither surface has a private setup recipe.
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
- Each saved space receives a stable pinned HTTPS origin under
  `*.rcp.localhost`; different ports on `127.0.0.1` are forbidden as isolation
  because cookies ignore ports. The exact hostname, certificate, and Keychain
  scheme are owned by
  [the desktop origins decision](../decisions/2026-08-30-desktop-local-https-origins.md).
- The native shell owns SSH tunnel lifetime, health/`space_id` plus live
  team-shell protocol negotiation, token exchange, WebView session
  establishment, and origin navigation. It stores no compatibility floor.
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
- GitHub Enterprise, arbitrary Git hosts, or member-supplied trusted origins;
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
packets in one recorded implementation slice while retaining their separate
file ownership and checks.
`main` must remain deployable after every recorded implementation slice.

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
| G2 | G0 | none |
| F1, D1 | G0 | none |
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

G0 starts directly on `main`. After it is green, G2, F1, and D1 can proceed,
subject to the shared-file mutexes below. After F1 and G2, P1 can proceed
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

## Completed packets and implementation evidence

The dated implementation log and all fifty-five completed packet specifications
moved to [the evidence archive](../archive/handoffs/handoff-2026-08-27-dev-team-space-and-server-evidence.md) on 2026-09-01. Read them there for what a
finished packet built and how it was verified; they are evidence, not authority.
The gate, server-foundation, backup/restore, member-removal, and server-settings
lanes are complete in full and have no section below.

Only packets whose drive is still open remain here.

## Provisioning packets

### P6a — Server preparation orchestration

Status: implemented hermetically on 2026-08-29; the complete numbered command,
private-control plan/step protocol, durable P3-P5 composition, recovery tests,
and installed-app socket regression pass. The packet remains live-unqualified:
no complete source-built team-service run has yet driven this composed command
against its disposable GitHub repository and reachable SSH target. P6a creates
no project; P6b remains the only finalizer.

Own:

- new `src/rcp/server_ops/project_provision.py`;
- narrow registration in `src/rcp/server_ops/cli.py` and
  `src/rcp/server_ops/control.py`; and
- `tests/test_team_project_provisioning.py` for machine preparation and recovery.

Run P3–P5 as resumable named steps and publish every result through P1. A zero
process exit cannot skip durable status readback. Crash at every preparation
boundary in a parameterized test. Before confirmation there is no canonical
project, and this service-account command has no route that can create one.
The interactive command is sufficient to complete every machine step without
the wizard: at each pause it prints the concrete account/action/success/resume
contract. The desktop is a structured renderer and fixed launcher for that same
workflow, not a second implementation.

The implementation publishes separate durable boundaries for entering setup,
preparing each repository key identity, proving Git write access, preparing
each central checkout, checking each provider profile, and binding the final
review. Every effectful step rechecks the request revision/status and target
digest before work. Resume replays already-published answers without duplicate
authority, including a crash after every boundary. Unsafe credential ancestry,
missing GitHub write grant, provider-native login, retained RCP research, key
rotation after write proof, and incomplete legacy configuration all stop with
one exact human target/action and the same request-bound resume command.

The CLI refuses a successful exit before emitting its final success event when
the installed service does not read back **ready for review**. Provider-only
machines do not invent checkout roots, while multiple repositories on one
machine may publish their paths one at a time. The coordinator explicitly
refuses completed, cancelled, stale, already-created, or cancellation-handling
requests instead of clearing or reinterpreting them. Post-setup cancellation is
still unfinished product behavior and remains part of S128 rather than a hidden
cleanup path in this command.

## Desktop packets

### D4a — Team handshake and WebView session establishment

Own:

- new `web/src-tauri/src/team_session.rs`;
- narrow command registration in `web/src-tauri/src/commands.rs`;
- Keychain calls through `web/src-tauri/src/team_connections.rs`; and
- focused Rust tests plus one live enrollment/session test.

Through the tunnel, verify health, expected `space_id`, team kind, installed
source identity, and the highest common team-shell protocol. Support one native
enrollment call for a bootstrap/invitation code and one storage path for an
existing permanent token; capture any newly issued token directly into Keychain
and clear the input. Then
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

**Implemented 2026-08-30:** D4a and D4b share D3's transport and one native
session owner. Focused tests, strict native checks, source build/startup, and the
scoped D4 audit pass. S105's real enrollment/cookie/navigation/restart drive is
still required before this packet is live-complete.

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

**Implemented 2026-08-30:** the grouped index, controlled Add flow, concurrent
reconcile, cached-unavailable rendering, reconnect, and native navigation bridge
are in `9be6c22`. The follow-up scoped audit found no Critical or High issue and
closed its findings around visible errors, browser capability, stable ordering,
stale reconciliation, keyboard behavior, and risk-bearing coverage. All 446 Web
tests, typecheck, and the production build pass. Only the visible two-space drive
remains open for D5.

### D6 — Fixed operator CLI bridge

**Implemented 2026-08-30:** the native route registry, exact direct/sudo argv,
fixed read-only probe, bounded structured event channel, authenticated durable
request readback, explicit Terminal handoff, Tauri permissions, TypeScript
bindings, and focused regressions are complete. The one audit's three interim
findings are closed. The live direct and named-operator drives remain open
because the reachable shared lab host has no installed `rcp` Linux account; no
server state was changed for this packet.

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

### D7 — Unified project wizard provisioning mode

**Implemented 2026-08-30; complete team drive still open:** one top-level
wizard shell now consumes the backend `project_creation` contract for its
personal and new-team modes,
creates and resumes durable provisioning requests, renders every backend-owned
status/action/readiness/final-review answer, and relays only D6's proven native
operator capability. All 456 Web tests, typecheck, the production build, 90
native tests, and strict Clippy pass. A disposable browser drive proved the
single shell and reload-stable `#/projects/new` route; a freshly rebuilt source
desktop opened that same shell from a disposable data directory and exposed its
native folder action. The one independent audit is closed after fixing stale
operator-probe admission, durable-request loading, exact command quoting,
machine-alias references, impossible lifecycle fixtures, structured operator
content, explicit Git-write facts, confirmation ordering, final-review detail,
and accessibility semantics.

T5b now activates move mode in this same wizard. The complete authenticated
team-server/direct-or-sudo operator drive remains open in S128 with D5/D6/P6a.
The reachable lab host still has no installed `rcp` account, so this packet did
not invent a false live server qualification.

Own:

- `web/src/views/ProjectSetup.tsx` as the one visible wizard, with optional
  focused step components that never become another top-level wizard;
- `web/src/App.tsx` routing and `web/src/views/ProjectSettings.tsx` deep-link
  contract;
- P2 integration in `web/src/api.ts` and `web/src/types.ts`; and
- browser plus desktop tests.

Extend the current wizard with plainly named personal and new-team intents; T5b
activates move mode in this same shell. Render the backend's six statuses,
exact diagnostic/next action, resolved paths, Git write and provider readiness,
final-review digest, and human authority. The team request form accepts only
P1's two documented GitHub.com URL forms and shows the canonical
`owner/repository` result, the fixed server-local root and, for SSH, the
backend-proposed home-derived root with an explicit absolute-root field for
intentional lab storage; it never asks for a member checkout to upload. Final
review repeats the resolved values. Invalid repository text is rejected before
the request exists. Show
**Run setup now** only from the D6 probe; always show **Copy server command**.
CLI events are transient progress, never the state machine.

Use each backend's P2 `project_creation` answer for product eligibility,
preselection, required fields, and pinned source identity from the project-index
primary action and `#/projects/new` deep link. Use D3/T5a's native bridge answer
only for relay capability and authenticated saved targets. Offer move only when
the source backend permits export, the selected target backend permits import,
and the native bridge can connect them. A browser has no native answer and
cannot offer move. The one wizard calls the personal path APIs only in personal
mode and durable provisioning APIs only in new-team mode. Do not derive product
authority from `space_kind`, repository paths, saved-connection presence alone,
or native-global detection; the direct API rejection remains the independent
backend fence.

At **operator action needed**, render P1/F1's structured responsibility, typed
machine or external-service target, ordered safe command or GitHub action,
nonsecret value, expected success, and resume command. Never parse the CLI
message for fields or add a wizard-only instruction. The deploy-key step
explains that the public key is the checkout's repository identity and that a
human with the required repository-administrator role—not an RCP GitHub
login—adds it with **Allow write access**.

Use one primary action and real error text. Do not add muted helper/commentary
lines beneath primary labels. Final creation requires an explicit human review
action. Move is shown only through T5b's complete pinned-source route; D7 does
not show a half-built transfer state or create a separate transfer wizard.

### D8 — Thin team-shell compatibility handshake

**Implemented 2026-09-01; coordinated live cutover still open:** protocol 1,
exact response echoes, commit diagnostics, the version-2 registry migration,
and the shared cross-language contract fixture have focused coverage. S105 still
owes the real installed-server and rebuilt-desktop drive.

Own:

- the protocol range and response echo in `src/rcp/api/health.py`,
  `src/rcp/api/team.py`, and `src/rcp/api/index.py`;
- negotiation and source-commit diagnostics in
  `web/src-tauri/src/team_session.rs` and `web/src-tauri/build.rs`;
- the version-2 to version-3 connection-registry migration in
  `web/src-tauri/src/team_connections.rs` plus the Web response shape; and
- one immutable protocol-1 fixture with focused Python, Web, and Rust checks in
  the existing CI jobs.

Negotiate the highest overlap in the compiled and advertised inclusive ranges;
send and require the exact selected-version echo on the native enrollment,
token-exchange, and project-card requests. Refuse missing or disjoint ranges and
missing or different echoes before cookie installation or navigation. Report
both exact source commits and the stale side's update action. Do not add server
operations or feature capability discovery to this entrance. Remove the saved
minimum shell version through an automatic migration without changing the
Keychain reference or any routing identity.

## Transfer packets

### T5a — Native transfer relay

**Implemented 2026-08-31; real source-built SSH drive still open:** the native
shell owns one request-bound streaming relay, proof return, cleanup
acknowledgment, protected manual export, exact retry, and strict IPC boundary.
Hermetic native tests and lint pass; S98 still owes the real operator-route
drive.

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

**Implemented 2026-08-31; live two-space drive still open:** the existing
wizard owns the pinned move route, durable final-review coordinator, native
restart recovery, explicit manual relay, and loud retry state. Browser, build,
native, and hook checks pass; S98 remains pending until the source-built desktop
interruption drive passes.

Own:

- move-intent steps inside D7's one `web/src/views/ProjectSetup.tsx` wizard,
  with focused child components allowed only beneath that shell;
- the **Move to team space** deep link in `web/src/views/ProjectSettings.tsx`,
  plus `web/src/api.ts`, `web/src/types.ts`, and `web/src/App.tsx` integration;
  and
- browser and source-built desktop recovery tests.

Show source and target absolute paths, what stays owned by the person, central
ownership, active work to settle, execution settings to re-establish, and the
settled archive contents/exclusions. Provider matching is automatic; keep its
bounded selected/skipped summary in the transfer details and do not add a
transcript-selection UI. No confirmation before target **ready for review**.
The project index may offer move as the third wizard intent only when the
personal backend permits export, the selected team backend permits import, and
the native bridge reports relay capability for their authenticated connections.
Project Settings opens that same intent with its source pinned; neither entrance
mounts a separate transfer wizard.
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
- every secret and account boundary above has a negative verification;
- the genuine live lab drill passes and is documented without credentials; and
- the workflow decision and agent instructions identify this archive commit as
  the end of direct development on `main`.

## Suggested skills for pickup

- The original design grilling and cross-document fact-check are complete. The
  desktop/server compatibility boundary was separately grilled and settled in
  its active decision record. Continue directly on
  `main` through this handoff's closure. Finish F6d's two-release
  live workflow before starting another source-update packet; O3c and the later
  restore lane may also consume the now-complete protected-backup boundary.
  Implement remaining packets without reopening product boundaries unless
  current code contradicts their authority.
- Use `computer-use:computer-use` for the real source-built desktop drives in
  D2, D4a, D4b, D6, D7, T5a, T5b, and V1; browser tests cannot prove native SSH,
  Keychain, cookie-store, or navigation behavior.
- Use `codex-security:security-diff-scan` after the credential/control-socket,
  SSH bridge, backup, and restore packets, scoped to their actual diffs.
