# Dev team space and source server completion handoff

Date: 2026-08-27
Status: active; design, grilling, and the final cross-document fact-check are
complete, and implementation is now in progress directly on `main`. G0 restored
the CI baseline, F1 provides the live server CLI command/event contract, G2
guards upgrades from every current server-era persistence boundary, F2 defines
the fixed Linux layout/config/unit, and F3a implements the concrete source-server
installer. Its one independent audit is
complete and its findings are fixed. Commit `638c19e` is the immutable first
installable boundary, and its chained `source-server-install-v7-638c19e` fixture
is pinned in the upgrade registry. F3a is therefore complete. F3b now has the
operator guide, guarded live drive, and fixed 22.04/24.04 manual Actions matrix;
its one independent audit is complete and every finding is fixed. Live
qualification is in progress with the protected repository-admin test
credential: both real disposable-host jobs now clear account and sudo-policy
validation, managed-Python installation, and source-grant creation, but the
isolated SSH trust probe still exits 255 after the verified fingerprint prompt.
A diagnostic rerun is pending before F3b is complete. P1 now provides the durable,
strictly guarded project-provisioning state machine; its one independent audit
is complete, every finding is fixed, and its exact schema boundary is retained
in the chained upgrade registry. P1 is complete. D1 now provides the strict
nonsecret desktop team-connection registry and the macOS Keychain write/removal
boundary; its one independent audit is complete and its in-scope findings are
fixed. D2's real WKWebView spike has reached its prescribed stop condition:
HTTP loopback aliases and the exact localhost control do not return the required
`Secure` cookie to the server, while extra loopback addresses require privileged
host mutation. No production routing code was changed; Q11 now records the
required origin-security decision. Real Keychain round-trip, SSH, enrollment,
navigation, and UI
remain D3 through D5 after D2 is resolved. Every concrete provisioning
operation, member/API projection, finalizer, and the unified wizard remain later
packets. O3a now implements strict backup machine configuration and installs the
matching backup service/timer units in a proven disabled state; encrypted
capture, integrity readback, retention, and safe timer enablement remain O1,
O2a, O2b, and O3b.
The previously planned G1 pull-request transition was rejected by the human for this
private, single-developer pre-team-server implementation; it no longer gates any
packet.

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

The remaining seams are also concrete:

- the strict `rcp server` command/event shell, Linux layout, and source installer
  now exist, but F3b's two real Ubuntu runs are still pending and doctor,
  control-socket, update, backup, restore, and member-removal owners do not yet
  exist;
- `default_data_dir()` still falls back to the macOS Application Support path;
  a Linux service works only through an explicit `RCP_DATA_DIR` today;
- the Web UI still says “Team connections are not implemented in this build”;
- the Tauri shell now stores strict nonsecret team-connection metadata and can
  write/remove a permanent member token in macOS Keychain, but it still trusts
  one current loopback backend and has no distinct-origin allocator, SSH tunnel,
  live token read/enrollment, multi-backend navigation, or operator-command owner;
- the durable project-provisioning request exists, but its member API,
  machine-side workers, finalizer, and personal-to-team transfer record do not;
  and
- canonical identity replay currently treats two differing identity payloads as
  corruption, so a home transfer cannot be represented by appending a second
  `ProjectIdentity` record.

The repository's current `AGENTS.md` prescribes direct work on `main`, which the
human retained for the full private pre-team-server implementation. CI reports
post-push `main` but has neither an old-data-to-candidate upgrade gate nor GitHub
branch protection. Current `main` also has the red baseline described in G0.
Repair G0 directly, then G2, F1, and D1 may begin according to the dependency
map.

### Resolved repository workflow boundary

A read-only GitHub fact-check on 2026-08-28 confirmed that `Zhi0467/RCP` is
private and its current plan rejects the branch-protection API with HTTP 403,
stating that private-repository protection requires a plan upgrade or a public
repository. The human chose not to change the repository's plan or visibility
and explicitly retained direct work on `main` throughout this private,
single-developer implementation. Each packet receives focused tests,
pre-commit, and code review; full desktop/live drives occur at meaningful
milestones. CI reports pushed failures but does not technically reject a direct
push, and the evidence must not imply otherwise.

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

### Implementation log

#### 2026-08-28 — G0 baseline repair complete in the working tree

- Work is on local `main` from `4e6d812`. The seven known formatter changes are
  mechanical. `_agent_task_record` now retains the persisted runtime, and the
  active-handoff test indexes valid active work instead of requiring an empty
  handoff directory.
- The complete Python suite passes with 2,373 tests and one existing dependency
  deprecation warning. The Web production build and all 434 Web tests pass.
  Focused runtime, compatibility, instruction, and documentation tests pass;
  `git diff --check` and all-file pre-commit are green.
- Surprise: the extra stale compatibility test was not named in the original G0
  inventory. It is now explicit in G0 ownership instead of being treated as an
  unrelated failure.
- The requested read-only review confirmed the runtime fix and formatter-only
  files, then found a future-empty-handoff test edge, stale S60 wizard language,
  ambiguous cross-backend move ownership, and an unsafe unspecified Git-source
  boundary. All four are resolved in the current test and authority docs. The
  focused follow-up then caught that external GitHub actions cannot truthfully
  name an unknown human account; the step contract now separates responsibility
  from typed machine and external-service targets. A final read-only check found
  no remaining issue in this scope.
- Not done: no desktop drive was run because G0 changes no desktop behavior. G0
  was committed locally as `7f0d9c2`; nothing was pushed.

#### 2026-08-28 — implementation authority refinements

- The human retained direct work on `main` for the full private pre-team-server
  implementation. The convention-only G1 PR transition was removed from the
  plan and dependency map. A temporary local G0 branch created from the stale
  instruction has no unique commit; the working edits were moved back to
  `main`, and the unused ref was left untouched rather than deleted implicitly.
- The human selected one unified project wizard with personal, new-team, and
  personal-to-team intents. New-team setup uses canonical GitHub.com repository
  identities and repository-scoped deploy keys; local-only code is pushed by the
  human first. The CLI is the sole exhaustive machine workflow, while the wizard
  renders its structured actions and retains human product approval. A fresh
  install requires `--team-name`; the strict request carries it so the CLI can
  print exact initialization argv and the future wizard can submit and render
  that same operation rather than owning another recipe.
- Security refinement from the G0 review: this slice is GitHub.com-only. One
  canonical repository-reference parser rejects local, credential-bearing,
  ambiguous, ported, or arbitrary-host inputs before persistence or side
  effects; GitHub Enterprise requires a later trusted-origin design.
- Not yet done: the provisioning command and progress contract now exist, but
  its deploy-key and provisioning owner still returns an explicit unavailable
  result. No unified-wizard code or transfer UI has been implemented or
  runtime-verified yet.

#### 2026-08-28 — F1 server CLI contract implemented in the working tree

- All ten accepted `rcp server` command forms now parse into one strict request
  model, enforce canonical UUID4 selectors, and apply the settled root-versus-
  `rcp` entry-account matrix before a concrete handler can run. Restore accepts
  only an absolute archive path plus protected identity-file path; transfer
  import accepts archive bytes only through stdin.
