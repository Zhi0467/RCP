# Projects, spaces, and operations

This specification owns durable space and project identity, team enrollment and
sessions, project membership, project homes, registration/setup, rebuildable
caches, and process/server ownership boundaries.

## Spaces and durable identity

A **space** is one durable RCP authority domain backed by one SQLite control
plane. It owns the project catalog, operational records, execution configuration,
and, for a team space, members and credentials.

Every space mints one random `space_id` and stores an immutable kind,
`personal` or `team`. Existing installations migrate to personal. The id and
kind survive process restart, address/port change, upgrades, machine replacement,
and authorized relocation of the complete data directory. RCP never infers them
from host, path, credentials, users, or process state.

`space_id` is distinct from the transient process `instance_id` and the
path-derived `data_dir_id`. `/api/health` reports all three lifecycles
separately. Copying/restoring the complete control plane also copies the authority
id; an operator must ensure an old restored copy cannot keep serving.

A personal space has one durable local owner and no team credential. A team
space authenticates every human request and gives equal product authority to its
members; RCP defines no administrator product role.

## Team initialization and enrollment

`rcp space init --team` deliberately creates a team space and requires an
interactive terminal and explicit name. It prints one single-use bootstrap code
exactly once. Serving a team space prints no secret.

On a fresh source-server installation, the systemd unit is installed but remains
stopped until an operator runs that command interactively as the `rcp` service
account through the installed wrapper. Only successful initialization permits
the first service start. Initialization therefore neither opens SQLite beside a
running lock owner nor sends the bootstrap code to a service log.

The first member exchanges that bootstrap code; later members use short-lived,
single-use invitations created by any existing member. Repeated invalid guesses
lock only that code. Invitation copy text names the nonsecret space and expiry,
shows the code once, and never embeds it in a URL.

Enrollment issues one permanent, high-entropy `rcp_`-prefixed token for that
human. RCP stores only an indexed SHA-256 hash and constant-time compares it.
Raw tokens are accepted only by the controlled exchange endpoint and are absent
from URLs, ordinary API requests, JavaScript storage, prompts, receipts,
diagnostics, configuration, and canonical history.

The exchange creates a server-side browser session in a `__Host-` prefixed,
HTTP-only, `Secure`, `SameSite=Lax` cookie with centrally configured idle
expiry. Restart does not re-enroll members. A member may rotate or revoke only
their own credential and may log out their own session.

A team space binds only loopback because credentials may not cross plaintext
HTTP. Direct HTTPS/VPN declaration and the desktop **Add team space** SSH client
are not current product paths. An unauthenticated browser receives the focused
team login boundary; a personal space instead shows the reserved team controls
disabled and unconnected.

Credential revocation does not cancel already-authorized tasks. Stopping work
and changing project membership are separate authority actions.

## Project identity and home

Every project carries a random durable `project_id` and current `home_space_id`
in canonical Patch history. Creation or legacy adoption appends one visible
system-produced identity revision. The id never derives from name, host, or path
and never changes.

Exactly one space is the writable home. A backend whose `space_id` differs from
canonical home refuses ordinary registration/writes instead of inventing a
second identity or read-only catalog mode. Low-level replay stays space-neutral
for recovery and never consults local membership or admission data.

The current product has no project transfer workflow, multi-space desktop
switcher, or fresh-identity fork. The confirmed one-way personal-to-team target
creates a separate team checkout and moves the canonical home only after the old
home is fenced. Its home-change record preserves both space-scoped human actors;
it does not change ownership of a person's checkout. Team-to-team and
team-to-personal product transfers remain excluded. The transfer's
operational-record boundary and sole archive format are settled in the
[personal-to-team transfer decision](../decisions/2026-08-27-personal-to-team-transfer-archive.md).

Every project has exactly one canonical state repository, local or remote. Its
main and Auto-research graph-branch namespaces are parts of that same canonical
repository; a graph branch does not create another project home.

## Project membership and invitations

Joining a team space does not join its projects. Project membership stores
durable `user_id`s operationally in SQLite and never enters `.research`.
Creating a project seats the creator. Legacy/memberless projects are claimed by
current members at registration or by the first later enrollee so the catalog
cannot produce an unrecoverable invisible project.

Any project member may invite any enrolled space member. A project invitation is
an authenticated in-product item carrying no credential and cannot enroll its
recipient in the space. Pending invitations appear on the project index because
the project Inbox is unreachable before membership. The recipient may accept or
decline.

Every member has the same project role. Settings shows membership, **Invite
member**, and **Leave project**. A person may leave their own project only while
another member remains; the last member must add another enrolled project
member first. Ordinary deletion remains available to a personal project, while
a team project requires the future operator-owned deprovisioning flow. Losing
membership applies the durable Stop fence to further project work while an
already-authorized turn settles honestly.

Every project-scoped route checks membership before revealing existence, and
Apply checks again under the canonical append lock. A nonmember project is
absent from project lists, the cross-project Experiment board, and direct lookup
in exactly the same way as an unknown id.

Membership is authority, not disk confidentiality. The canonical repository
still has whatever read visibility its host operating-system account permits.

## Repository and truth scope

The project manifest names repository aliases, paths paired with execution
machines/hosts, truth-scope membership, provider roots, and execution profiles.
Project truth scope is human-authored canonical membership. Run scope is a
nonempty contextual subset; it does not change project truth.

A Patch declares the repositories its run read. Task contracts name repositories
by path while scope is a list of aliases, so RCP accepts either spelling and
records each read by its manifest alias, resolving a path at or under a
registered root to that root. A declared repository outside run scope is still
rejected.

Every graph-capable run receives the complete graph and canonical research
rendering for its exact graph target plus only its selected raw repository
pointers. Work-like write containment separately permits exact admitted
repositories on the execution machine; context and write permission are not the
same thing.

Routes never write a manifest, Patch, branch, or materialized output directly.
State workspaces own locks, atomic temp-file replacement, and explicit local or
remote publication.

## Durable agent task lifecycle

Agent task status transitions are one durable contract: `running` may follow
`queued`; `pausing` may follow `queued` or `running`; and `paused`, `succeeded`,
`failed`, or `interrupted` may follow any active status (`queued`, `running`, or
`pausing`). Resume and Retry are child-attempt admissions with their own
attempt-chain requirements, not transitions of the parent row.

Every status-changing operation observes and updates the task under one write
transaction. An existing task that refuses a single-task transition keeps its
status, receipts, retained Patch output, lifecycle notices, and cleanup state,
and appends one truthful warning refusal event. The human pause request keeps
its explicit error response and writes no refusal event; bulk restart
interruption is quiet for terminal rows. A missing task id raises `KeyError`
without writing any event, receipt, notice, or cleanup. Progress updates on an
existing terminal task quietly do nothing, while a missing id still fails
loudly. Completion cleanup, pause/failure receipts, and lifecycle notices occur
only when their guarded transition applies.

Public task rows carry the backend's lifecycle answers alongside the retained
status: `active`, `queued`, `pausing`, `awaiting_human`, `paused`, `failed`,
`settled`, `finished`, and `status_label`. Browser task surfaces group, gate,
and label work from those answers; they do not compare the status vocabulary to
reconstruct the lifecycle. The Web response type seals that vocabulary so a new
string branch fails typechecking instead of silently creating another state
machine.

A persisted task request crosses one compatibility decoder when its SQLite row
becomes an `AgentTaskRecord`, before startup, watcher, mail, Retry, or recovery
policy can choose a parser. The decoder removes only fields named in an explicit
per-kind retirement allowlist; the current allowlist contains only legacy
`auto_research.ending`. Unknown or unallowlisted fields remain and strict request
validation refuses them. A stored mapping assembled outside row decoding uses
the same migration helper. Live request models remain `extra="forbid"`.

## Add project and retained research

RCP exposes one visible project wizard with three backend-authorized intents:
**Use an existing checkout personally**, **Create a shared team project**, and
**Move an existing personal project to a team**. Entry context may preselect an
intent, but the heading, review, and progress always name it plainly. Project
Settings opens the same wizard with the move intent and source project already
selected; RCP does not grow a second transfer or team-setup wizard.

