# Server and machine operations

This spec owns the rules for installed-server authority, deployment, checkouts,
provisioning, transfer and backup.
[Projects and spaces](projects-spaces-and-operations.md) owns identity and
membership. [server.md](../server.md) owns operator procedures.

## Agent secret hiding and repository authentication

The backend owns one deploy-key `ssh-agent` per OS account. An account lock
coordinates backends with different data directories. Only its owner starts and
stops the agent. Contenders may load their own keys without taking lifetime
ownership. The stable private socket under `~/.rcp` survives the service's
`PrivateTmp` boundary and remains separate from the user's `SSH_AUTH_SOCK`.
Startup starts the agent before recovery launches. Shutdown drains workers
first. Provisioning, restore and rotation load new keys immediately when it is
running.

Repository transport retains its `core.sshCommand` and adds `IdentityAgent`.
Keys become hidden only after a matching fingerprint and a bounded signing proof
on the launch host. Remote launches use an account-owned systemd or launchd
agent and a shipped helper. Private keys remain on their owning machine. Missing
agents or failed confirmation leave the affected key readable so Git continues
to work. [ssh_agent.py](../../src/rcp/ssh_agent.py) and
[remote_ssh_agent.py](../../src/rcp/transport/remote_ssh_agent.py) own this
evidence.

Settings and doctor expose missing hiding support, blocked namespaces, readable
keys and the macOS browser exception. Remote status remains unchecked until
launch. Local status includes account-key evidence. Gaps are reported as
unhidden in the prompt without blocking provider launches or reducing browser or
Git capability. This protects selected secrets from generic network prompt
injection. It is not a separate account or a hostile same-account sandbox.

## External dependencies

[dependencies.py](../../src/rcp/dependencies.py) owns roles, platforms and
whether a dependency is required or optional.
[dependency_check.py](../../src/rcp/dependency_check.py) checks machines against
it. Installation, doctor and machine cards share this registry. Missing required tools cause launch refusal.
Optional gaps name their feature and its visible limitation. Feature-specific
versions stay with their concrete owners.

Dependency admission checks local macOS and Linux in-process and remote Linux
over state SSH with nonce-bounded output. Unsupported remote OSes and definite
missing tools cause refusal before task creation. Unreachable hosts, timeouts
and unreadable results remain `not checked` and allow admission. Cached missing
results are rechecked before refusal. Remote helpers use their declared
standard-library Python compatibility floor.

[state_transfer.py](../../src/rcp/transport/state_transfer.py) selects rsync by
required flags and protocol, caches completed host verdicts and respects PATH
order. Transport failures remain unavailable-host outcomes, not capability
failures. Only synchronization or publication of canonical state and publication of
run-stage inputs use its tar fallback. Backups, kept files, views and source
indexing need rsync.

The tar fallback validates before changing the mirror. It publishes complete
temporary siblings and preserves the root. It converges on retry. It announces the first
fallback per host in logs and task events and exposes the selected engine in
project readiness. The fallback does not block work. Dropped rsync streams share
one retry owner. No implementation duplicates a remote helper inside a command
string.

## Remote display state

Remote reads pin immutable local generations for their whole operation.
Background capture takes the remote refresh lock and downloads privately. It
validates the download and atomically publishes a descriptor. Cold reads report unavailable and queue
capture without transport waits or state initialization. Old generations survive
until readers exit. Failures retain the last copy and report sync health
separately.

The lifespan coordinator combines membership-filtered open sets under expiring
leases. Activation bypasses the cadence and coalesces a follow-up. Shutdown
drains workers. Changed paths advance graph, branch, chat, Paper and Settings
tokens. An unchanged capture updates health only. Local state uses direct reads
and counters.

Capture and authoritative publication share sequencing. Writes retain canonical
transactions. Execution reloads the authoritative scope after preparing display
inputs. Pause needs no graph. Stop commits its SQLite fence before rebuilding
controls. Retry, Resume and chat receipts commit with admission. Generation
ownership lives in
[transport/generations.py](../../src/rcp/transport/generations.py).

## Confirmed first team-server target