- The concrete-owner seam is deliberately two phase. It prepares a side-effect-
  free complete plan, RCP validates and flushes that plan, and only then does the
  executor receive stdin or begin machine work. Every live event is lifecycle-
  checked, size-bounded, secret-scrubbed, flushed immediately, and rendered as
  either plain interactive guidance or the same versioned NDJSON record. The
  wizard can consume that record without parsing prose or owning another setup
  recipe.
- Each interactive plan names every step's purpose, human-versus-RCP
  responsibility, typed target, pending state, and success condition. A human
  pause carries ordered safe commands or external UI actions, nonsecret values,
  and the exact recheck/resume argv. Unexpected executor errors become a generic
  terminal failure; exception and subprocess text are never copied into the
  event stream.
- Focused verification passes 78 tests in `tests/test_server_cli.py` and
  `tests/test_main.py`; adding the repository's eight documentation checks gives
  86 passing tests. Focused Ruff and formatting checks, `git diff --check`, and
  the final all-file pre-commit baseline pass.
- Review surprise: the first implementation buffered a completed execution and
  printed its nominal progress only afterward. The read-only review caught that
  this violated both the operator workflow and real desktop progress. The seam
  was refactored to plan-then-stream, and a regression proves the plan is visible
  before the first simulated side effect. The same review found unguarded URL
  query/fragment and age/provider-key channels plus representative-only account
  tests; those are now rejected or redacted, and all ten commands exercise their
  full privilege boundary.
- Final self-review added a 64 KiB live-output reserve. A malformed owner can no
  longer consume the entire one-MiB event budget and then prevent RCP from
  emitting its generic terminal failure; the maximal secret-safe failure shape
  is measured against that reserve in a focused regression.
- Not done: concrete install, doctor, provider, provisioning, transfer, backup,
  restore, member-removal, and update owners remain intentionally unavailable
  until their packets land. F1 changes no Web or native desktop code, so no
  desktop drive was run. No full backend suite has been run for this packet; the
  user explicitly accepted focused packet tests plus pre-commit and one
  independent audit. One attempted documentation-test command named stale path
  `tests/test_docs_consistency.py` and therefore ran nothing; it was replaced by
  the real `tests/test_documentation.py` command whose checks pass. No concrete
  command can be live-verified until its owning packet replaces the explicit
  unavailable result.

#### 2026-08-28 — G2 old-data upgrade gate complete

- CI now has one stable **old-data upgrade** job. It builds the Web assets and
  candidate environment, then runs both the immutable historical-boundary
  fixtures and an exact-base fixture. A dirty local tree uses current `HEAD` as
  the base and the working tree as candidate; a committed CI checkout uses
  `HEAD^1` as the base and the checked-out commit as candidate.
- Six immutable boundaries cover the actual first team-server-capable merge and
  every later stored-shape or migration-interpretation era: episode vocabulary,
  orchestrated children/membership, graph targets, provider runtimes, and the
  modern Experiment repair. Each sanitized bundle contains a small team
  database, active credential and session hashes, canonical identity history,
  one project/member relationship, and one task that was running at shutdown.
  An external registry pins the exact set, source commits, and whole-bundle
  digests; each bundle also inventories its payload files. The first is created
  by the first server code and every later bundle is the preceding database
  opened and settled by that boundary's exact source, preserving accumulated
  migration state a fresh database would miss. Paths are fixture-relative; raw
  bootstrap/member/session/provider/Git credentials and runtime SQLite/lock
  sidecars are absent.
- The candidate copies each bundle before use, runs current migrations, replays
  canonical history with attribution required, starts the complete FastAPI
  lifespan with provider execution disabled, and verifies health, membership,
  project projection, startup interruption recovery, exact canonical Patch
  filenames/bytes and revision, credential/session survival, SQLite integrity,
  and the task projection. The focused static-boundary run passes seven checks
  with one exact-base skip; the local CI-equivalent exact-base run passes all
  eight checks.
- Builder surprises found before review: the first historical build called
  identity-aware history initialization before the old code had claimed an
  identity; using the version-neutral initialize-then-claim order fixed it. An
  absolute `npm --prefix` invocation against the archived checkout produced a
  misleading lockfile failure even though `npm ci` from the Web working
  directory succeeded, so the harness uses an explicit working directory. The
  first bundle inventoried an ephemeral empty WAL sidecar and retained an append
  lock; a direct SQLite inspection also proved that updating an absolute locator
  later leaves that path in free database pages. The builder now writes relative
  locations initially, snapshots identifiers before final checkpoint, removes
  sidecars/locks, and hashes only the settled bundle. The exact-base harness
  also needed to create its intermediate parent directory explicitly.
- The one independent audit found that the first draft covered only the oldest
  and newest data, allowed Patch changes behind `>=` revision checks, bypassed
  credential survival, and let `fixture.json` attest to itself. All four are
  closed by the six-boundary external registry and the exact assertions above;
  the audit's additional SQLite integrity check is included. The history
  inventory also fact-checked `0bb0e72` as a file split and `15824c5`/`e84b461`
  as transaction-behavior changes with no new stored shape, so redundant
  fixtures were not invented for them. No second audit was run, per the human's
  one-audit-per-packet rule.
- Final self-review caught that six separately fresh databases would label the
  modern Experiment-repair era without exercising it. The final fixtures are a
  real upgrade chain instead: the first-team code records a bound live legacy
  Experiment, the episode-vocabulary boundary migrates it and then completes its
  task, and the later pre-repair starts retain the contradictory
  `legacy_unavailable` wrap-up beside the still-live parent. Each affected test
  first proves that raw old row exists, then proves current migration removes it
  without losing the episode. The last pinned source is the actual pre-repair
  `af52e03`, not the already-fixed `650d1f0`.
- Both final focused runs emit only the repository's existing Starlette
  `TestClient`/httpx deprecation warning. No warning was suppressed or treated as
  upgrade evidence.
- The first commit attempt was correctly rejected by the repository's 500 KiB
  staged-file guard because five immutable SQLite databases exceeded it. The
  guard was not bypassed. Fixture databases are now stored as deterministic
  gzip (`mtime=0`) of the exact settled historical bytes, and the candidate
  expands only its temporary copy before migration. The external bundle digests
  pin that representation, while the same pre-migration row assertions prove
  that compression did not turn the fixture into a reconstructed approximation.
- Not done: this packet has not run the new job on GitHub-hosted Ubuntu, rehearsed
  an actual server data directory, driven the desktop, tested source update
  rollback, or tested disaster restore. Those remain owned by F6a-F6d, O4, and
  the milestone drives; this CI evidence is not a substitute.

#### 2026-08-28 — F2 Linux layout and installed config complete