Personal setup keeps the current path-based local/SSH fields. New team setup
accepts GitHub.com repository URLs and execution placement, then derives the
server-managed central checkout paths; it never asks the member to move or
upload an existing checkout. This slice accepts only
`https://github.com/<owner>/<repository>[.git]` and
`git@github.com:<owner>/<repository>[.git]`, normalizes either to one bounded
`GitHubRepositoryRef`, and derives all clone and settings URLs from that value.
A local-only codebase must first be pushed through the human's ordinary Git
workflow to a GitHub repository with a real commit. RCP neither creates that
GitHub repository nor takes the member's GitHub token. Move mode reads
repository and history identity from the selected personal project and asks
only for target placement and configuration that may change.

The one wizard is presentation, not one authority path. Personal setup uses the
existing direct setup owner, new team setup uses a durable provisioning request,
and move coordinates the authenticated personal and team backends. Direct calls
to the old registration route or ordinary setup preflight/create routes on a
team backend are rejected before request-body interpretation, filesystem
inspection, or catalog mutation. Each backend exports only its own
product eligibility, required fields, and any pinned source identity. The native
desktop bridge separately exports relay capability and its authenticated saved
targets. The desktop offers move only when the personal backend permits export,
the selected team backend permits import, and the native bridge can connect and
relay between them. A browser has no native capability answer and therefore
cannot offer cross-space move. Neither surface infers product authority from
paths or space kind.

In a personal space, Add project treats an existing `.research/manifest.toml` as retained RCP
research regardless of the name entered in the wizard. Read-only preflight
replays it without publication, repair, identity claim, or catalog mutation and
shows the canonical project, location, revision count, compatibility, and exact
failure if replay halts.

Compatible retained state may be opened without overwriting its manifest.
Incompatible state may expose its last coherent materialization read-only, but
that inspection neither claims home nor repairs history.

Starting fresh never overwrites retained work. After exact human review, RCP
fingerprints the manifest and Patch history and, under the same local or remote
append lock, atomically renames the entire `.research/` directory to a unique
timestamped sibling. If the fingerprint moved, review starts again. If the
archive cannot be proven complete, initialization does not begin. A later setup
failure leaves the archive recoverable and does not register a broken project.

An already registered legacy project without a nameplate adopts into its current
space idempotently. Separately discovered legacy state requires explicit claim.
Catalog aliases, tasks, chats, papers, watchers, attachments, and display-cache
rows migrate atomically to the canonical random id; interrupted migration
converges without duplicating the identity revision or moving a native session's
workspace.

## Rebuildable caches

Remote-source copies, derived session slices, and display caches are project
owned, bounded, and never canonical truth. Clearing the open project's cache
affects only that project and is blocked only by its active readers.

Clearing every project's rebuildable cache is a separate app-wide danger action
with an explicit warning and is blocked while any project has an active reader.
Neither action touches provider originals, canonical state, repositories,
tasks, chats, drafts, views, or paper content.

## Process and lock ownership

One RCP process owns one data directory, enforced by an OS lock. `rcp open`
reuses a healthy owner or gracefully replaces an unavailable one; explicit
`rcp serve` performs the same takeover only after recoverable work is paused.
The human is never asked to discover or kill the old process manually.

Remote canonical locks are process-held advisory files. Live contention waits.
Process or connection death releases ownership. RCP may reclaim only a provably
empty legacy lock directory; populated, symlink, or special entries remain with
an exact diagnostic and no instruction to delete them.

## Confirmed first team-server target

The first supported team deployment is deliberately narrow: one lab, one Linux
server, one team space, and source-built desktop clients. The server runs from a
checkout of GitHub `main` with a `uv` environment and clean built Web bundle
under a non-reloading system service. A dedicated Linux `rcp` account owns its
private home, data directory, runtime files, and server-local team checkouts.
An explicitly configured remote execution account owns a team-controlled
checkout on its SSH machine. Ordinary members do not share those identities and
their personal checkouts remain theirs.

The `rcp` account has fixed home `/home/rcp`, a real `/bin/bash` shell, and no
usable password. Its Ubuntu shadow entry uses an unusable non-locking value such
as `*NP*`, not a leading `!` account lock that OpenSSH may reject before public
key authentication. This supports native provider login through
`sudo -u rcp -H` and the explicitly allowed direct-key SSH route without turning
the service account into a human identity. Installation does not enable
password SSH or edit global `sshd_config`. Direct `rcp@server` access exists only
if the operator deliberately installs a public key; the preferred alternative
is a named operator account with the narrow sudo command. `rcp` has no general
sudo or supplemental privileged group membership.

Supported servers are Ubuntu 22.04 LTS and Ubuntu 24.04 LTS on x86-64 with
systemd. Server builds use Node.js 24 and an application-owned Python 3.12
managed through `uv`; Git, OpenSSH, system-wide `uv`, and the upstream `age`
CLI in the range `>=1.0.0,<2.0.0` are prerequisites. Installation validates
those system tools but does not install general OS software or modify apt
repositories. After creating the service account, it uses system-wide `uv` as
`rcp` to install and revalidate that account's managed Python 3.12 before any
source checkout or build. The operator does not provision files inside a
not-yet-existing account. The operator guide supplies tested prerequisite
commands for both Ubuntu releases.
Other Linux distributions and architectures remain unverified.

Ordinary service-owned content is grouped below `/home/rcp/rcp-server/`: the
managed source checkout, clean per-commit releases, application data,
server-local central project checkouts, the source and server-local project keys,
update checkpoints, and restore-operation journals.
Provider-native state stays in each provider's normal per-account home path
(currently `/home/rcp/.codex` and `/home/rcp/.claude`), and SSH state stays in
`/home/rcp/.ssh`; RCP does not relocate or manage provider authentication. A
later provider retains its own native path rather than joining an RCP credential
store.

Only root/system integration lives elsewhere: `/etc/rcp/server.toml`, the
root-owned current-release pointer, `/run/rcp/control.sock`, the stable CLI
wrapper, systemd units, and journald. Backup destination remains explicitly
configurable and may live outside this layout.

The installed config carries one immutable random nonsecret `installation_id`.
A private source checkout's read-only deploy key is labelled
`rcp-source:<installation-id>` and only its public fingerprint is recorded.
This machine-installation identity is distinct from the durable team `space_id`
and from every human member.

The server binds only loopback. A desktop member reaches it through an SSH
tunnel, then uses RCP membership and a browser session for product authority.
The SSH account that transports a desktop connection is not thereby an RCP
member or a server operator. Direct public HTTPS, a Linux desktop package,
containers, hosted RCP, high availability, and multi-server failover are outside
this slice.

Operator documentation shows both supported console routes. A deliberate public
key may grant direct `rcp@server` access, or root may install a
`visudo`-validated narrow rule for one named operator to run only the documented
service-account command family through `sudo -n -u rcp -H`. RCP neither infers
that named account nor silently edits sudo policy. The latter route is preferred
because its machine access can be audited and revoked independently.

These identities and credentials must never be collapsed:

- the RCP member identifies and authorizes Z, Alice, or another human;
- the Linux `rcp` account owns the service and server-local team files, while an
  explicit remote execution account owns its remote team files;
- an OpenSSH credential authenticates only its configured desktop-to-server or
  server-to-remote machine route;
- a repository-scoped Git deploy key authenticates one central repository
  checkout; and
- the provider login belongs to the operating-system account that actually runs
  that provider, locally or through SSH.

## Server and machine operations

RCP defines no administrator member role. Installation, backup, restore, source
update, machine credential provisioning, and removing another human belong to
whoever has operating-system authority on the server. Provider authentication
stays entirely provider-native under the execution account; RCP only checks its
readiness. A member token cannot perform the machine operations.