The supported target is one Ubuntu 22.04 or 24.04 LTS x86-64 server running
systemd, one team space, and macOS desktop clients. Installation refuses other
targets. The server runs promoted, verified wheels, lock files and Web assets
without source reload. [Current exclusions](../design.md#current-exclusions)
owns product limits.

The dedicated service account has no usable password, general sudo or privileged
supplementary group. Installation owns its home and exact non-locking unusable
shadow value. It does not change SSH password policy. Direct key-only access is
an explicit operator grant. Named operators with narrow sudo support independent
revocation. RCP does not select human accounts or edit their sudo policy.

The account owns releases, data, checkouts, keys and application Python. Root
owns the supervisor runtime, selected receipts, journals and system integration.
Paths and modes are defined by
[server_ops/layout.py](../../src/rcp/server_ops/layout.py). Installation
enables and verifies service-account linger. Updates retain its choices.
Application preparation uses umask 077 and validates ownership and modes.
Browser library installation is best-effort during root installation or update.
Failure is visible. Browser readiness supplies the needed operator action.

The service binds to loopback. SSH transport, RCP member identity, repository
deploy keys, provider login and execution-account ownership are separate
authorities. Project members are trusted as the service account, including its
provider logins. Terminal containment guards accidents, not hostile same-account
users. There is no read-confidentiality promise. Member checkouts remain
independent. Remote resources belong to the configured account. Provider-native
state remains in that account's home, outside protected backups. The random
installation identity is distinct from space and member identities.

Provider updates run the native updater as the service account. They never
collect credentials. They check the executable and login independently.
Discovery saves stable command paths rather than resolved versioned symlinks.
Product login behavior belongs to
[Providers and containment](providers-and-containment.md#login-state-and-failure). Operator actions belong to
[server.md](../server.md#11-provider-authentication).

### Release check and update notices

[release_check.py](../../src/rcp/release_check.py) checks stable releases and
verifies the same-commit desktop companion and assets before advertising it.
Concurrent readers use bounded cached status. Failures retain the check time and
a safe reason, not response bodies. They cannot alter installed-build integrity.
Team status uses the selected receipt and reports pins. Doctor performs its own
lookup. `RCP_UPDATE_CHECK=off` disables calls. Requests contain no credentials,
project data or installation identity. Timing constants live in `limits.py`.

Source serving holds a shared checkout lock. The source updater needs exclusive
ownership before checkout or build. It restores the starting revision on
failure.

## Machine authority and the operator surface

There is no administrator member role. Installation, deployment, backup,
restore, credential provisioning and removal of another human require machine
authority, not a member token. Root coordinates installation, restore, update
and supervisor replacement. It drops to the service account for application and
provider work. Privileged commands use a separate root-owned runtime, never the
service-owned application environment. A command using the wrong identity
refuses before durable work.

**Why:** account creation and system integration cannot depend on an operator's
checkout or home or be delegated to the account being installed.

Bootstrap verifies promoted artifacts without fetching a source tree. Fresh
installation leaves the unit stopped and disabled until team initialization and
the operator's confirmation that the bootstrap code was saved. Interrupted
installation retains an exact continuation. Existing initialized spaces converge
to running. SQLite is owner-only before the code is exposed, including during
resumed installation.

Online commands use the private service-account control socket. Offline work
proves that the service has stopped and takes the instance lock. The
authenticated, versioned probe advertises supported operations. No member HTTP
route grants machine operations.
[server_ops/control.py](../../src/rcp/server_ops/control.py) owns protocol
negotiation. [models.py](../../src/rcp/server_ops/models.py) owns CLI requests
and events rather than a copied field inventory here.

Interactive output has one current-step line. Stops identify the target, ordered
actions, nonsecret values and exact continuation. Enter performs the declared
actions. Quitting, EOF or a disconnect pauses the operation. Machine-readable
output is append-only. It never prompts or performs human actions. Neither
output mode emits secrets. Failures preserve a bounded cause, safe-state
evidence, recovery steps and diagnostic reruns. Authenticated refusal text is
not accepted from an unverified peer.

Member removal previews the affected work and credentials. It refuses removal of
the last enrolled member or a sole project member. Otherwise, it atomically
fences access and revokes tokens, sessions and unconsumed invitations. The user
remains a tombstone. The owners of graceful shutdown settle work. Apply rechecks membership.
After the member's workers stop, removal deletes their local folder under
`<data_dir>/agent-threads`, their agent schedules and their undelivered
handoffs, and closes their threads. Private project transcripts, member memory
notes and remote thread stages stay until the project or host is cleaned.
Service connections are removed under their lock. Startup and re-entry resume
unfinished removal. Self-revocation has the same anti-stranding rule. Rotation
returns a replacement first. Operators cannot assign memberships to bypass a
guard or mint user credentials.

Provider journaling records process start and end identities, executable,
runtime, capability, host, PID and operation. End records add status, elapsed
duration and RCP's failure classification. Requested stops are expected. Raw
provider output and stderr stay on the task row, never in journald, because they
may contain secrets. [agents/launcher.py](../../src/rcp/agents/launcher.py) owns
this boundary.

### Recurring service errors

On Linux, doctor reads `rcp.service`'s own journal over a recent window. It uses
the same account and runner as its `systemctl` reads, and reads no other unit.
It groups `ERROR` and `CRITICAL` log records by logger, exception type and the
function of the traceback's final frame. Message text never forms the key or
leaves doctor, because it can carry ids, paths and secrets. A signature seen
often enough in the window is a problem with its count and first time seen.
A rarer one is a `service_log_warnings` entry. An unreadable journal is one
warning and never fails doctor. The desktop app has no service journal and skips
the read. [server_ops/service_errors.py](../../src/rcp/server_ops/service_errors.py)
owns parsing and grouping. `limits.py` owns the window, the count, the entry and
character caps and the number of signatures shown.
**Why:** after an update, a background failure that retried every minute
appeared only in the journal while doctor reported healthy.

### Compute backend probe

Compute probing is a service-socket operation that persists independent route
results and succeeds only when the requested route is ready. Its rule owner is
[Machines and compute](machines-and-compute.md#readiness-and-setup). Commands
belong to [server.md](../server.md). It grants no scheduler authority.

### Package identity and offline storage migration

Version reporting returns the package identity without serving. It explicitly
reports absent source build and commit values.
[server_ops/models.py](../../src/rcp/server_ops/models.py) and the CLI own
machine-readable shapes.

Offline migration resolves the same data root as serving and first takes its
exclusive instance lock. Check mode opens existing SQLite read-only, including
the committed WAL. It classifies the state as fresh, current or pending and
returns a success status. Unknown or ahead ledgers, invalid SQLite, unrecognized
nonempty pre-ledger data or failed validation cause refusal without database
mutation. Normal SQLite sidecars may change. Actual migration runs the known
ordered chain and validation without serving.

Installation converges. There is no uninstall verb. Operator teardown must
preserve the backup recovery identity or explicitly accept undecryptable
archives. Operators must separately revoke external grants. Removing the account
removes its owned data, checkouts and keys. [server.md](../server.md) owns
teardown commands.

## Keeping a Mac awake

Personal macOS spaces have an enabled-by-default idle-hold preference in SQLite,
not manifests. The team API rejects it. Other platforms report it as
unsupported. [machine_power.py](../../src/rcp/machine_power.py) owns demand and
platform profiles. [machine_sleep.py](../../src/rcp/machine_sleep.py) gates
automatic launches until wake stabilizes.

Demand holds `caffeinate` for the backend lifetime without root or lasting
state. Display sleep and screen locking do not release it. Lid closure or OS
emergencies may. Locally owned episodes count when they are starting, active,
recovering, stopping or wrapping up. Admitted tasks, nonidle runtime and scheduled transport
retries also count. Human-only waits, ended work, sign-in-blocked tasks and
armed watchers alone do not. Failed reads retain the hold and log a failure
rather than infer completion.

### Lid-closed mode

Opt-in lid mode sets `SleepDisabled` only during demand. **Why:** idle
assertions do not prevent lid sleep, and an independent watchdog must restore
sleep when the backend dies. Administrative installation grants only the two
exact pmset flag operations and installs a boot-reset daemon. Foreign files or
accounts cause refusal. Uninstall clears and verifies the flag before removing
the integration.

A machine-wide advisory lock owns the flag. A flag without an activation record
is external ownership, never adopted or cleared. Only the detached watchdog sets
it. Heartbeats bind the generation, process identity and demand. Stale or
mismatched heartbeats cause the watchdog to clear and read back the flag, revoke
the generation and sleep unless the lid is known open. A resumed backend cannot
re-arm a revoked generation.

Safety checks read the battery, thermal, lid and flag state through macOS
frameworks without a command fallback. Low battery off AC, serious thermal
state, unreadable state, incomplete installation or a failed watchdog releases
lid mode. The idle hold remains demand-driven. Ending demand releases both
without sleeping an open Mac. Thermal or cleanup failures latch the mode off
across restart until a human reenables it. Battery release re-arms on AC. A
failed clear retains its cause and asks for human repair instead of launching
repeated watchdogs. Preferences, latches and thresholds stay with the owner.

## Release selection, deployment, and automatic recovery

**Why:** an independent supervisor can recover when a new app cannot start, and
promotion preserves tested bytes. The app owns admission and semantic proof. The
supervisor owns selection, journals and filesystem rollback.

Every qualifying main CI run publishes one `build/<N>` prerelease from the
already-tested artifacts. Main runs do not cancel earlier builds. App version
stamping binds the base version, build number and commit. The supervisor version
is independent. Completeness of the wheels, hashed locks and manifest is
enforced by [release_build.py](../../packaging/release_build.py) and
[CI](../../.github/workflows/ci.yml), including old-data and installed-upgrade
gates.

Only a human promotes a release.
[Promotion](../../.github/workflows/promote.yml) verifies the base version, tag
and commit. It refuses existing tags or releases. It copies the same assets
without rebuilding. Historical complete asset sets remain promotable. A partial
supervisor pair causes refusal. Installation requires supervisor assets.
Promotion also refuses without a passing qualification receipt. Each receipt
entry is one [update rehearsal](#application-boundary-and-local-checkpoint) on
copied team data. It holds only its status, which must be `ready`, and the
SHA-256 of this build's `manifest.sha256`. The receipt is public, so any other
field refuses. It names the desktop candidate run, which must be
the run promotion publishes. The CI gates, including the browser journey, are
already implied: a `build/<N>` exists only after they pass. **Why:** 0.4.35
passed CI but broke on real records.
[Build pruning](../../.github/workflows/prune-builds.yml) removes only expired
build prereleases and tags under `STALE_BUILD_DAYS`. Stable releases stay
retained. **Why:** build expiry bounds CI artifacts while promotion preserves
tested bytes and a permanent human-selected release.

The separately versioned `supervisor/` distribution imports no RCP modules.
Installed deployment commands delegate through the operator event envelope.
Planning has no side effects. Non-deployment operations keep application owners.
Strict configuration and selected-receipt types live in
[server_ops/config.py](../../src/rcp/server_ops/config.py) and
[supervisor releases](../../supervisor/src/rcp_supervisor/releases.py).

Stable follows the newest non-prerelease release. An explicit pin selects an
exact tag. Unsupported selectors, missing assets, altered hashes or inconsistent
wheel and manifest identities cause refusal without choosing another source.
Metadata binds the full commit to the wheel suffix. The root-owned selected
receipt and current pointer must agree before launch. The operator-set HTTPS
team address is stored separately from pinned release configuration and is
read-only to members.

Update confirms an exact tag and manifest digest, then prepares an isolated
release directory without overwriting failed or existing builds. A required
newer supervisor must be installed first, under the same operation lock.
Supervisor replacement switches its validated root-owned runtime without rolling
back application data.

Update rehearsal snapshots online SQLite and copies local project roots into a
disposable output. It then runs candidate inspection, preparation and proof
there. It never changes live admission or takes the live instance lock. A ready
result covers that captured copy, not a later cutover. Warnings identify omitted
projects. Rebinding local stage paths preserves shared workspace identity across
stored records while placing those paths under an isolated, absent directory.

### Application boundary and local checkpoint

Deployment first attempts a protected backup. An incomplete or unreadable
capture produces an explicit warning without refusing the update. Authenticated
maintenance closes mutations, launches, watchers, machine work and recovery. It
drains entered work and captures SQLite at that quiet boundary. A timeout causes a clean refusal. Human-input waits park under closed admission rather than prevent
drainage.

The candidate inventories captured local roots and rechecks failed backup
inventories. Unreadable projects are named and excluded from copying and live
proof. Identity, ownership, path, migration and proof failures on proven
projects still cause refusal. Capacity is estimated per filesystem. Actual copy
errors determine success. There is no fixed ceiling on scientific-history size,
entry count or time.

After the service is proven stopped, root snapshots the entire data directory
and all registered local canonical roots. The snapshot collapses nesting and
covers candidate replacement roots. Credentials, scratch, unknown entries, empty
directories and special nodes survive. Provider homes, source checkouts and
remote canonical roots are outside replacement scope. Candidate proof never
grants rollback power.

**Why:** typed backup inventory can lag valid application data. Whole-root
stopped snapshots restore it exactly. Backup warnings remain visible without
blocking all updates. Stored-data readers enforce reader safety, not current
writer rules or secret-redaction rules misapplied as storage validation.

Pre-selection effects are fenced and local only. Remote live checks use current
local display caches or leave projects unopened. Copied main and branch heads
replay and must match. Remote projects open only after durable selection. Proof
exercises API projections, replay and merge receipts, retained tasks and stages,
imported history and startup recovery with the application lifespan. A migration
check alone is insufficient. Rollback runs the old binary's own fenced proof
without making it parse the candidate's newer schema.

Both copied-state checks start from the live display caches, which backup
omits. Before any project opens, and again after every project has opened,
each member reads the Runs list, the space and project Experiment indexes,
resource watermarks and each project's consolidation Inbox. Any non-200 read,
or an Experiment index row that cannot read a copied project, refuses. **Why:** project opens rebuild caches, so only a cold read finds old
cached bytes that a space-wide read cannot use. A restore has no caches to
carry and makes only the second pass.

Due consolidation then runs on the copy with the fence still closed. Copied
queued runs launch first. Then the copied clock advances to each schedule's next
due time, so real admission writes new rows. Every launch of a copied project
goes through the engine's own pre-spawn validation, `validate_admitted_launch`,
and stops where a provider would start. A validation error, a run that never
reached validation, or a new run with an error code refuses. Provider sign-in
and machine programs are not part of the copy, so their deferral is accepted.
Watchers and other scheduled dispatch are not exercised.

The candidate check also captures each application log record at WARNING or
worse, with the check phase that logged it. Any such record refuses, naming
the phase, logger and message. The one exception is a short commented
allowlist in
[application_validation.py](../../src/rcp/server_ops/application_validation.py).
An allowlisted warning still refuses when it names a copied project. **Why:**
the 0.4.35 consolidation failure showed only in the log, and an ended
Experiment on an unusable cache is only logged.

Projection upgrades verify the baseline digest, remove retired fields and fill
only proven deterministic defaults. They preserve explicit values, unknown keys,
edge layers and serializer-owned operations. Display caches upgrade or replay
against the same proof. Unproven factories require a migration. Compatibility
keeps frozen sanitized fixtures written by each supported old persistence
boundary. **Why:** skipped releases and old writers cannot be verified with
current models.

The deployment source floor is `UPDATE_SOURCE_FLOOR` in
[deployment.py](../../src/rcp/server_ops/deployment.py), not a copied release
number here. It is the oldest release CI updates from. A server below it still
updates, with an untested-path warning; a failed update rolls back as usual.
**Why:** the floor bounds what CI proves, not what an operator may try.
Uncomparable identities require receipt repair or adoption. Old data still uses
its migration chain. Raising the floor requires explicit review, not fixture age.

CI qualifies published stable releases at or above the floor and frozen
fixtures, using production installation and update owners, populated old data
and offline scratch. Failure injection verifies rollback and old-code use before
retry. Pull requests and main install every one. After the committed update, the candidate's service journal
must hold no ERROR line or traceback, and the seeded question follow-up must
still read as succeeded. The aggregate gate promotes those same bytes. It does not
prove arbitrary installations, provider authentication, remote-root behavior or
reboot recovery.

The web suite's
[release-upgrade browser journey](../../web/tests/releaseUpgrade.browser.test.mjs)
serves the data the update rehearsal just passed, beside a fresh personal space.
A member signs in to both, then opens the team Runs list before any project,
the project, Runs, Inbox, a chat and the saved artifacts, and returns to the
personal space. The cold Runs list must list the Experiment on the old cache.
Failed responses, requests, console errors and page errors fail it, except for
the signed-out identity probe and its console error, and navigation-cancelled
requests reported as `ERR_ABORTED`. A failure keeps `evidence.json` with both
server logs.

The privileged checkpoint copier preserves metadata, links, special nodes and
cross-root hardlinks on each filesystem. Process connections do not survive.
Payloads live outside captured roots. Copy and filesystem synchronization finish
before readiness. Rollback journals each root, quarantines live trees, restores
saved trees (including original absence) and syncs parents. Recovery resumes
from journal and path evidence. Consumed checkpoints never overwrite accepted
later work.

Preparation and operation locks separate staging from startup recovery. Pruning
requires no unfinished operation and no mismatch between the selected receipt
and current pointer. It follows confirmed successful operations or an explicit
invocation. Retention limits belong to
[supervisor limits](../../supervisor/src/rcp_supervisor/limits.py). Payloads
and quarantines are removed before journals. Selected and executing runtimes and
adoption state stay protected. Success clears bounded diagnostics and staging.
Service Python and provider caches remain. Unknown files are reported and left
untouched. Research and agent scratch have no pruning byte quota.

### Selection and startup guard

The supervisor journals before effects. Candidate probation runs as the service
account with admission closed and proves the exact build and private state
before stopping the candidate. Durable `candidate_chosen` ends automatic
rollback authority. Only afterward are the selected receipt and normal service
published. An earlier failure restores stopped roots and runs old-code proof.
Failed candidate trees remain quarantined and are reported.

The root startup guard recovers from local code, journals and checkpoints
without network. It completes rollback before startup. It never recursively
controls its own unit. Coordinator contention grants nothing. Startup still
requires a durable choice and a matching receipt and pointer. Unknown, corrupt,
inconsistent or multiple active journals cause refusal.

After the candidate or previous release is chosen, recovery verifies the chosen
state without replaying captured proof over accepted work. Fresh-host rollback
leaves an uninitialized service stopped. `create_app` never chooses releases or
rollback. Read-only doctor and Settings projections cannot authorize startup.
Missing public restore timestamps mean unavailable history, not proof that no
restore occurred.

### Source installation adoption and qualification

Legacy source adoption stages integration beside the active install and journals
its files and full commit. The startup guard precedes stop. Disposable SQLite
inspection determines the roots before one stopped whole-root snapshot.
Candidate replacement roots must fit. Activation requires a complete protected
capture. Failure before choice restores data and integration before the old
source starts. Failure after choice preserves candidate data.

Reboot qualification uses disposable Linux VMs and interruption at journal and
root boundaries, including repeated rollback and network-free recovery. Changed
boot identity is required. A process restart is not reboot proof. Production
promotion requires the corresponding qualification and human approval.

## Central checkouts and repository credentials

A team project prepares one managed checkout per declared repository, under its
owning local service or configured remote account, before registration. Explicit
central-root intent or exact-account default resolution binds final reviewed
paths. Ownership, modes and symlink-free ancestry are checked. Manifest paths do
not authorize a later root choice. Personal checkouts are not auto-discovered.

Repository sources are GitHub.com or server-only. `GitHubRepositoryRef` in
[server_ops/github.py](../../src/rcp/server_ops/github.py) owns URL validation.
Credentials, traversal, alternate hosts or ports and unsafe components cause
refusal before input or output. GitHub uses a write-enabled deploy key per
repository, including public repositories. RCP collects no GitHub user token or
OAuth credentials.

Private keys remain only on the checked checkout account in RCP-owned private
storage. A shipped helper validates the uid, home and modes. Neither the
manifest nor shell HOME can choose a key root. Keys never enter SQLite,
manifests, prompts, diagnostics or backups. Public labels and fingerprints
remain recovery evidence.

Write proof creates, reads and removes a request-scoped ref to existing history.
An empty source instead uses the announced initial `Start RCP project` commit
and reads back the non-forced push. Retry reuses the recorded commit.
Conflicting pushes need human repair. Server-only setup creates the same initial
history without a remote, key or push. Its code is explicitly not backed up;
backup and doctor list it as a warning, not a problem.
**Why:** Git-backed research can start without GitHub, while research archives
remain separate from source code. Restore recreates empty server-only checkouts.
Adopted folders must already exist at their recorded path and commit.

Cancellation records explicit key and checkout cleanup or reuse. A granted
public key requires confirmed revocation or preservation. Deleting the private
key does not revoke the grant.
[Project deletion](projects-spaces-and-operations.md#deletion) leaves central
checkouts, canonical history and deploy keys for operator disposition.

## Durable project provisioning

The wizard creates a durable request before machine work. The browser and
desktop can prepare team setup. Personal transfer uses the desktop's
authenticated two-space relay.
[storage/models.py](../../src/rcp/storage/models.py) owns durable request and
review records.
[api/project_provisioning.py](../../src/rcp/api/project_provisioning.py) owns
API input types. The state and exact next actions come from the backend. They
are never inferred from files or subprocess exit. Reserving a project id grants
no canonical identity.

Machine preparation resumes bounded steps for paths, keys, checkouts, readiness
and write proof. Each step checks for a stale boundary. Empty-repository commits
are announced before execution. Only ready-for-review readback proves
preparation completed. Members may inspect requests. A new-project authorizer
can cancel through the member API only before machine work and with nothing to
remove. Afterward, the machine owner records the cleanup or reuse disposition.

Creation checks for an existing identity or Patch history before overwrite and
again at review. Either causes refusal. It never adopts retained research. Personal history requires
transfer. Desktop setup needs a separately proven operator SSH route using
system SSH, without collecting private keys or sudo passwords. Interactive work
opens the fixed command in Terminal. Browsers receive an operator command to
copy.

Final completion requires the exact review digest and a current member while the
preparation authorizer remains enrolled. It rechecks configuration and profiles,
paths, Git and provider proof and retained inputs. It then publishes the
manifest, one created-identity Patch, registration and seating through
recoverable boundaries. Retry accepts only the request's exact identity prefix.
Stale or conflicting inputs cause refusal. Settings requests to add or connect
repositories use the same review path. Their scope is owned by
[Repository and truth scope](projects-spaces-and-operations.md#repository-and-truth-scope).

Transfer links bind the source summary, compatible formats, aliases, state and
truth provenance and manifest digest. The target rebuilds configuration without
source paths. Drift causes refusal before source fencing. Final desktop review
records target admission then source release through independently authenticated
actors. Target confirmation alone creates nothing and leaves the source
writable.

Each side commits a hash of an independent one-time proof. The source reveals
its proof only in the sealed post-fence archive. The target reveals its proof
only after activation. **Why:** request ids, client JSON and CLI success cannot
prove the other space's transition. These proofs protect product authority, not
data from root or the owning account.

## Personal-to-team transfer archive

Transfer preserves project identity while changing home, under the two human
confirmations above. One sealed archive provides one retry and compatibility
boundary. Copying a raw database would import unrelated space authority and
continuations. [TransferArchiveManifest](../../src/rcp/transfer/archive.py) and
[TransferRecordBundle](../../src/rcp/transfer/records.py) own inventories,
fields, format versions and exclusions. Every project-linked table is
classified.

The archive carries canonical histories and heads, branch and merge metadata,
typed chats, saved Paper and facts, finished operational history, referenced
artifacts and matched native transcripts. Membership, credentials, caches,
resumable sessions and stages and executable bindings never travel. Imported
tasks remain history-only. Chat ids, answers and receipts stay readable. Future
work uses the target configuration. Private-thread transcripts, Space-thread data
and thread rows stay behind. A private thread's tasks keep their operation id,
kind, authorizer and Patch output, so Patch provenance resolves, but lose private
request text, answers, traces, diagnostics and artifact references. Private-thread
artifact metadata and bytes stay behind too. Question records are excluded; private
steering text stays behind with transcripts and task receipts. Public artifacts follow
[availability rules](paper-artifacts-and-result-views.md).

Source release settles paused standalone attempts as interrupted with receipts.
Queued work, running work and episode work must finish through their normal
owner. Live tasks, episodes, watchers, reports, children or deliveries cause
export refusal. Read-only export never changes status. Source release fences new
admissions transactionally. Authorized continuations may settle. Capture waits
for a quiet boundary.

Native transcript selection is best-effort by the recorded account, working
directory and repositories. Clear matches copy unchanged. Unreadable or
ambiguous files produce diagnostics, not a completeness claim. Once sealed, the
content-hashed read-only inventory is durable: missing, changed or unsafe
entries are corruption. Seed and Refresh read it alongside live sources. Staging
verifies its fingerprint. Old session ids or paths never grant execution
authority.

Target paths, accounts, binaries and profiles come from reviewed readiness
proof. Historical aliases and truth provenance remain stable. Main and branches
replay in isolation. Episode branch authorizers bind to canonical home at the
immutable base. Past-head forks validate against the recorded home-transfer
chain, as owned by [history/branches.py](../../src/rcp/history/branches.py).
Retained research must be an exact coherent archive prefix without later or
unknown content. Review, identities, heads and prefix fingerprints bind the
receipt. Conflicts never overwrite.

GitHub unpushed-commit inclusion is opt-in. It binds reviewed HEADs and rechecks
them at export. Server-only repositories require reviewed self-contained
bundles. No push, dirty files, unrelated branches, credentials or hooks travel.
Import validates bundles offline. Changed GitHub revisions use detached HEAD.
Unchanged checkouts retain attachment. Server-only history attaches to main.
Unsafe or divergent checkouts cause refusal. Only ordinary full clones are
supported. Linked worktrees, shallow clones, submodules and LFS or
filter-managed content cause refusal. Format incompatibility requires review
before release.

After the home changes, the source seals one protected request archive. Retries
rehash the same bytes. The native relay pins both backend identities and streams
bounded chunks over system SSH into a request-derived upload lease. Private
partial publication never overwrites existing bytes. Exact retries verify the
existing bytes. Browser and Tauri messages carry request and progress metadata,
never archive bytes, raw proofs or credentials.

Import validates the inventory and both proofs. It inserts mapped operational
rows transactionally, then publishes through concrete file owners. Partial
publication stays invisible. Retry uses the stored configuration and digests.
Replay and readback complete activation, registration and seating together
before the target proof becomes visible. Upload or CLI success alone grants no
project authority. Source cleanup validates that proof and sealed bytes before
retirement or removal. Corrupt or missing archives remain repairable. The
historical membership audit remains.

An active upload causes update refusal without closing the upload completion
route. Completed receipt-backed inboxes survive preparation and rollback.
Backups exclude transfer inboxes and exports. Restore invalidates leases and
marks unfinished requests for operator action. Re-entry must validate the
restored review, actor and archive before a new lease. Target restore never
reverses a committed source home change.

## Backup and restore

Protected backups are encrypted archives of research and control-plane state,
separate from whole-root update checkpoints. Capture does not pause dispatch or
Apply or claim unreachable projects are protected.
[BackupArchiveManifest](../../src/rcp/server_ops/backup_models.py) owns
inventory, recovery descriptors and exclusions.
[server.md](../server.md#back-up-the-team-server) owns configuration, capture
and restore commands.

One immutable online SQLite snapshot determines referenced durable bytes:
canonical main and branch history, typed chats, saved and published Paper,
facts, artifacts and sealed imported histories. Saved unpublished Paper comes
from that snapshot. Artifact capture holds its pruning lock. Live credentials
and provider homes, source Git, caches, temporary stages and materialized
outputs are excluded. The whole `<data_dir>/agent-threads` root (Space
transcripts and thread stages) and local memory notes under `<data_dir>/memory`
are excluded, so a restore starts with empty memory. Private project
transcripts are typed chats and are captured.
Unknown roots, unsafe or missing references or unreachable projects are visibly
uncaptured.

File reads use complete typed chat boundaries and bounded stable reads without
canonical publication or remote refresh locks. Healthy projects remain usable
during churn elsewhere. Entries bind the size and digest and stream without a
scientific-history size ceiling. Capacity failure remains explicit. Atomic
encrypted publication and full readback produce immutable receipts with safe,
bounded diagnostics.

Each project binds the current manifest, resolved routes and checkouts and
public key evidence in a nonsecret recovery descriptor. The GitHub origin and
commit must match. Server-only code is not protected. Restore initializes empty
server-only Git history, but adopted folders must still exist with the recorded
commit. Missing or inconsistent descriptors cannot silently become successful
capture. Historical schema and default compatibility is enforced by the typed
readers and fixture checks.

Backup accepts native X25519 age recipients and the supported upstream age
contract. It does not accept plugins, passphrases or SSH recipients. The
destination is explicit local or mounted storage. RCP infers no off-host
durability. Managed identity creation is root-only and atomic, with a public
sidecar. A missing, corrupt or mismatched identity causes refusal rather than
regeneration. External-recipient rotation is never implicit. **Why:** full-host
recovery requires retaining both archive and decryption identity elsewhere.
Same-host convenience alone cannot prove recoverability.

Configuration serializes under the operation lock and journals public intent. It
fences and reconciles the timer before replacement. Enablement needs a
successful first backup and readback. Shared retention keeps the configured
count plus the newest complete archive when needed. It bounds diagnostics and
staging without truncating research or scratch. Publication retries reconcile
known intents before cleanup.

Restore requires operator confirmation that old authority cannot resume. Before
mutation, it also requires archive integrity, replay and an exact roster review.
Future formats cause refusal. Preparation binds the archive and release outside
app data. The operator must revoke or prove destruction of old deploy-key
grants, SSH grants and provider logins. The operator must renew required access.
RCP never collects or revokes those credentials automatically. Permanent member
token hashes survive the snapshot. Known-stale members therefore need safe
removal or a newer archive. A sole stale member cannot be impersonated or
bypassed. Transient credentials and executable lifecycle bindings detach before
startup. Notification devices detach and observations reset to a quiet baseline.
Preferences remain.

Space identity, readable answers, receipts, chats and Paper survive. Prior tasks
become history-only. Active work becomes interrupted. Unfinished machine
requests need operator action. No old turn, watcher, retry or graph mutation may
auto-launch. Every captured checkout is reconstructed before research publication.
Conflicting history causes refusal. Uncaptured projects remain visible but
unavailable.

Root uses the deployment journal and whole-root rollback protocol under closed
admission, preserving unrelated files and original absence. Divergent remote
history causes refusal because local rollback cannot undo it. Fenced proof of
replay, history, the roster and lifecycle precedes durable selection. Pre-choice
failure restores the initialized service or leaves a fresh host stopped.
Post-choice recovery preserves accepted work. Missing provider login blocks new
tasks, not restored research reads.

## Agent browser host runtime

Browser installation is explicit and optional. Readiness checks the execution
account's Node and npm environment. Missing browser prerequisites never prevent
core startup. [browser/host.py](../../src/rcp/browser/host.py) owns the pinned
CLI, Chromium and Node compatibility contract. Agents use the RCP-owned launcher
and its checked Node, not whichever executable appears first on PATH.

The tools live in an RCP-owned protected root. Browser profiles, configuration
and ownership state live in a separate protected root. This separation also
applies to remote account storage. Both roots are excluded from backups. Cookies and process handles are never restored. Doctor and machine
cards use the same readiness service, not root's environment.