- `server_ops.layout` now owns one fixed, validated path set for the `rcp`
  account, managed source and per-commit releases, data, central repositories,
  credentials, update checkpoints, restore journals, native provider and SSH
  state, root configuration/current pointer, private runtime socket, stable CLI
  wrapper, systemd unit, and journald service identity. Release paths require a
  full lowercase Git object id; central checkout paths require canonical UUID4
  project ids and one safe alias component. Remote repository credentials derive
  only from the explicit absolute home reported by that execution account, not
  `/home/<name>` or a shell environment value.
- `/etc/rcp/server.toml` began with one closed version-1 TOML model. O3a's
  current version-2 reader upgrades that exact first shape in memory and adds an
  optional strict backup section; a version-1 document carrying that future
  section is rejected. The file still records the immutable installation UUID,
  fixed account/unit/path contract, and one GitHub `main` source using either
  HTTPS with no credential or SSH with the dedicated deploy-key public
  fingerprint. Unknown fields, path drift, cross-wired transport/authentication,
  malformed fingerprints, and an explicit empty identity fail closed. No
  private key, provider login, recovery identity, or member credential enters
  this machine file.
- The config writer resolves the actual root UID and `rcp` primary GID rather
  than accepting caller-selected ownership. It rejects symlinked ancestry and
  an existing file with the wrong owner, group, or exact `0640` mode, writes a
  same-directory temporary with the final ownership/mode, fsyncs it, atomically
  replaces the target, fsyncs the parent, and validates the published file. An
  existing config may change only while retaining its installation id.
- The shipped `rcp.service` asset runs `/usr/local/bin/rcp` as `rcp` from the
  root-controlled current release, binds only `127.0.0.1:8421`, uses the fixed
  data and mode-0700 runtime directories, serves the prebuilt Web bundle, and
  has no reload path. It leaves provider homes readable and does not invent a
  file log beside journald.
- Focused layout/config tests and the shared server-CLI suite pass 69 checks;
  Ruff and formatting checks pass. The one independent audit found arbitrary
  config ownership, uncoupled source transport/authentication, falsey identity
  regeneration, control characters in remote homes, and incomplete fixed-path
  assertions. All five were fixed before closure, and no second audit was run.
- A supplementary wheel probe did not reach asset inspection because the
  repository's pre-existing Hatch configuration tries to add
  `rcp/skills/episode-report/SKILL.md` twice. This slice intentionally does not
  repair or depend on wheel packaging: the accepted team-server path is a clean
  source checkout plus `uv sync --frozen`. Source resource loading of the unit
  is covered. F3b still owns proof from the installed source environment.
- Not done: no account, directory, config, wrapper, symlink, or systemd state was
  changed on a real Linux host. F3a owns those effects and F3b owns Ubuntu 22.04
  and 24.04 installation/readback. No concrete CLI operation or wizard flow is
  exposed by F2 alone.

#### 2026-08-28 — F3a source-server installer implemented and audited

- `rcp server install --team-name "<team name>"` now replaces the unavailable
  install seam with one concrete nine-step operation. The strict request owns
  the team name because a terminal-only run must print exact initialization and
  resume argv; the future wizard submits and renders the same plan/events. Plan
  preparation only reads the supplying checkout's credential-free GitHub origin,
  and the complete plan is flushed before host effects begin.
- Preflight accepts only Ubuntu 22.04/24.04 x86-64 with running systemd, Git,
  system-wide `uv`, Node.js 24/npm, OpenSSH, and `age` 1.x. It installs no apt
  source or general tool. After account creation it installs a missing managed
  Python 3.12 through system-wide `uv` as `rcp`, then resolves and executes that
  runtime again before any source work. This closes the fresh-host gap where an
  operator was previously expected to prepare files inside an account that did
  not exist yet. Account
  convergence creates or strictly validates `rcp` at `/home/rcp` with
  `/bin/bash`, exact non-locking unusable shadow value `*NP*`, its dedicated
  non-root user/group identity, primary group, no supplemental groups, and no
  sudo authority. The preflight proves the running systemd manager is reachable
  before the account or filesystem can be changed.
- All managed Git/npm/uv/SSH commands cross one `runuser` boundary with an empty
  environment, fixed service home/PATH, no inherited SSH agent or operator
  credential, and no shell evaluation. Source Git additionally disables system
  and global config, credential helpers, and askpass before deciding whether the
  repository is public. Public GitHub `main` records HTTPS and no key. A private
  source creates one Ed25519 key labelled
  `rcp-source:<installation-id>`, records only its public fingerprint, and pauses
  with the exact GitHub deploy-key URL, public key, read-only checkbox rule,
  published host-fingerprint URL, host-trust command, success signal, and exact
  resume argv. Network failure is not misreported as a missing grant.
- The bootstrap checkout is never adopted. Install clones the separate managed
  checkout as `rcp`, refuses local changes and unfinished restore state, fetches
  only the recorded `main`, and refuses to turn a newer upstream into an install
  update. It creates one detached per-commit worktree, runs exact `npm --prefix
  web ci`, Web build, and Python-3.12 `uv sync --frozen`, and never rebuilds an
  active release in place. Existing source, key, release, data, wrapper, unit,
  and current-pointer state is converged only when ownership and exact meaning
  are proven; unknown or symlinked state fails loudly.
- Root installs the stable data-aware wrapper, exact non-reloading unit, and
  atomic current pointer. A fresh empty data directory is proved stopped and
  disabled, then the CLI pauses with ordered team-init and rerun commands plus
  their success signals. Activation and health remain the next system-owned
  step and execute inside the resumed CLI. An initialized
  rerun never opens SQLite: it converges systemd, reads back exact service state,
  and uses a direct proxy-free/non-redirecting loopback HTTP connection with a
  bounded body. It requires `status=ok` and `space_kind=team`, and proves the
  service is stopped and disabled after a wrong-space result before saying so.
- The packet's one independent read-only audit inspected the installer, service
  unit, CLI/model/limits changes, tests, README, spec, and handoff. It found six
  defensible gaps: root-valued service identity, a one-command sudo probe,
  unchecked systemd fencing, proxy/redirect-capable health readback, ambient Git
  credential helpers, and a filesystem-only systemd preflight. All six are now
  fixed with focused regressions; no second audit was run, per the accepted
  packet process.
- Commit `638c19e17252e0e441a698e628b49449df088c81` is the exact first
  installable source-server boundary. The next chained historical fixture is
  `source-server-install-v7-638c19e`; its metadata names that full commit and the
  external registry pins bundle digest
  `3f2c9a6cac26424882a7ec64f35d0c0410ea64d86597a3e7359c2ba5951c8a69`.
  The fixture/upgrade and documentation run passes 16 tests; the separately
  environment-gated exact-candidate-base build is the one expected local skip.
- Focused installer and shared CLI verification currently passes 158 tests,
  including public/private/fresh/resumed orchestration, managed-runtime install
  and recheck, plan-before-effect and
  dual-renderer contracts, credential-environment clearing, Git failure
  classification, fixed command sequences, source/update separation, build
  order, unprivileged-account and sudo-policy checks, live-systemd preflight,
  credential-free Git, direct loopback HTTP, fail-closed service fencing,
  wrong-space shutdown, and unsafe data refusal.
  Focused Ruff/format and diff checks pass. The concrete Linux branches have
  60% statement coverage here; the intentionally separate F3b disposable-host
  drive remains the proof of real NSS, filesystem ownership, systemd, SSH, and
  Ubuntu tool behavior.