The confirmed machine surface is a narrow `rcp server ...` CLI. It includes
source installation, `doctor`, provider readiness checking, project provisioning,
backup configuration and capture, restore, member removal, and source update.
The same command implementation emits either interactive terminal guidance or
structured progress for the desktop shell. RCP does not add CLI mirrors of
ordinary graph, task, chat, or project-member actions.

Interactive output is the complete, plain-language operator workflow, not a
terse diagnostic that assumes the wizard will explain the missing step. Before
doing work, the CLI prints the numbered plan and distinguishes steps RCP will
perform from steps a human must perform. At every step it names the step,
purpose, `performed_by` responsibility, typed target, current state, and success
signal. A machine target contains host and operating-system account. An external
service target instead contains service, resource, destination URL, and required
authority role; it never invents a user identity RCP does not know. Whenever the
CLI stops for operator action, it additionally gives ordered safe commands or UI
actions, the nonsecret value needed, a plain success signal, and the exact
command to recheck or resume. System-owned steps run those internal commands
themselves; the operator never has to reconstruct a missing human command from a
status message.
Secret values never appear in those instructions. Machine-readable output
carries the same ordered step and bounded action fields so the wizard can render
them without parsing terminal prose. For team machine preparation, the wizard is
the graphical presentation of that same CLI-owned operation; the CLI remains a
complete step-by-step workflow without it, and there is no wizard-only machine
procedure.

Privilege is fixed per command rather than inferred from what happens to work on
one machine. `install`, `backup configure`, `restore`, and `update` enter through
a narrow root coordinator because they change accounts, `/etc`, systemd, or
stopped-service state; the coordinator drops to `rcp` for ordinary source and
data work. `doctor`, `provider check`, `project provision`,
`project transfer-import`, `backup run`, and `member remove` execute as `rcp`,
either through direct service-account SSH or a narrow operator sudo rule. A
wrong calling identity fails before durable work, and root's home or credentials
never become provider/Git/build state.

A completely fresh source clone has one documented bootstrap before that CLI is
available. A normal machine operator clones it under their own account, installs
the declared system prerequisites, runs `npm --prefix web ci`,
`npm --prefix web run build`, and `uv sync` in the repository-required order.
The first privileged RCP invocation is the bootstrap checkout's absolute
`.venv/bin/rcp server install --team-name "<team name>"` path under `sudo`; the
dedicated `rcp` account may not exist before that command. That required name is
the value used in the exact interactive initialization argv, not a second
installer setting.

Installation creates or validates `rcp`, then creates a separate managed Git
checkout of GitHub `main` plus one clean release directory for its exact commit
in the recorded service layout. The bootstrap checkout never becomes production
state and may be removed afterward. Root owns only account, directory, systemd,
release-pointer, and other operating-system changes. The installer performs
managed Git fetch, npm, Web build, and `uv sync --frozen` as `rcp`. From that
point on, `rcp server install` owns service installation and `rcp server update`
owns every later fetch/build/sync/switch/restart. This bootstrap is not a second
server-operations implementation.

For a fresh data directory, install leaves the unit stopped and disabled, then
prints the exact `sudo -u rcp -H /usr/local/bin/rcp space init --team --name ...`
and `sudo /usr/local/bin/rcp server install --team-name ...` resume commands.
The wrapper resolves the installed `RCP_DATA_DIR`. The operator receives the
one-time code in that terminal; the resumed root CLI then enables/starts and
reads back the service itself. Re-running install against an already initialized
owned team space may converge the service to running.
Before showing that one-time code, team initialization restricts `rcp.sqlite3`
to owner-only mode; recovery of an interrupted unclaimed initialization repeats
that restriction, and resumed installation refuses a wider database mode.

When the service is running, a server command that needs durable RCP state uses a
private machine-local control socket owned by `rcp`; it never opens SQLite beside
the lock-owning process. Installation and restore may open the data directory
only while they prove the service is stopped and acquire the normal ownership
lock. No member HTTP route exposes this machine authority.

The versioned control probe advertises its exact operation set. `server doctor`
reports provider checking as available only when the running service advertises
both provider-plan and provider-check operations; a healthy socket that omits
them is an installation problem. `server provider check` resolves only one
durable request or existing team project through that service, shows the full
plan before probing, and returns the same bounded success, failure, or operator
action in interactive and machine-readable modes.

`member remove` previews the target's active work, project memberships, tokens,
sessions, and pending invitations before confirmation. It refuses to remove the
last active member who has completed enrollment: a pending invitation or a
preprovisioned name is not yet a person who can invite the lab's next member.
It also refuses while the target is the only active member of any project and
names those projects; an existing project member must add another enrolled
member through the ordinary product flow first. Machine authority never assigns
project membership merely to make removal pass.
On confirmation one transaction marks removal in progress, revokes tokens and
sessions, revokes unconsumed space invitations they authored and pending project
invitations they authored or received, and removes active project memberships.
Invitation revocation is recorded distinctly from an invitee decline.
Identity/admission checks reject the member immediately.
The immutable user row and name remain as a tombstone for historical
attribution. Existing graceful task/episode owners then stop live authorized
work; an in-flight provider turn may settle but Apply rechecks membership. Only
after no live work remains does the operation mark the member removed. Startup
and CLI re-entry resume a crash-interrupted removal from its durable fence rather
than restoring access or forgetting to stop work. This guard avoids stranding
the space without inventing an administrator rank.

The same last-member rule applies to self-service credential revocation: RCP
refuses to revoke the sole live permanent token of the last active enrolled
member, and also refuses when that member is the only member of any project who
can still authenticate. Atomic token rotation remains available because it
returns a replacement before invalidating the old token. This slice does not let a machine operator
mint or impersonate a member credential; with the required two-member lab,
ordinary loss recovery is re-invitation by the other enrolled member.

### Source version and update

The installed version is the exact commit of the service's current source
release. `rcp server doctor` reports the managed-main, candidate, current, and
running commits plus the configured upstream. The running process captures its
physical immutable release and a bounded, deterministic SHA-256 identity of the
symlink-free Web bundle before startup and publishes both through server
metadata and health. Non-installed personal/desktop processes publish neither.

Doctor is a read-only, secret-safe interactive or structured CLI operation. It
does not fetch: its `upstream_head` is the last locally fetched `origin/main`.
It validates the configured origin/branch and clean checkout; source/release
roots and owners; current and running Git/Web identities; effective loaded
systemd fragment, absence of drop-ins, reload state and PID; space/process/data
identity through the private authenticated control socket; private runtime-file
modes; and the required installed dependencies. It refuses to traverse an
unsafe release or probe a socket selected by mismatched metadata. Healthy,
upstream-update-available, checkout-candidate-pending, and restart-pending are
distinct coherent results; inconsistent identity or any failed owned check is a
complete failed report. Before F6a's build receipt exists, `candidate_commit`
means the clean managed-checkout target that differs from the running process,
not a claim that its immutable release has already been built.

An authorized machine operator invokes `sudo rcp server update`. Its coordinator
first acquires one update-admission lock and refuses unfinished restore or
unknown update maintenance. It fetches with only the configured source identity,
shows the exact current and fetched 40-character commits, and stops with an exact
`--confirm-target <commit>` resume command. A confirmed invocation fetches again
and refuses a changed or stale target. It then fast-forwards the managed checkout
to `origin/main`, creates or validates a separate clean detached per-commit
worktree, and runs `npm --prefix web ci`, `npm --prefix web run build`, and `uv
sync --frozen` there as `rcp`. Candidate preparation never opens live app data,
changes the current release or its environment, or calls systemd.

Successful source preparation publishes one immutable private built-candidate
receipt for the later rehearsal owner. It binds the installation and configured
source; the exact base current/running commits, process instance and PID; the
candidate commit and detached release path; and the deterministic built-Web
identity. The updater revalidates those identities and bytes before publishing,
never overwrites a different receipt, and reports exact managed, candidate,
current, and running identities after a failure. This receipt proves only source
and build readiness; it is not migration, replay, rehearsal, or cutover proof.

Preflight includes a candidate rehearsal against a consistent copy of actual
server state while the old release keeps serving. Rehearsal may migrate, replay,
plan recovery, and answer representative reads, but an explicit offline fence
prevents provider turns, watcher polling, scheduled operations, Git writes, and
every other external effect. Any attempted effect fails preflight. After it
passes, the updater closes mutation and machine-operation admission, waits for
in-flight provider turns, mutations, backups, provisioning steps, and transfer
uploads to reach a durable boundary, and enters a short maintenance window.
Watchers remain durable and do not have to finish. The updater takes a final
local rollback checkpoint of all RCP-owned state the candidate startup may
change, then the narrow root portion switches `current` and restarts systemd
with normal work still closed and the same external-effect fence still active.
Provider capability warming, watcher poll/delivery, timers, recovery dispatch,
remote-stage cleanup, Git writes, and every other external effect remain
deferred while the switched candidate is eligible for rollback.

The rehearsal copy never resolves a transfer request to the live
`transfer-inbox/`, whether that request names a partial upload or a complete
verified inbox file. Every copied lease/path is rebound to a request-owned
known-absent overlay entry. Recovery may report what the missing bytes would
require, but cannot read, complete, import, or clean up the live inbox. The final
checkpoint captures an exact complete inbox entry only after admission is closed
and that upload has reached its durable boundary.

Rehearsal inventories every project. A configured SSH project already
unreachable to the current release may remain explicitly not replay-verified for
that update only if the candidate preserves its identity, returns the same
unavailable projection, and performs no effect on it. That condition is named in
the receipt and does not masquerade as successful replay. Any reachable-project
capture/replay failure, new candidate-only failure, unsafe entry, or unknown
cause blocks the update.

The updater reads back the running commit and verifies startup, ownership,
canonical replay/recovery, and representative API reads before releasing that
one fence and reopening work. Because the candidate cannot touch remote run
stages before this decision, the local checkpoint does not pretend to copy
them; it does include local run stages and temporary attachment sets through
their concrete owners. Rebuildable caches and materialized snapshots remain
excluded.
A failed pre-switch candidate never changes `current`. If post-switch
verification fails, the updater automatically stops the candidate, restores the
checkpoint and previous pointer, starts and verifies the previous release, and
only then reopens service. The failed target and restored commit remain loud in
CLI output, server status, and a durable operation receipt; this is never a
silent rollback. The checkpoint is an update-local safety boundary, not the
off-server backup format. The service account receives no general sudo or
systemd-control permission.

Rollback is a crash-safe replacement, not an overlay. Before moving the failed
candidate's app-data or server-local `.research` roots to request-specific
quarantine, the coordinator fsyncs a phase journal beside the verified
checkpoint. Re-entry through install, update, or doctor sees an unfinished
journal, leaves service stopped, and idempotently restores and verifies the
previous bytes/release before anything can serve. Candidate-created unknown
roots remain only in quarantine; a coordinator crash cannot strand a mixed old
and new data tree or make startup skip the pending restoration.

The source checkout has its own fetch identity, separate from every project. A
public RCP origin needs no secret; a private origin uses a dedicated read-only
source deploy key installed for `rcp`. Update never pushes RCP source, copies an
operator's personal SSH key, or borrows a project's write deploy key.

The configured `origin/main` commit is trusted host code. Git, npm, Web, and
Python build steps intentionally run as `rcp`, so they share that account's
access to provider-native state and server repository credentials. The rehearsal
effect fence protects live application state from accidental startup behavior;
it is not a sandbox against a malicious or compromised source commit executing
as the same Linux user. Before external sharing, protected human-reviewed
`main` is therefore required as part of this trust boundary.

`origin/main` is the single server update channel. During the private,
single-developer implementation of this first slice, work remains directly on
`main`; scoped tests, pre-commit, and code review precede recording or pushing a
change, while full desktop/live drives occur at meaningful milestones. CI
reports pushed results but the current GitHub plan does not block a bad direct
push. Development branches are never server configuration. Before public or
external sharing, the repository becomes public and real `main` branch
protection requires pull requests and the named jobs and rejects direct pushes
and failed or missing checks. The repository workflow rationale is recorded in
the
[main update-channel decision](../decisions/2026-08-27-main-is-the-server-update-channel.md).

From the first team-server-capable commit onward, current `main` directly
upgrades state from every earlier server-era persistence boundary; an operator
never walks through intermediate commits. Required CI retains one immutable,
sanitized SQLite-plus-canonical-history fixture bundle per distinct schema or
migration-semantics boundary and also exercises an upgrade from the exact
candidate base. Historical fixtures do not expire automatically. Retiring one
requires a separate explicit migration path and human decision. The compatibility
rationale is recorded in the
[server-schema decision](../decisions/2026-08-27-server-schema-compatibility.md).

A dirty managed checkout, a non-`main` checkout, divergence from `origin/main`,
an existing inconsistent release directory, a failed build, or a failed
readiness check stops with an exact diagnostic. The CLI never resets local
changes, force-pulls, silently rolls back, or switches to a packaged artifact.
The old process keeps serving its unchanged release throughout candidate
preparation. Any failed switch or restoration, and any current/running-version
mismatch, remains visible to `doctor` until repaired.

The rationale for the bootstrap, managed checkout, and privilege split is in the
[source-server install/update decision](../decisions/2026-08-27-source-server-install-and-update-privilege.md).

### Central checkouts and repository credentials

Each team project has one server-managed checkout set: exactly one
team-controlled central checkout for every declared repository on that
repository's configured execution machine. A server-local checkout is owned by
`rcp`; an SSH checkout is owned by the explicit remote execution account and is
provisioned from the server through that account. Member checkouts are
independent working copies, not alternate RCP homes, and RCP neither discovers
nor imports them implicitly. A new team project or personal-to-team transfer
prepares the complete central checkout set before registration.

The server-local central root is the fixed installed layout. For an SSH account,
the durable setup request records either one reviewed nonsecret absolute central
root or a null default-root intent. The API never guesses the remote home. The
shipped exact-account helper resolves that home and turns the default intent into
`<remote-home>/.local/share/rcp/projects`; the machine receipt then persists the
resolved central root and exact `<project-id>/repositories/<alias>` descendants
for final review. A lab may deliberately choose another absolute account-owned
root, such as mounted research storage, but the machine operator's CLI
revalidates ownership, modes, symlink-free ancestry, and the exact descendants
before cloning. The project manifest records only the resolved repository paths,
not authority to pick a different root later.

This first slice is GitHub.com-only. Before persisting a provisioning request or
performing filesystem or network work, one `GitHubRepositoryRef` parser accepts
only the two documented HTTPS and SCP-style SSH forms. Its deliberately narrow
ASCII subset accepts an owner of 1–39 alphanumeric-or-hyphen characters that
begins and ends alphanumeric, and a repository of 1–100 characters from
`A-Z`, `a-z`, `0-9`, `.`, `_`, and `-` other than `.` or `..`. It strips one
exact optional `.git` suffix and stores a lowercase `owner/repository` identity.
It rejects credentials or userinfo, query/fragment text, percent-encoded or
traversal segments, local paths,
`file://`, `ssh://`, arbitrary hosts, ports, and extra path components. Clone
URLs and GitHub deploy-key settings URLs are generated from the canonical
identity; request input is never passed through to Git. GitHub Enterprise and
other Git hosts require a later operator-configured trusted-origin design.

The default Git credential is a repository-scoped SSH deploy key whose required
capability is write. [GitHub's deploy-key form defaults to read-only and one key
cannot be reused for several repositories](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys),
so setup must create one key per GitHub repository, explicitly instruct the
operator to enable write access, and verify an actual request-scoped push before
claiming readiness. Each private key stays in the protected credential directory
of the account that owns that checkout, local or remote. Server-local keys stay
under `/home/rcp/rcp-server/credentials`; an SSH checkout's key is generated and
used only as its exact configured remote account under the verified absolute
`<remote-home>/.local/share/rcp/credentials/` root. RCP resolves that account's
home through a fixed shipped helper and verifies uid, ownership, and modes; it
does not assume `/home/<name>`, trust shell `$HOME`, allow a manifest override,
or copy private key bytes between machines. Every key is absent from SQLite,
project manifests, provider prompts, diagnostics, and backups. RCP never asks a
member to surrender a personal GitHub token to the team service.