- Not done in F3a: no Ubuntu host or desktop was driven. F3b owns the disposable
  Ubuntu 22.04/24.04 installation/readback and operator guide; those facts are
  explicit rather than inferred from the unit suite.

#### 2026-08-28 — F3b guide and live-drive implementation in progress

- `docs/server.md` is now the exhaustive terminal guide. It separates Ubuntu
  22.04 and 24.04 prerequisites, pins the qualified Node.js and uv downloads,
  gives the disposable bootstrap build, and follows the CLI's ordered GitHub,
  initialization, activation, readback, provider-auth, and operator-route
  boundaries. The CLI stays complete without the future wizard; the wizard may
  only submit and render the same structured operation.
- A clean-machine review found one F3a defect before the live drive: install
  expected Python 3.12 to exist inside the newly created `rcp` account. The
  installer now uses required system-wide `uv` as `rcp` to install a missing
  managed 3.12 runtime, then resolves and executes it again. The focused server
  suite passes 121 tests with two intentional environment-gated skips, including
  the new install/recheck and bounded-output regressions.
- `tests/test_server_install_live.py` is destructive only behind two explicit
  gates. It refuses a nonempty host, accepts the private-repository
  Administration token only from a caller-owned protected file, creates one
  temporary read-only GitHub deploy key, verifies GitHub's published Ed25519
  fingerprint before accepting host trust, and removes the deploy key in
  cleanup. It builds and then deletes a separate bootstrap checkout before
  initialization and finishes through the installed CLI.
- The live drive checks interactive bootstrap-code isolation from journald,
  systemd/process identity, fixed owners and modes, loopback-only port 8421,
  health, password refusal, optional direct-key `rcp` SSH, a fresh named
  operator's exact D6 sudo command, refusal of an unlisted command, installed
  CLI convergence, source-key revocation, restart, and continued health.
- `.github/workflows/server-install-live.yml` provides separate fixed
  `ubuntu-22.04` and `ubuntu-24.04` x86-64 jobs and repeats the documented
  prerequisite versions. It is manual and `main`-only. The private source means
  GitHub's ordinary `GITHUB_TOKEN` is insufficient: deploy-key creation requires
  repository Administration write. The workflow therefore expects the narrowly
  scoped secret `RCP_LIVE_GITHUB_ADMIN_TOKEN`, materializes it as mode 0600, and
  never passes it to RCP.
- The packet's one independent audit found eight concrete gaps: a stale
  four-command initialization expectation, ambiguous deploy-key cleanup after a
  partial API failure, an unverified downloaded uv installer, temporary SSH and
  sudo access left on the host, an incomplete clean-host fence, unbounded build
  output capture, a decision example missing required `--team-name`, and a
  mutable checkout action tag in the privileged workflow. The live drive now
  follows the CLI's one human init command plus exact root resume; records the
  nonsecret deploy-key label before creation for unconditional cleanup; installs
  the immutable uv archive only after a pinned SHA-256 check; removes its test
  account, authorized key, and sudoers rule; rejects a loaded service, live RCP
  process, port 8421 listener, runtime directory, or prior test state; caps
  subprocess output; repairs the exact decision command; and pins checkout to
  the reviewed v7.0.0 commit. No second audit was run, per the one-audit packet
  rule.
- Live qualification is now using the protected
  `RCP_LIVE_GITHUB_ADMIN_TOKEN` secret on disposable GitHub-hosted runners.
  Temporary deploy-key creation remains inside the guarded live test and every
  failed attempt so far stopped before a source grant was required; workflow
  cleanup completed on both releases. F3b remains incomplete until one exact
  commit passes the entire install/remove/readback drive on both Ubuntu
  releases and the repository has no leftover temporary deploy key. Because
  install deliberately consumes
  `origin/main`, this workflow is a post-push qualification rather than a
  pre-merge PR gate; the current human-approved direct-main development boundary
  makes that explicit instead of pretending otherwise.