The deploy key itself is the central checkout's GitHub identity; no GitHub user
is logged in on the RCP server. RCP generates the key pair on the local or remote
execution account, retains the private half there, and publishes only the public
key, fingerprint, deterministic label, and exact repository instructions. A
human with repository-administration authority adds that public key under the
repository's GitHub deploy keys with **Allow write access**. Re-running the same
provisioning command then uses the protected private key for `ls-remote`,
clone/fetch, and the request-scoped write proof. A private repository cannot be
read before that grant; a public repository may be read anonymously but is not
ready until the same write grant passes. Fully automatic grant installation
would require GitHub OAuth, a GitHub App, or a user token and remains outside
this slice.

Each project deploy key receives the deterministic nonsecret label
`rcp:<space-id>:<project-id>:<repository-alias>` and its public fingerprint is
retained in the provisioning receipt. This is recovery metadata, not a secret:
it lets a replacement server tell the operator exactly which old GitHub grant
to revoke while private key bytes remain outside SQLite and backup.

The write proof points a temporary request-scoped ref at an existing commit,
reads it back, and removes it. An empty repository therefore stays **operator
action needed** until the operator creates and pushes its first real commit; RCP
does not manufacture a hidden initialization commit.

Cancellation removes only request-owned local/remote private-key material after
an explicit disposition. If its public key was already added to GitHub, the
request names the label/fingerprint and stays **operator action needed** until
the operator confirms revocation or explicitly preserves the prepared request
for reuse. Losing the private half is not falsely reported as deleting the
GitHub grant.

The ordinary member-facing **Delete project** action applies only to personal
projects. A team project card publishes `can_delete=false`, and the Web omits
that action rather than deriving safety from checkout state. The API and catalog
repeat the space-kind check before any deletion work. Removing only RCP's rows
would leave the server-managed checkout and repository deploy key with no owner,
so full team-project deprovisioning requires a future operator CLI flow that
names key revocation and checkout disposition. It is not part of the first
one-lab server slice.

### Durable project provisioning

A human selects **Create a shared team project** in the unified project wizard,
whether viewed in a browser or source-built desktop. **Move an existing personal
project to a team** uses that same wizard but requires the desktop because it
coordinates the authenticated personal and team backends and owns the native
archive relay. The authoritative backend creates a durable provisioning request
before any machine work. Its backend-decided status is one of **waiting for
server setup**, **setup in progress**, **operator action needed**, **ready for
review**, **completed**, or **cancelled**. The browser renders those answers and
the exact next action; it does not infer progress from files or Git output.

The request names the target space; project name; canonical
`GitHubRepositoryRef` values and repository/machine aliases; state repository;
project and default-run truth scopes; default Auto-research invocation ceiling;
explicit central roots or default-root intent; and the human who authorized
preparation. An explicit root has a derived intended checkout path immediately;
an SSH default-root intent keeps that path null until the exact account home is
resolved. Invalid source or project-configuration text is rejected before the
request, filesystem access, or network access. For a new project it also mints
one random proposed `project_id`; an incoming transfer uses its existing project
id.
That id reserves the final central path namespace but creates no canonical
identity or writable home. `rcp server project provision <request-id>` performs
and resumes the server steps: path and permission checks, deploy-key
creation/readiness, clone or fetch, provider and execution readiness, and a
request-scoped Git write check. The request id is correlation, not machine
authority; the command still requires the server's OS privilege boundary.

The current direct team-project member API can create, list, and read these
durable requests without performing any machine step. Every authenticated team
member may inspect the preparation state. Only the member whose named identity
authorized a direct new-project request may cancel it, and member cancellation
is intentionally limited to **waiting for server setup**, when the recorded
disposition is **nothing to remove**. Once server preparation starts, the member
API refuses cancellation until the machine-owned workflow can record the exact
key/checkout cleanup or reuse disposition. Repeating an already completed
member cancellation returns the same durable result.

The command is resumable and exhaustive. If a deploy key is not yet installed,
it prints the exact GitHub repository settings destination, label, public key,
**Allow write access** requirement, and the same command to rerun. If the source
repository has no commit, it explains that the member must push their local code
through their normal GitHub workflow and names the repository plus the recheck
command; it never reaches into the member checkout. Missing SSH or provider
authentication similarly names the execution account and provider-native or
OpenSSH action, then resumes the same request after the operator performs it.

A direct `create_team_project` request is not an adoption or destructive fresh
setup path. If its cloned state repository already contains a canonical project
identity or Patch history, preparation becomes **operator action needed** before
any overwrite or archive: a personal identity directs the human to **Move to
team space**, and any other retained-history conflict requires a deliberately
cleaned or different repository outside this request. Final review rechecks that
no identity/history appeared after preparation. Incoming transfer has a
different bound rule below: matching retained history may be reused only after
the archive proves the same project, source home, aliases, and canonical heads.

The desktop offers **Run setup now** only when its native shell can prove a saved
operator SSH route can invoke that exact CLI. Otherwise it shows a copyable
command for an operator. The shell uses the system SSH configuration and agent;
it never imports a private key or asks for a `sudo` password. Direct `rcp@server`
access is allowed for the current development deployment. A named operator plus
`sudo -n -u rcp -H` is preferred because it is independently revocable and
auditable; if interaction is required, the app opens the command in Terminal
rather than collecting the secret itself. A browser without the desktop shell
can create and review a request but cannot run its machine steps.

Machine preparation alone never creates, transfers, or re-homes a canonical
project. When the request reaches **ready for review**, the UI shows the resolved
central paths, repository and provider readiness, and any work that must settle.
For a new project, only the final explicit target-space human confirmation
appends exactly the proposed project id; final creation does not mint a second
id. For personal-to-team transfer, one desktop review action records two
independent confirmations through the already-authenticated spaces: a team
member first admits the prepared incoming project, then the personal owner
releases the source project. Each backend records its own actor and binds its
receipt to the linked request; no cross-space user-id equality is assumed. A
crash after only the target confirmation leaves the source writable and the
same request resumable. The machine import command must revalidate both human
receipts and cannot supply either one itself.

For direct new-team creation, `POST
/api/project-provisioning/requests/{request-id}/complete` accepts the exact
published final-review digest from a current named team member. The original
preparation authorizer must also remain enrolled. The route rechecks the bound
configuration, all six execution profiles, every resolved checkout path, Git
and provider proofs, and retained canonical inputs without rerunning
preparation.
It renders the reviewed machine aliases, operating-system accounts, repository
paths, truth scopes, provider runtimes, and fixed permission contracts into the
manifest, then appends one system-owned `created` identity with the already
reserved id. The reviewer becomes the first project member and is retained as
the seating actor.

The recoverable product boundaries are manifest publication, exact identity
Patch, catalog registration, first-member seating, and request completion.
A retry may accept only the exact one-Patch identity prefix created for this
request; any other identity or Patch history returns to review with the
transfer/clean-repository action. Repeating confirmation after any boundary or
after completion returns the same project and request. A stale digest, changed
manifest, moved or unsafe path, incomplete provider proof, departed authorizer,
or conflicting catalog identity fails loudly without adopting or archiving
research.

Linking also binds a checksummed nonsecret source-configuration summary: source
RCP/schema and supported archive-codec versions, repository sources and
repository/machine aliases, state repository, truth-scope provenance, and the
source-manifest digest. The target chooses a common supported version before
machine preparation and rebuilds its reviewed configuration from that structure,
not from source absolute paths or provider homes. Source release rechecks the
same summary and target preparation revision before fencing; incompatible code
or a changed source manifest returns to preparation while the source is still
writable rather than creating a transfer the target cannot decode.