- Live qualification run
  [33225665846](https://github.com/Zhi0467/RCP/actions/runs/33225665846)
  reached both Ubuntu 22.04 and 24.04 runners but stopped in prerequisite
  verification before building or invoking RCP. The downloaded Node.js and uv
  archives both passed their pinned checksums; the workflow then incorrectly
  required the complete `uv --version` line to equal `uv 0.12.7`, although the
  upstream binary appends a build hash and date. The workflow now validates the
  command name and semantic version fields separately. No deploy key was
  created and no installer-owned host state was reached in this failed attempt;
  a rerun on the corrected commit remains required.
- Corrected run
  [33230398233](https://github.com/Zhi0467/RCP/actions/runs/33230398233)
  passed the Node.js and uv version checks on both releases, then exposed a
  hosted-runner impurity before RCP was built: extracting the pinned Node.js
  archive over GitHub's preinstalled `/usr/local/lib/node_modules/npm` retained
  stale nested packages and made every npm command crash. The workflow now
  removes only that exact disposable-runner npm directory before extracting the
  pinned archive and explicitly requires `npm --version` to succeed in the
  prerequisite step. The secret and live installer were not reached, so no
  deploy key or RCP-owned host state was created; another corrected rerun
  remains required.
- Corrected run
  [33231425876](https://github.com/Zhi0467/RCP/actions/runs/33231425876)
  passed prerequisite installation and the complete bootstrap build on both
  releases, then found the first live-harness privilege defect: its clean-host
  fence used ordinary-user `Path.exists()` below root-only `/etc/sudoers.d`.
  The fence now uses bounded noninteractive-root probes for both existence and
  symlink identity, with focused tests for a present object, broken symlink,
  absence, and an indeterminate probe. The test failed before reading the
  protected token or creating a deploy key, and workflow cleanup completed on
  both runners; another corrected rerun remains required.
- Corrected run
  [33231556482](https://github.com/Zhi0467/RCP/actions/runs/33231556482)
  passed all clean-host gates and reached the first real root installer call on
  both releases. That call returned 1, but a second defect masked its event and
  stderr: root's Python import created bytecode in the operator-owned bootstrap
  environment, then ordinary-user cleanup failed on those root-owned files.
  The documented/bootstrap live command now fixes
  `PYTHONDONTWRITEBYTECODE=1` before Python starts, and the installed wrapper
  applies the same invariant to root maintenance commands and the service.
  Live diagnostics now retain only bounded output tails, and in-process
  deploy-key revocation precedes bootstrap cleanup. Workflow cleanup completed
  on both runners and no deploy-key receipt was present; the next run must both
  prove ownership-safe cleanup and expose or clear the underlying installer
  exit.
- Corrected run
  [33231763674](https://github.com/Zhi0467/RCP/actions/runs/33231763674)
  proved that the bootstrap stays ordinary-user removable and exposed the
  underlying product failure on both releases: account creation consumed the
  full generic 30-second read-only probe limit, then returned the generic
  `useradd` failure event. Account creation is a stateful one-time operation,
  so it now owns a separate bounded two-minute limit while all ordinary probes
  remain at 30 seconds; an actual expiry is reported as such with the exact
  operator inspections. No source-grant pause or deploy-key receipt was
  reached, and workflow cleanup completed on both runners. A corrected rerun is
  still required to prove whether hosted `useradd --create-home` completes
  within that stateful boundary.
- Corrected run
  [33231993855](https://github.com/Zhi0467/RCP/actions/runs/33231993855)
  proved account creation completes beyond the old probe cutoff on both
  releases, then falsely reported that the new `rcp` account had sudo
  authority. The first diagnosis attributed this to the root process's
  inherited `SUDO_USER=runner`; root-owned installer subprocesses consequently
  drop only the four inherited `SUDO_*` caller-identity variables while
  preserving unrelated environment. Run 33232185202 below disproved that as the
  complete cause. No source grant or deploy key was reached, and workflow
  cleanup completed on both runners.
- Corrected run
  [33232185202](https://github.com/Zhi0467/RCP/actions/runs/33232185202)
  ran exact commit `e445246c2c4936823712c1fe56a14974e6de40fd` and again reached
  account validation on both Ubuntu 22.04 and 24.04 after caller-identity
  variables were removed. The remaining defect is the probe itself: a root
  process used `sudo -U rcp -l` and interpreted the query's zero exit status as
  proof that `rcp` could use sudo. The first correction executed `sudo -n -l`
  directly as `rcp` through the existing clean `runuser` boundary. No source
  grant or deploy key was reached, and cleanup completed on both runners.
- Corrected run
  [33232456126](https://github.com/Zhi0467/RCP/actions/runs/33232456126)
  ran exact commit `26c3317fb6c22d5a7182f061fcf8486747aad67b` and disproved the
  direct-account query: both Ubuntu releases returned an indeterminate result
  because listing policy as the password-disabled `rcp` account can require
  authentication before reporting that it has no grants. The installer now
  returns to the root-authorized `sudo -U rcp -l` policy query but evaluates its
  C-locale result before the query caller's exit status. The explicit “not
  allowed to run sudo” result is accepted with either observed status; a
  successful listing without that denial is rejected as policy, and every
  other result fails closed. Focused regressions pin all three outcomes. No
  source grant or deploy key was reached, and cleanup completed on both
  runners. Another corrected rerun is required.
- Corrected run
  [33232623180](https://github.com/Zhi0467/RCP/actions/runs/33232623180)
  ran exact commit `908d803328b758477bf5cdb6d343f51b95bcc72a` and cleared the
  account and sudo-policy boundary on both releases. It then failed while uv
  installed managed Python 3.12 as `rcp`. The live path exposed that the root
  installer inherited the operator-owned bootstrap checkout as its current
  directory; that checkout sits below a `0700` temporary parent. The shared
  `_run_as_account` boundary changed identity and environment but not directory,
  so `rcp` could run cwd-independent version probes yet could not perform uv's
  real install from that inaccessible directory. Account commands now default
  to the account's fixed home and retain an explicitly supplied checkout cwd.
  The uv failure message no longer assumes every failure is network-owned. No
  source grant or deploy key was reached, and cleanup completed on both
  runners. Another corrected rerun is required.
- Corrected run
  [33232916530](https://github.com/Zhi0467/RCP/actions/runs/33232916530)
  ran exact commit `bdf51500c151349b5a5afe7528d12789a4d048d8` and proved the
  fixed home-directory boundary: both releases installed and rediscovered the
  managed Python, created the private-source key, paused for the read-only
  deploy-key grant, and authenticated that key to GitHub. The harness then
  stopped because no fingerprint prompt appeared. GitHub-hosted runners already
  carry system-wide host trust, while RCP named only its user known-hosts file;
  OpenSSH therefore accepted the global record without exercising the required
  human comparison. The trust action and all later source Git operations now
  set `GlobalKnownHostsFile=/dev/null`, leaving RCP's owned known-hosts file as
  the sole trust source. Both temporary read-only deploy keys were revoked and
  workflow cleanup completed. Another corrected rerun is required.
- Corrected run
  [33233089933](https://github.com/Zhi0467/RCP/actions/runs/33233089933)
  ran exact commit `da2bac852eccf640ec4ad6f0d16cf620f5542333`. Both releases
  reached the isolated GitHub trust command; the PTY helper observed the
  confirmation prompt and accepted it only after finding GitHub's published
  Ed25519 fingerprint. SSH then exited 255 rather than producing GitHub's
  expected authenticated/no-shell result. The assertion previously discarded
  the captured SSH explanation, so it now includes only a bounded output tail
  on failure. No acceptance condition was weakened. Both temporary read-only
  deploy keys were revoked and workflow cleanup completed; a diagnostic rerun
  is required before choosing a fix.

#### 2026-08-28 — P1 durable provisioning boundary implemented and audited

- One strict GitHub.com parser now accepts only the two reviewed HTTPS/SCP forms,
  stores one lowercase `owner/repository` identity, and generates every clone and
  settings URL from that identity. Request creation performs no filesystem, DNS,
  or network work and never persists the member's raw source string.
- The team `AppStore` now owns durable new-project and incoming-transfer
  preparation requests, the six exact display states, proposed project-id/path
  reservation, machine/repository/provider checks, structured human actions,
  final-review binding, explicit cancellation disposition, and transactional
  idempotent step receipts. Preparation does not register a project, append
  canonical identity, or establish a writable home.
- Human-action persistence is request-bound: a GitHub target must name one
  request repository and its exact settings page, a machine target must name one
  declared execution account, and the resume argv must re-enter this exact
  request through project provisioning or its request-scoped provider check.
  Deploy-key labels are derived and secret-safe; ready Git checks require the
  retained public fingerprint and the actual request-scoped write proof.
- The packet's one independent audit found four gaps: generic actions were not
  request-bound, deploy-key labels were not secret/line-safe, a stored review
  digest was shape-checked but not recomputed, and receipt hashing happened
  before diagnostic normalization. All four are fixed with focused regressions;
  no second audit was run, per the one-audit packet rule.
- Focused parser, request lifecycle, strict reload, installer integration, and
  broader storage verification passes 173 tests. Focused Ruff and format checks
  are green. Commit `227f9645e850d20cb19a49be7e944ded64309e43` is the exact
  P1 schema boundary. Its chained `project-provisioning-v8-227f964` fixture
  contains a live in-progress request plus one step receipt, proves the proposed
  project remains absent, and is pinned by bundle digest
  `59c77fd91519935483a93ab6bb6e1c5c4b5dff7f3e21496443ce12a8fb2f029d`.
  The eight-boundary upgrade/start drive passes, so P1 is complete.
- Not done in P1: no member HTTP route, backend UI projection, machine-side Git
  or provider work, final project creation, CLI handler, or wizard code exists.
  P2 through P6 and D7 retain those owners. The CLI remains the exhaustive
  operation owner; the later unified wizard may only submit and render its
  structured state and commands.

#### 2026-08-28 — D1 desktop connection and Keychain boundary implemented and audited

- The native desktop now owns one versioned `team-connections.json` registry in
  its app-config directory. It stores only canonical connection UUID, display
  name, one SSH argv target, remote loopback port, expected team `space_id`,
  stable canonical loopback origin, minimum shell version, and a bounded minimal
  project-card cache. The strict loader rejects unknown fields, duplicate
  identities/spaces/origins/cards, noncanonical UUIDs/origins/versions, unsafe
  SSH arguments, unsupported versions, and oversized state.
- Registry publication uses a same-directory mode-0600 temporary file, file and
  directory sync, and atomic replacement. Reads open one no-follow,
  nonblocking file handle, verify that exact handle is regular, and cap the read
  at one byte beyond the one-MiB limit. A symlink, FIFO-like special file,
  concurrent growth, or corrupt registry fails closed without following or
  unboundedly reading a replacement path.
- Routing metadata can be written only by later verified native connection and
  session owners; no raw Web command may rewrite an existing SSH target or
  origin. The current Tauri surface lists nonsecret records, accepts one
  permanent token only after metadata exists, and removes metadata and Keychain
  credentials through separate idempotent commands. The token is held in a
  zeroizing native buffer, never returned, and stored under the fixed Keychain
  service plus an account derived solely from the canonical connection UUID.
- Every persisted string and the final serialized bytes reject all current RCP
  credential shapes: permanent member and browser-session tokens plus bootstrap
  and invitation codes. The packet's one independent audit found the original
  detector missed dotted enrollment codes and that path-check-then-read left a
  symlink/FIFO/size race; both are fixed with focused regressions and the
  single-handle reader above. No second audit was run.
- Focused and full native verification passes eight new registry/reference
  tests and all 60 desktop Rust tests. Strict `cargo fmt` and
  `cargo clippy --all-targets -- -D warnings` pass, and command permissions are
  generated and granted only to the main desktop window capability.
- Not done in D1: no real member credential was written to this developer's
  login Keychain, and no UI or desktop navigation was driven. The handoff
  assigns the real store/read/replace/delete and missing-item proof to D4a's
  live enrollment/session test, after D2 and D3 provide the verified origin and
  tunnel. D2-D5 still own origin allocation, cookie isolation, SSH lifetime,
  token retrieval/enrollment, session establishment, multi-backend navigation,
  cached-card refresh, and the visible Add-team-space flow.

#### 2026-08-28 — D2 loopback-origin spike stopped at the security boundary

- A dedicated source-built Tauri example and three-server harness exercise the
  actual WKWebView cookie store. They admit only the two exact configured
  origins, set a `Secure; HttpOnly; __Host-rcp_session` cookie, record what each
  server receives, and fail automatically on a missing or cross-space cookie.
- Generated `rcp-<connection UUID>.localhost` names resolve exclusively to
  IPv4/IPv6 loopback and were served on both. The real WKWebView requested the
  login endpoint, received the cookie response, followed the redirect, and sent
  no cookie on the next same-origin request. Exact `localhost` produced the same
  result. This rules out the proposed HTTP alias mechanism before isolation or
  restart can be claimed; it does not distinguish rejection during storage from
  suppression during sending.
- Distinct `127.0.0.2` and later hosts cannot be bound on the stock development
  Mac without privileged loopback-interface mutation. No network configuration
  was changed, and that mutation would not make an HTTP origin satisfy the
  already-failed `Secure`-cookie gate.
- The unbundled probe is a real Tauri WKWebView but is not exposed as a named
  macOS application to the accessibility driver. The server request log is the
  live behavioral evidence; no visual interaction claim is made.
- No production allocator, navigation rule, capability, connection record, or
  cookie policy was changed. D2 is not complete. Q11 asks whether to prove a
  desktop-owned per-space HTTPS endpoint with app-scoped certificate trust or
  choose a larger native transport design. D3 through D5 remain dependent on
  that decision; unrelated server and provisioning lanes remain available.

#### 2026-08-28 — O3a backup configuration and inert timer implemented

- `sudo rcp server backup configure` now requires one absolute destination, one
  checksum-valid native X25519 `age1...` public recipient, explicit `--confirm`,
  and configurable daily server-local time/archive count with defaults of
  `02:00` and 30. The strict request rejects relative/root/non-normalized paths,
  malformed recipients, private age identities, invalid times, nonpositive
  retention, partial requests, and configuration fields on other commands.
- The destination must already exist. A bounded, empty-environment helper runs
  through `runuser` as the exact installed `rcp` account, opens the directory
  without following its final component, creates one mode-0600 exclusive probe,
  fsyncs and removes only that probe, and returns no path or subprocess output.
  RCP does not classify or warn about local versus mounted storage.
- `/etc/rcp/server.toml` is now schema version 2 with one optional strict backup
  table. The reader upgrades the exact unconfigured version-1 shape in memory;
  it does not let a version-1 document smuggle in backup fields. Atomic
  publication retains installation id, source, and fixed paths and stores only
  destination, schedule, retention, and the public recipient.
- The source assets install one `rcp-backup.service` that invokes the future
  service-account `backup run` command and one timer rendered from the stored
  schedule. One root-owned `0600` advisory lock serializes configuration with
  installer convergence. Both paths fence a previously loaded timer before
  touching its units and prove it inactive and disabled after reload. A
  root-owned pending config makes an interrupted timer/config publication
  recover its exact intended policy before any later operation continues; only
  exact config, timer-text, and systemd-state readback clears that marker. There
  is no code path in O3a that enables the timer.
- The one independent O3a audit found four issues, all fixed without a second
  audit: early failures could precede the first timer fence; config and timer
  publication lacked serialization/recovery; `PrivateTmp=true` hid otherwise
  valid `/tmp` destinations from the future service; and the destination bound
  exceeded the CLI event bound. Focused O3a, shared CLI, installed-config,
  installer, and documentation verification now passes 150 tests. The internal
  destination helper, configured-file atomic round-trip, concurrent-lock
  refusal, and injected publication recovery were exercised against real
  temporary files. Focused Ruff and formatting checks pass.
- Not done: no archive is captured, encrypted, integrity-read, retained, or
  deleted; `backup run` remains unavailable and the timer remains disabled.
  No real root-owned `/etc` file or systemd manager was changed on this Mac, and
  no Ubuntu live drive was claimed. O3b and the later live milestone own those
  checks.

## What remains

Everything after the existing auth/membership foundation remains implementation
work:

1. F3b's real two-Ubuntu install qualification, then server health/doctor and
   source update;
2. private machine-local CLI-to-server control;
3. project-provisioning API projections and concrete machine orchestration;
4. central Git checkout and write-deploy-key setup;
5. local/remote provider readiness against authentication already present on
   each execution account;
6. source-built desktop distinct origins, tunnels, live Keychain
   enrollment/readback, navigation, cached team groups, and optional operator
   bridge (the strict metadata and token-write/remove substrate is complete);
7. app-visible project setup driven by the backend and prepared by the CLI;
8. encrypted online backup capture, safe timer enablement, retention, restore,
   and server status (strict configuration and disabled units are complete);
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
- The full private, single-developer pre-team-server implementation stays
  directly on local `main`; this handoff adds no short-lived-branch or PR gate.
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
- Local Web and desktop development may run any branch, but this implementation
  remains on `main`. Emergency fixes use the same scoped verification.
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

## Delivery-gate packets

### G0 — Restore the current `main` CI baseline

Own:

- formatting-only normalization of `src/rcp/api/tasks.py`,
  `src/rcp/runs/chat.py`, `tests/test_api.py`, `tests/test_episode_api.py`,
  `tests/test_unified_artifacts.py`, `web/src/rootRecovery.tsx`, and
  `web/tests/rootRecovery.test.mjs`;
- `src/rcp/storage/rows.py` and the focused runtime regression in
  `tests/test_background.py`, plus the superseded forward-column assertion in
  `tests/test_stored_request_compat.py`; and
- the active-handoff assertion in `tests/test_agent_instructions.py`.

Start directly from clean `main` commit `4e6d812`; its code is the `c0909b6`
baseline plus the planning-doc commit. This is a baseline repair, not team-server
feature work. Apply the repository's configured formatters mechanically to the
seven named files without changing their behavior. Replace the stale
documentation assertion that says there can be no active implementation handoff
with an assertion that distinguishes the archived closed backend-refactor
handoff from valid indexed active work.

Repair the real runtime projection regression rather than weakening its test.
`checkpoint_agent_task_runtime` currently writes the selected runtime and its
receipt, but `_agent_task_record` removes `runtime_id` before model validation;
the read model therefore silently reports the legacy Codex exec runtime even
after app-server was selected. Remove that obsolete pre-runtime compatibility
path and prove the runtime event is still read back before the provider-session
checkpoint. Do not change runtime selection, fallback, or provider-auth policy.

Before closing G0 on `main`, run the complete current lint/Python/Web
CI-equivalent baseline, including `uv run pytest` and
`uv run pre-commit run --all-files`, and read the diff to prove formatter output
did not hide semantic edits. G0 must be green before dependent packets begin.

### G2 — Old-data upgrade CI gate

Own:

- new `tests/test_server_upgrade.py` and its focused harness;
- upgrade/startup smoke helpers beside their concrete owners;
- a stable named job in `.github/workflows/ci.yml`; and
- immutable sanitized fixture bundles under
  `tests/fixtures/server_upgrade/<boundary>/` for every server-era persistence
  boundary, beginning with the first team-server-capable commit.

Build the exact candidate base, create representative prior data, then build the
candidate, upgrade a copy, start the complete backend with external/provider
effects disabled, and verify health, replay, startup recovery, and key
projections. For direct local work the base is current `main` and the candidate
is the working tree; for committed CI the base is the candidate's first parent.
Exercise every historical boundary fixture as well as that exact base. A
fixture contains the small SQLite database and any canonical history needed for
realistic replay/recovery, is produced while its boundary is current, and is
never regenerated by newer code. New persistence changes add a boundary fixture
before the old shape leaves `main`. Fixtures have no rolling expiry; dropping a
boundary requires a separately approved migration path. The check must pass
before a persistence-changing slice is recorded or pushed and test the exact
candidate. The later public branch-protection gate makes it GitHub-required.

The on-server actual-data rehearsal and update-local restore boundary remain in
F6a–F6d, while disaster restore remains in O4; CI evidence never substitutes for
that server-specific preflight.

## Server-foundation packets

### F1 — Server CLI command and event contract

Status: complete in the working tree on 2026-08-28. Its one independent audit
found buffered rather than live progress, secret-channel gaps, incomplete
privilege-matrix coverage, and stale status prose; all four were resolved before
closure. Concrete operations intentionally remain owned by later packets.

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
- a versioned bounded ordered-step record with command, step number and title,
  purpose, `performed_by` (`system` or `human`), phase, state, expected success,
  message, timestamp, optional nonsecret fields, and a discriminated target: a
  machine target has host and OS account; an external-service target has service,
  resource, destination URL, and required authority role but no invented user
  identity; an operator-action record also has ordered safe argv or external UI
  actions, nonsecret values, plain success signals, and exact recheck or resume
  argv;
- interactive and `--machine-readable` renderers over the same command result;
- an initial plan event followed by one event when each step starts, succeeds,
  fails, or pauses, so an interactive user and the wizard see the same complete
  workflow rather than reverse-engineering it from diagnostics; and
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
Prove the interactive renderer and structured event contain the same operator
action without making the desktop parse prose or invent a missing command.

### F2 — Linux service layout and explicit paths

Status: complete in the working tree on 2026-08-28. The fixed layout, strict
machine config, systemd asset, and focused regressions are implemented; F3a and
F3b remain the concrete installer and two-Ubuntu live proof.

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

Status: complete. The audited installer is commit `638c19e`; the immutable
chained fixture `source-server-install-v7-638c19e` names that exact commit and is
pinned by the G2 registry. F3b owns live Ubuntu qualification, not another
installer implementation.

Own:

- new `src/rcp/server_ops/install.py`;
- `src/rcp/server_ops/assets/rcp.service`;
- `tests/test_server_install.py`; and
- the first immutable server-era fixture under
  `tests/fixtures/server_upgrade/<first-server-boundary>/`, produced by the
  exact first installable team-server commit through G2's harness.

Deliver an explicit root/operator installation that:

1. validates x86-64, systemd, Git, `uv`, Node.js 24/npm, SSH, and `age
   >=1.0.0,<2.0.0`, then installs or validates the `uv`-managed Python 3.12
   service runtime as `rcp`, without changing apt sources or installing general
   system tools;
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
   exact installer resume command, then exits with the fresh service stopped.
   The installed wrapper resolves the configured `RCP_DATA_DIR`; the operator
   runs initialization in that terminal so neither another process nor a
   service log receives the one-time bootstrap code; and
8. only after successful initialization does the resumed root CLI enable/start
   systemd and read back process and HTTP health without widening the loopback
   bind. F5 later makes the printed `server doctor` readback authoritative. A
   rerun against an already initialized owned team data directory may converge
   the service to running.

`--team-name` is a required strict install-request field. It exists so the CLI
can independently print exact `space init` and resume argv. The future wizard
submits that same request and renders the same structured plan/events; it never
owns a private setup branch or fills in omitted machine instructions.

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
- `tests/test_server_install_live.py`; and
- `.github/workflows/server-install-live.yml` for the fixed two-release drive.

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

- new `src/rcp/server_ops/github.py` and
  `tests/test_github_repository_ref.py` for the single GitHub.com source parser;
- `src/rcp/storage/models.py`;
- schema/migration additions in `src/rcp/storage/base.py`;
- new `src/rcp/storage/provisioning.py` mixed into `AppStore`; and
- `tests/test_project_provisioning_storage.py`.

Model one request id, kind (`create_team_project` or incoming transfer), target
space, human authorizer, proposed canonical project id, canonical
`GitHubRepositoryRef` values, the fixed local central root or one requested
absolute SSH central root,
intended/resolved paths, Git and provider checks, timestamps, retryable
diagnostic, final-review digest, and explicit cancellation disposition. A new
project request mints one random proposed `project_id` when the request is
created; an incoming transfer uses the source project's existing id. This
reserves a collision-resistant path namespace only. It does not append project
identity, register a project, or establish a writable home before final human
review.

Accept only `https://github.com/<owner>/<repository>[.git]` and
`git@github.com:<owner>/<repository>[.git]`. Normalize them through the shared
parser before storage. Its accepted owner has 1–39 alphanumeric-or-hyphen
characters and begins and ends alphanumeric. Its repository has 1–100
characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, and `-`, other than `.` or `..`.
Strip one exact optional `.git` suffix and store a lowercase
`owner/repository` identity. Reject
credentials/userinfo, query/fragment text, percent-encoding, traversal, local or
`file://` paths, `ssh://`, arbitrary hosts, ports, and extra path components
before a row, filesystem access, DNS lookup, or other network call.
Generate fixed clone and repository-settings URLs from the canonical identity;
no later owner consumes the member's raw string.

An **operator action needed** transition stores a bounded structured action,
not arbitrary shell prose: `performed_by`, the same typed machine or
external-service target, ordered safe command tokens or external UI steps,
nonsecret values, expected success, and exact resume command. It may include a
GitHub deploy public key but never a private key, provider token, SSH secret, or
member credential. A GitHub action targets `github.com`, canonical repository,
settings URL, and repository-administrator role; it does not claim to know that
administrator's account.

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
that backend's product eligibility, preselection, primary action label, required
fields, and any pinned source identity. The three possible visible intents are
**Use an existing checkout personally**, **Create a shared team project**, and
**Move an existing personal project to a team**. D7 separately consumes the
native bridge's relay capability and authenticated saved targets. It offers move
only when the personal backend permits export, the selected team backend permits
import, and that native capability can connect them. A browser has no native
answer and cannot offer move. Both the index action and direct
`#/projects/new` navigation render explicit answers rather than branching on
`space_kind` or paths.
Personal setup, durable provisioning, and linked transfer keep their separate
APIs and authority despite sharing one wizard.

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

Consume only P1's canonical `GitHubRepositoryRef`. Derive the fixed
`git@github.com:<owner>/<repository>.git` clone URL and
`https://github.com/<owner>/<repository>/settings/keys` operator URL from that
identity; never pass a request-supplied URL to Git or use it as an SSH host.

The deploy key is the checkout's GitHub identity; RCP never needs a GitHub user
login on the server. Before the grant exists, publish one structured operator
action containing the exact repository settings destination, label, public key,
write checkbox, expected probe, and `project provision` resume command. A human
with repository-administration authority installs it through GitHub. Interactive
CLI output renders the same action; desktop output does not add private steps.

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
instruction for the human to push their local-only code through their ordinary
GitHub workflow and the command to recheck; RCP does not create a GitHub
repository, upload/adopt a member checkout, take a GitHub token, or invent a
hidden initialization commit in this slice. Never place a private key in SQLite,
the manifest, logs, structured output, prompts, or backups.

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
directly as that local or remote account, the expected readiness signal, and the
exact `provider check` or `project provision` command to resume. Interactive and
machine-readable modes carry the same structured action. Persist only nonsecret
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
The interactive command is sufficient to complete every machine step without
the wizard: at each pause it prints the concrete account/action/success/resume
contract. The desktop is a structured renderer and fixed launcher for that same
workflow, not a second implementation.

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

**Current result:** stopped at that condition on 2026-08-28. The reproducible
real-WKWebView probe is retained under `web/src-tauri/examples/` and
`web/src-tauri/scripts/`. Both generated `.localhost` aliases and exact
`localhost` failed to return the required `Secure` cookie to the server over
HTTP; the extra-address path could not reach WKWebView because stock macOS could
not bind those addresses without privileged network mutation. See
[Q11](../open-questions.md#q11--how-should-the-desktop-provide-isolated-secure-local-origins).
Do not implement a production origin allocator until that question is decided.

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

### D7 — Unified project wizard provisioning mode

Own:

- `web/src/views/ProjectSetup.tsx` as the one visible wizard, with optional
  focused step components that never become another top-level wizard;
- `web/src/App.tsx` routing and `web/src/views/ProjectSettings.tsx` deep-link
  contract;
- P2 integration in `web/src/api.ts` and `web/src/types.ts`; and
- browser plus desktop tests.

Extend the current wizard with plainly named personal and new-team intents; T5b
later activates move mode in this same shell. Render the backend's six statuses,
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
action. Until T5b lands, the backend does not offer the move intent; D7 does not
show a half-built transfer state or create a separate future transfer wizard.

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

Status: implemented in the working tree on 2026-08-28; focused verification and
the one independent audit are complete. O3b still owns every archive side
effect and the only transition that may enable the timer.

Own:

- new `src/rcp/server_ops/backup_config.py`;
- the backup section of `src/rcp/server_ops/config.py`;
- new `src/rcp/server_ops/assets/rcp-backup.timer` and
  `src/rcp/server_ops/assets/rcp-backup.service`;
- narrow root-command and unit-installation wiring in
  `src/rcp/server_ops/cli.py` and `src/rcp/server_ops/install.py`; and
- `tests/test_backup_configuration.py`.

`backup configure` explicitly records a destination, schedule,
retention, and `age` public recipient. It never accepts or stores the private
identity.

Persist those four values in F2's versioned installed-server config, not SQLite,
using root-owned atomic replacement. Propose daily at 02:00 server local time,
retain the newest 30 integrity-readback archives, and additionally retain the
newest complete archive when it is older than that window. Require explicit
confirmation or edited values before enabling the systemd timer. Render and
read back the timer from the same resolved schedule so configuration cannot
drift from execution.

Serialize install and configuration through one stable root-owned lock. Fence
an existing loaded timer before unit mutation and again after reload. Journal
the complete intended public config before mutation, recover that exact record
after interruption, and clear it only after exact config/timer/systemd
readback. The service must see every accepted destination, including `/tmp` and
`/var/tmp`; do not add a private temporary namespace that changes path meaning.

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
- every secret and account boundary above has a negative verification; and
- the genuine live lab drill passes and is documented without credentials.

## Suggested skills for pickup

- The design grilling and final cross-document fact-check are complete. Dispatch
  G0 directly on `main`, then begin G2, F1, and D1 according to the dependency
  map; all decisions are settled. Implement the remaining packets without
  reopening product boundaries unless current code contradicts their authority.
- Use `computer-use:computer-use` for the real source-built desktop drives in
  D2, D4a, D4b, D6, D7, T5a, T5b, and V1; browser tests cannot prove native SSH,
  Keychain, cookie-store, or navigation behavior.
- Use `codex-security:security-diff-scan` after the credential/control-socket,
  SSH bridge, backup, and restore packets, scoped to their actual diffs.