Serialized cross-space receipts are not treated as self-authenticating. When
the requests are linked, the source and target each generate an independent
random 256-bit one-time proof, keep their own raw value protected, and exchange
only SHA-256 commitments through the two authenticated request APIs. Final
review binds both commitments. The source reveals its raw release proof only
inside the post-fence sealed archive; the target verifies the commitment before
import. The target reveals its raw activation proof only after activation
commits. A fixed native retrieval route requires the saved permanent team-member
token, the exact target confirmer, and the completed linked request; a
cookie-only Web session cannot read it. The native relay returns the proof
directly to the pinned source backend, which verifies the commitment before
retiring its catalog row or recovery copy, then returns only a public cleanup
acknowledgment so the target can erase its raw proof. Retry before that
acknowledgment returns only the same request-bound value to the same member. A
raw proof is request-scoped transition evidence, not a
member/provider/Git/SSH credential, and never enters Web state, command
arguments, logs, or imported project history.

These proofs make a serialized receipt, request id, archive path, or successful
CLI exit insufficient within RCP's supported protocol. They are not a hostile
machine-security boundary: root or the `rcp` account can read or alter the
storage it owns, as S95 already states. Human and machine authority remain
separate product paths without claiming cryptographic defense from the machine
administrator.

### Personal-to-team transfer archive

After source work settles, transfer produces one versioned, checksummed project
archive. It contains the durable project identity, accepted main and graph-branch
canonical history and exact heads; typed canonical RCP chat transcripts; the
current Paper draft and canonical introduction; opaque `.research/facts/`
files; all finished human-visible operational history; and the exact bytes of
referenced kept artifacts and legacy kept result views. Immutable branch
metadata, Patches, and merge receipts travel; main and branch materialized
outputs do not. Finished database history includes terminal task attempts and
their events/receipts/usage, the current Paper draft, and stopped
episode/watcher/report history. Every finished Auto-research child, message,
lifecycle notice, recovery, receipt, Apply result, and inert command record
needed to render the stopped episode also travels; a pending child, delivery,
recovery, report, or watcher must settle or be terminalized before export and
never crosses as runnable state. Native-session/stage bindings, pending wake
fields, wrap-up output paths, space membership/authentication, project
invitations, provider-skill inventories, disposable result views,
reconciliation watermarks, and source machine-operation leases do not travel. A
schema-inventory test requires every later project-linked table to be
classified explicitly. Temporary human-input attachment bytes remain excluded.
It also contains one complete read-only
historical source for every provider-native conversation matched to the
project's declared repositories. The existing conversation index makes that
selection automatically and best-effort from recorded working paths and
declared repository paths, using configured provider roots and its existing
local/SSH retrieval. Selection and copying run under each saved source profile's
exact local or SSH execution account; they do not substitute another provider
home or member laptop. Clear matches travel in full; rewritten, unmatched, or
unreadable conversations are skipped with a non-blocking summary and no
completeness claim. There is no human transcript-selection step. RCP-owned
project chats travel separately as project history and are not selected again
as provider-native sources.

The export is a typed projection, not a raw copy of the personal data directory
or SQLite rows. It removes reusable stages, execution host/root bindings, live
continuations, temporary attachment bytes, scratch/cache pointers, credentials,
and machine configuration. Canonical RCP chat JSONL is parsed and rewritten:
stable chat/message ids, text, provider/model labels, graph receipts, and
display-only attachment metadata remain, while native session ids,
execution-machine/cwd fields, and unmapped source operation bindings do not.
The current Paper draft retains base/ancestor conflict content and the canonical
introduction remains a separate file. Completed Paper-coach task answers remain
terminal history, but `writing_sessions` and `chat_session_contexts` do not
transfer because they are executable native-session/prompt checkpoints rather
than durable human content.
Every imported terminal task has a durable history-only marker. Backend task
projection and control admission both force Pause, Resume, and Retry unavailable
and expose no native-session id as an executable continuation, without changing
the task's honest terminal status or answer. A later target task is a new
ordinary task under target configuration; imported failure is not
relabeled as success or as an abandoned target recovery.

Safe artifact metadata remains part of that terminal history even when its
disposable stage bytes do not. A referenced kept artifact remains openable
and downloadable through its repository owner, but cannot Keep again or revise
through the detached native session. An unkept artifact whose stage is excluded
from transfer or restore is projected explicitly unavailable. Every task-artifact
response publishes `available`, `unavailable_reason`, `can_open`, `can_download`,
`can_keep`, and `can_revise`; the unavailable case makes every `can_*` false and
has no stage URL. Content, download, Keep, and artifact-context admission recheck
those durable facts. The Web renders the backend answers, never infers
availability from `history_only`, `kept_filename`, or a remembered stage path,
and never constructs or probes a route for an unavailable action.

The source `manifest.toml` travels only as checksummed configuration provenance;
it is never published as the target's live manifest. Historical repository and
machine aliases, state-repository identity, and truth-scope provenance remain
stable because accepted Patches and `SourceRef`s refer to them. The reviewed
target request rebuilds the live manifest with the team central paths,
host/accounts, provider binaries and native-source roots, and profile execution
choices. Source absolute paths and provider homes cannot become target
configuration. Before import, main and every retained branch must replay against
that rebuilt manifest. Retained `.research` already cloned from Git is accepted
only when its identity and canonical inputs are byte-identical to the bound
archive and contain no later archive-external commit; a different identity,
missing/renamed historical alias, later head, unknown durable entry, or byte
conflict stops without overwrite.

Imported task histories are readable only and cannot Resume or Retry. Imported
provider histories receive no execution binding and are never installed under
the target account's native provider home. Seed/Refresh receives
the imported project-owned sources alongside current provider roots and the
preserved overlap-tolerant `last_refresh_at`. A local provider reads the durable
project source directly. For an SSH provider, RCP copies only the validated
project-owned imported-source inventory into the existing immutable task-input
stage and binds the prompt/read scope to that staged path; the remote native
provider roots remain in place and are never copied. Resume verifies and reuses
the same staged fingerprint, while a missing or changed stage takes the existing
visible clean-retry path. Best-effort applies to selection before the archive is
sealed; afterward a missing or hash-mismatched imported file is durable
project-source corruption and blocks Seed/Refresh rather than being silently
omitted. Source repositories are absent because target
provisioning prepares the central checkout set through Git.

This provider-history extension remains pending with the rest of the transfer
workflow. The source indexer copies each selected original native transcript
file byte-for-byte under
`<RCP_DATA_DIR>/project-sources/<project-id>/provider-history/<provider>/`, using
content-addressed filenames and read-only modes. This is durable project-owned
app data, not canonical `.research`, a source checkout, a rebuildable cache, or a
native provider home. RCP does not normalize the bytes into its lossy
conversation-record model. Provider session ids, source paths, tool output, and
other passive metadata may remain inside them, but none becomes a live execution
or resumption binding. After copying, the indexer rechecks the recorded working
path against the same declared repository before the file is admitted. Selection
is deliberately limited to best-effort path matching; historical checkout
inference and a conversation-classification UI are outside this slice.

The target validates the complete manifest, checksums, identities, canonical
replay, record references, and excluded-field rules before mutation. It stages
files, inserts the selected operational records in one SQLite transaction with
explicit id mapping, publishes canonical, kept, and imported-provider-history
files through their concrete atomic owners, and records idempotent receipts.
Activation follows successful database and file readback. A crash can leave one
non-active repairable request, never a partially imported project presented as
ready.

Once the source home change commits, the personal backend atomically seals the
one exact mode-0600 archive at
`<RCP_DATA_DIR>/transfer-exports/<request-id>.rcp-transfer` and binds its digest
receipt to those bytes. Its control envelope contains the raw source-release
proof only after the source fence has committed; that proof contributes to the
archive digest and is consumed, not imported, by the target. Every relay retry
re-hashes and streams that same file;
an already bound archive is never regenerated from a later provider-history
selection. The file remains recovery-critical until the matching target
activation receipt arrives. Only then may the source retire its catalog row and
unlink that exact request file. Ordinary project Delete is unavailable while
the source fence or sealed export is needed.

The desktop is the transfer-byte relay for this first target. Its final review
records target admission before source release through the two separate
authenticated backends. The source backend persists its human release receipt,
then fences source admission, settles authorized work, commits the home change
with both human actors, and binds the resulting exact archive digest to both
linked requests. Only after both human receipts and the source-fence receipt
exist does the native Rust shell re-verify the pinned personal backend, request
the confirmed request-bound sealed export itself, and stream bounded response chunks
into the stdin of one system-SSH child running the fixed remote command
`rcp server project transfer-import <request-id>`. Through the private control
socket, that CLI obtains the expected digest/size and an upload lease, then alone
writes a mode-0600 same-directory `.partial` under
`<RCP_DATA_DIR>/transfer-inbox/` and atomically renames it to
`<request-id>.rcp-transfer` after verification. It accepts no arbitrary archive
path, never opens SQLite, and revalidates both human confirmations, the
source-fence receipt, target readiness, ownership/mode, digest, and the
precommitted source-release proof before importing through the running server.
After activation, the native relay carries the target's precommitted activation
proof from that permanent-member-token-authenticated native route directly back
to the pinned source backend so source cleanup cannot be authorized by a forged
JSON receipt. The proof never appears in CLI progress or a cookie-authenticated
browser response. Tauri constructs no `scp`, `mv`, or remote shell pipeline.
Browser JavaScript, URLs, command arguments, shell strings, logs, and either
space's credential store never contain archive bytes, either raw transition
proof, or the other space's credential. The Web command supplies only the
validated request id and receives only progress/result metadata; bytes and raw
proofs never cross Tauri IPC. Without a proven
operator route the desktop exports one protected local file and shows bounded
Terminal commands instead of collecting SSH or sudo secrets.

Partial target inbox files are not team backup data. If a team restore contains
a nonterminal incoming request, its old upload lease and in-progress machine
state are invalidated and it becomes **operator action needed**. The target may
accept only a fresh relay of the already bound request/digest after both
backends are revalidated; it never treats the absent inbox as imported. The
sealed source archive belongs to the personal app data, not the team backup. A
committed source home change remains fenced and is never reversed merely because
the target was restored.

### Backup and restore

An unattended backup uses an `age` public recipient stored on the server; the
matching recovery identity stays off-server. The encrypted archive contains a
consistent SQLite snapshot, a manifest of captured main and graph-branch
canonical heads, and the append-only main/branch history needed to replay or
validate those heads. Immutable branch metadata, Patches, and merge receipts are
included; derived main and branch materializations are not. It separately
captures canonical RCP chat JSONL, the optional canonical Paper introduction,
safe regular `.research/facts/` files, and only the kept artifacts and legacy
kept result views referenced by the SQLite snapshot. It also contains
project-owned provider histories imported by personal-to-team transfer because
those files may be the team's only durable Seed/Refresh source. It excludes Git and provider
authentication/configuration stores, live provider homes and logs, SSH keys,
source repositories, other materialized outputs, temporary input attachments,
run/transfer staging, scratch, and caches. Backup does not pause dispatch or
Apply and never marks an unreachable project protected.

The app-data inventory is closed rather than an implicit recursive copy.
`rcp.sqlite3` enters only through SQLite's online snapshot, and transferred
`project-sources/` enters through its typed project-history owner. Raw SQLite
WAL/shared-memory files, `rcp.lock`, `rcp-server.json`,
`bootstrap-manifests/`, `project-snapshots/`, `paper-snapshots/`,
`state-cache/`, `project-caches/`, legacy `source-cache/` and
`session-slices/`, `chat-attachments/`, `run-stage/`, `transfer-inbox/`, and
`transfer-exports/` are explicit exclusions. They are respectively database
ephemera, process metadata, reconstructed locator copies, derived
snapshots/caches, temporary execution or upload state, and personal
transfer-recovery state owned by its source receipt. An unknown direct app-data
child makes capture visibly partial until its concrete owner classifies it.

For each protected team project, the manifest also binds a nonsecret recovery
descriptor from the same captured provisioning state: repository sources and
aliases, resolved central paths and machine/SSH-route references, canonical
manifest configuration, and old deploy-key labels/fingerprints. This descriptor
is enough to reconstruct the checkout set without a member checkout or personal
Git credential. A missing, stale, credential-bearing, or inconsistent descriptor
makes that project uncaptured.

The first archive contract accepts only a native X25519 `age1...` recipient and
uses the upstream `age` CLI from `1.0.0` through the 1.x line. Plugin, SSH,
passphrase, and post-quantum recipients are not accepted in this slice, so an
archive created on either supported Ubuntu release has the same required
decrypt path.

The first backup destination is one explicit writable filesystem directory.
RCP owns archive creation, atomic placement, integrity status, and retention in
that directory; it does not implement S3, SSH upload, cloud synchronization, or
another storage transport. The directory may be a local path or a mounted
filesystem. RCP neither infers nor warns whether its physical storage is on or
off the server, and it makes no durability claim based on that topology.

Backup destination, `age` public recipient, schedule, and retention are strict
versioned machine configuration in the installed server config file, not team
SQLite state. The root-owned file is readable by `rcp`, contains no private
recovery identity, and is replaced atomically only through
the following explicit operation:

```bash
sudo rcp server backup configure \
  --destination <absolute-directory> \
  --recipient <age1-public-recipient> \
  --schedule <HH:MM> \
  --retention <count> \
  --confirm
```

The schedule and retention flags default to `02:00` server-local
time and 30 newest integrity-readback archives, but the destination, native
X25519 public recipient, and explicit confirmation are always required. The
private `AGE-SECRET-KEY-...` identity is never accepted. The same resolved
schedule renders the systemd timer; there is no second editable timer value.
Retention also preserves the newest complete archive if it has fallen outside
the configured count.
One stable root-owned operation lock serializes install and configuration. RCP
fences an already loaded timer before changing either unit and proves it
inactive and disabled after daemon reload. Before the first unit mutation it
atomically records the complete intended public configuration in a pending
file; an interrupted operation must finish and read back that exact pending
configuration under the same lock before a later install or configuration may
continue. Only an exact config/timer/systemd readback clears the pending file.
Backup outcomes and archive manifests remain project-visible operational status,
but restoring SQLite never silently reconfigures this machine or its timer.

The timer is not enabled in an intermediate source commit that lacks the
concrete `backup run` owner. Configuration and unit rendering may land first,
but activation follows only after the command and unit readback exist; a failed
first run/readback leaves the timer disabled with an exact diagnostic.

Project-file capture does not take canonical append/chat/publication or remote
refresh locks and therefore does not delay dispatch or Apply. Append-only chats
are captured only through a complete typed-valid JSONL byte boundary. Mutable
Paper, facts, and referenced kept files use bounded stable reads; continued
churn, a missing referenced file, an unsafe entry, or an unclassified durable
project root makes that project visibly uncaptured. A file created or replaced
after its observed boundary is absent, never claimed as half-captured.

Every archive records exact entry and total sizes and streams through fixed-size
buffers. Required scientific history is never silently truncated or subjected
to an arbitrary archive-size ceiling; retry and diagnostic outputs are bounded
in code, and insufficient staging or destination capacity produces an explicit
partial/failure outcome. Transfer uses its exact manifest size as the upload
lease boundary rather than loading the archive into browser or process memory.

Restore is a console workflow with integrity checks, replay verification, the
installed server's displayed configured `RCP_DATA_DIR` in fresh/empty state,
and an operator confirmation that the old copy of the space cannot resume
serving. The recovery identity is read only for that run from a protected
operator-supplied file or descriptor; raw identity text never enters argv,
environment, progress, installed config, or restored data. This first contract
does not silently redirect systemd to a second data root. Before serving it
names the old source and project deploy-key labels/fingerprints,
server-to-remote SSH authorization, and provider-native login state that must be
revoked or proven destroyed, then names the replacement checkout and SSH routes
that data restoration requires. RCP
does not perform provider or SSH login/revocation and never asks for those
secrets.
An unknown newer archive or persistence boundary is rejected before target
mutation with the compatible-update requirement; an older restore binary never
best-effort interprets future data.
Before the first mutation of the fresh data root or any reconstructed checkout,
restore fsyncs a request journal under service-owned machine state outside all
target data and checkout roots. It binds the archive digest, configured target,
verified candidate, checkout/publication inventory, confirmation receipts, and
exact phase. Every preparation, publication, review, and activation step is
idempotent. `install`, `update`, and `doctor` detect an unfinished restore and
keep the service stopped; only `restore` re-entry may resume it. If protected
temporary inputs disappeared, re-entry requires the same archive and recovery
identity again rather than persisting the identity or guessing past the missing
phase. The journal completes only after service and project readback, so a crash
can leave a resumable stopped restore but never a partially restored space
serving. Restore-operation state is excluded from backup, transfer, update
rehearsal, and update checkpoints. Only the restore owner may remove one exact
completed journal/candidate after durable final readback; unfinished state is
never generic cleanup input.
It preserves `space_id`, converts captured active work to interrupted, and never
claims that RCP itself can prove the old authority is offline. Because provider
homes, run stages, and provider-native conversation state are excluded, restore
marks every pre-restore task history-only, clears `writing_sessions` and
`chat_session_contexts`, and exposes no old native-session id as an executable
continuation. Task answers, receipts, RCP chat text, and Paper content remain
readable. A later message in the same RCP chat starts a fresh provider session
only after the configured account passes readiness.

Every restored browser session and unused bootstrap/team-enrollment code is
invalidated before startup. The snapshot's active permanent member-token hashes
remain so existing members can reconnect from their credential stores. Before
serving, restore shows the archive time and exact snapshot-time member roster and
requires operator confirmation; it never claims to know about a token rotation
or revocation that occurred after capture. A known stale credential keeps the
service closed until the operator chooses a safe newer archive or, when another
active enrolled member remains, removes that member through restore's explicit
offline console step. The step reuses the ordinary member-removal transaction
and completion fence under the stopped-service ownership lock after restored
work has been detached; it is not a second policy or a general offline database
editor. If the known-stale credential belongs to the only active member, the
last-member guard correctly refuses removal and restore remains stopped for a
separate human-identity recovery design outside this slice. Machine authority
cannot mint or impersonate a replacement member credential.

Restore also terminally detaches every nonterminal episode, report attempt,
external or graph watcher, automatic recovery, and child admission before the
ordinary startup path runs. Completed history remains readable, but the
replacement must prove that startup schedules no pre-restore provider turn,
watcher check/delivery, report retry, child admission, or automatic graph
mutation. A database snapshot is not treated as safe merely because its task
rows were interrupted.

Snapshotted in-progress provisioning and machine-operation leases are also
invalid on the replacement. Completed receipts remain history, while unfinished
project setup becomes **operator action needed** with old step claims cleared;
an interrupted backup/update is reported but never auto-resumed as though its
old process still had machine authority.

On a fresh installation, restore must reconstruct every captured central
checkout from Git before publishing its archived `.research` state. It uses the
recovery descriptor and the same repository-key/checkout helpers as project
provisioning, generates fresh repository-scoped keys on the exact local or
remote checkout account, and requires any remote SSH route to be re-established
and verified. It never extracts a source checkout from the archive or creates a
bare `.research/` directory where a later clone must go. If a clone already
contains retained RCP inputs, only byte-identical archive entries with no later
canonical commit are accepted; a conflict is not overwritten. Every captured
checkout, file group, and replay must validate before the replacement serves.
Any local bootstrap manifest is regenerated from the validated recovery
descriptor and the restored catalog row is rebound to the replacement checkout;
an old locator file or absolute path is never restored as authority.
Projects explicitly recorded as uncaptured remain visible but unavailable.

Provider-native login is not required to restore or read that durable history.
If the configured execution account is not authenticated, backend readiness
keeps new task dispatch and chat continuation unavailable and names the exact
provider-native login action. The operator performs that login outside RCP and
then asks RCP to recheck; missing provider auth is not reported as failed data
restore.

The fixed service-account layout, source installation, nonsecret desktop
connection registry, durable provisioning-request boundary, disabled backup
configuration, repository-scoped deploy-key primitive, exact central-checkout
owner, exact-account provider check, final human project creation, and strict
backup-manifest/read-only inventory boundary now exist. The installed service
also creates a private immutable online SQLite snapshot and derives typed
project, provisioning, task, and kept-file inventory only from that copy. The
backup boundary classifies every direct app-data and `.research` root, rejects
materialized or credential-bearing archive entries, binds a captured project to
its unchanged completed provisioning proof, and can record a project as
uncaptured without refreshing remote state or taking a canonical writer lock.
It now consumes that immutable SQLite receipt to optimistically copy exact local
or SSH canonical main/branch history, typed chat prefixes, Paper introduction,
facts, and only SQLite-referenced kept files. Checkout identity and remote
direct-root inventories are revalidated without fetch or provider credentials;
one unavailable or continuously changing project becomes uncaptured while
healthy projects remain usable. It does not yet build or encrypt an archive,
enable the timer, publish durable backup status, apply retention, or restore
bytes.
The private installed-service control socket exposes probe, provider plan/check,
project-provision plan/step, and online SQLite-capture operations. `rcp server
project provision <request-id>` publishes one complete plan, advances one
stale-boundary-checked durable step at a time, and stops with a structured human
action when Git, checkout, transport, or provider readiness needs repair. It
exits successfully only after reading back the same request as **ready for
review**, and it has no project-creation route; only the authenticated final
review route can append the reserved identity and complete the request.

Machine orchestration, final creation, and the team deletion guard are
hermetically covered, but provisioning has not yet passed S128's complete
source-built team-service/GitHub/SSH/browser/desktop live drive. Cancellation
after machine preparation, the unified wizard and desktop operator bridge,
encryption/restore, member removal, and transfer remain active acceptance work.
Project-file capture is hermetically complete but still awaits S104's full live
Linux/SSH no-pause drive. Current RCP must not simulate the other unfinished
journeys or describe **ready for review** as an existing project.

## Deletion

Deleting a personal project removes its RCP catalog/control-plane data and
rebuildable caches after confirmation. It does not delete the underlying
research repositories or canonical state repository. Active work and tabs
reconcile through the existing ownership and Stop rules.

Ordinary deletion is unavailable for a team project. The backend publishes that
decision on its card and rechecks the team space kind before deleting anything;
the Web does not infer it from paths or checkout state. A future operator-owned
deprovision command must decide central-checkout disposition and Git deploy-key
revocation before team deletion can exist.

## Verification contracts

The durable current boundaries are [S01 first project](../acceptance/S01-first-project.md),
[S14 remote state](../acceptance/S14-remote-state.md),
[S26 delete project](../acceptance/S26-delete-project.md),
[S60 project setup](../acceptance/S60-plain-language-project-setup.md),
[S95 durable team space](../acceptance/S95-durable-team-space.md),
[S96 team enrollment](../acceptance/S96-joining-a-team-space.md),
[S98 project transfer](../acceptance/S98-move-a-project-into-a-team-space.md),
[S101 project membership](../acceptance/S101-project-membership.md),
[S102 team execution](../acceptance/S102-team-runs-execute-as-the-space-account.md),
[S103 console operations](../acceptance/S103-server-operations-are-console-operations.md),
[S104 backup](../acceptance/S104-backups-never-pause-work.md),
[S105 multi-space client](../acceptance/S105-move-between-spaces-in-one-window.md),
[S116 retained research](../acceptance/S116-choose-existing-or-fresh-research.md),
[S122 project invitations](../acceptance/S122-project-invitations.md), and
[S128 team project provisioning](../acceptance/S128-provision-a-team-project-through-desktop-and-server-cli.md).

Pending scenarios in that list describe intended future promises, not current
implemented behavior.
