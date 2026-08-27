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
home is fenced; it does not change ownership of a person's checkout. Team-to-team
and team-to-personal product transfers remain excluded. The transfer's
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
another member remains; the last member must invite somebody or delete the
project. Losing membership applies the durable Stop fence to further project
work while an already-authorized turn settles honestly.

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

Add project treats an existing `.research/manifest.toml` as retained RCP
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

The server binds only loopback. A desktop member reaches it through an SSH
tunnel, then uses RCP membership and a browser session for product authority.
The SSH account that transports a desktop connection is not thereby an RCP
member or a server operator. Direct public HTTPS, a Linux desktop package,
containers, hosted RCP, high availability, and multi-server failover are outside
this slice.

Four identities must never be collapsed:

- the RCP member identifies and authorizes Z, Alice, or another human;
- the Linux `rcp` account owns the service and server-local team files, while an
  explicit remote execution account owns its remote team files;
- a repository-scoped Git deploy key authenticates one central repository
  checkout; and
- the provider login belongs to the operating-system account that actually runs
  that provider, locally or through SSH.

## Server and machine operations

RCP defines no administrator member role. Installation, backup, restore, source
update, machine/provider credential provisioning, and removing another human
belong to whoever has operating-system authority on the server. A member token
cannot perform them.

The confirmed machine surface is a narrow `rcp server ...` CLI. It includes
source installation, `doctor`, provider configuration, project provisioning,
backup configuration and capture, restore, member removal, and source update.
The same command implementation emits either interactive terminal guidance or
structured progress for the desktop shell. RCP does not add CLI mirrors of
ordinary graph, task, chat, or project-member actions.

A completely fresh source clone has one documented bootstrap before that CLI is
available. A normal machine operator clones it under their own account, installs
the declared system prerequisites, runs `npm ci`, builds the Web bundle, and
runs `uv sync` in the repository-required order. The first privileged RCP
invocation is the bootstrap checkout's absolute `.venv/bin/rcp server install`
path under `sudo`; the dedicated `rcp` account may not exist before that command.

Installation creates or validates `rcp`, then creates a separate managed Git
checkout of GitHub `main` plus one clean release directory for its exact commit
in the recorded service layout. The bootstrap checkout never becomes production
state and may be removed afterward. Root owns only account, directory, systemd,
release-pointer, and other operating-system changes. The installer performs
managed Git fetch, npm, Web build, and `uv sync --frozen` as `rcp`. From that
point on, `rcp server install` owns service installation and `rcp server update`
owns every later fetch/build/sync/switch/restart. This bootstrap is not a second
server-operations implementation.

When the service is running, a server command that needs durable RCP state uses a
private machine-local control socket owned by `rcp`; it never opens SQLite beside
the lock-owning process. Installation and restore may open the data directory
only while they prove the service is stopped and acquire the normal ownership
lock. No member HTTP route exposes this machine authority.

### Source version and update

The installed version is the exact commit of the service's current source
release. `rcp server doctor` reports the managed-main, candidate, current, and
running commits plus the configured upstream. An authorized machine operator
invokes `sudo rcp server update`. Its coordinator fetches and fast-forwards the
managed checkout to `origin/main`, creates a separate clean release directory
for that exact commit, and runs `npm ci`, the Web build, `uv sync --frozen`, and
migration/readiness preflight there as `rcp`. Candidate preparation never
changes the current release or its environment.

After preflight, the narrow root portion switches the service's `current`
release pointer and restarts systemd. It reads back the running commit before
reporting success. A failed candidate never changes `current`; a failed
post-switch start reports the exact current/running mismatch and never rolls
back silently. The service account receives no general sudo or systemd-control
permission.

The source checkout has its own fetch identity, separate from every project. A
public RCP origin needs no secret; a private origin uses a dedicated read-only
source deploy key installed for `rcp`. Update never pushes RCP source, copies an
operator's personal SSH key, or borrows a project's write deploy key.

A dirty managed checkout, a non-`main` checkout, divergence from `origin/main`,
an existing inconsistent release directory, a failed build, or a failed
readiness check stops with an exact diagnostic. The CLI never resets local
changes, force-pulls, silently rolls back, or switches to a packaged artifact.
The old process keeps serving its unchanged release throughout candidate
preparation; any current/running-version mismatch after the explicit switch
remains visible to `doctor` until the operator repairs it.

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

The default Git credential is a repository-scoped SSH deploy key whose required
capability is write. [GitHub's deploy-key form defaults to read-only and one key
cannot be reused for several repositories](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys),
so setup must create one key per GitHub repository, explicitly instruct the
operator to enable write access, and verify an actual request-scoped push before
claiming readiness. Each private key stays in the protected credential directory
of the account that owns that checkout, local or remote; it is absent from
SQLite, project manifests, provider prompts, diagnostics, and backups. RCP never
asks a member to surrender a personal GitHub token to the team service.

### Durable project provisioning

A human starts **Create team project** or **Move to team space** in the desktop
UI. The authoritative backend creates a durable provisioning request before any
machine work. Its backend-decided status is one of **waiting for server setup**,
**setup in progress**, **operator action needed**, **ready for review**,
**completed**, or **cancelled**. The browser renders those answers and the exact
next action; it does not infer progress from files or Git output.

The request names the target space, repository sources, intended central paths,
and the human who authorized preparation. `rcp server project provision
<request-id>` performs and resumes the server steps: path and permission checks,
deploy-key creation/readiness, clone or fetch, provider and execution readiness,
and a request-scoped Git write check. The request id is correlation, not machine
authority; the command still requires the server's OS privilege boundary.

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
Only the final explicit human confirmation admits project creation or the
transfer transition.

### Personal-to-team transfer archive

After source work settles, transfer produces one versioned, checksummed project
archive. It contains the durable project identity, accepted canonical history
and exact head, all finished human-visible operational history, and explicitly
kept artifact bytes. Finished history includes terminal task attempts and their
events/receipts/usage, chats and durable attachments, Paper drafts/history, and
stopped episode/watcher/report history.

The export is a typed projection, not a raw copy of the personal data directory
or SQLite rows. It removes provider-native session ids, reusable stages,
execution hosts/roots, live continuations, scratch/cache pointers, credentials,
and machine configuration. Imported records are readable historical evidence;
they cannot Resume or Retry through the source execution setup. Source
repositories are absent because target provisioning prepares the central
checkout set through Git.

The target validates the complete manifest, checksums, identities, canonical
replay, record references, and excluded-field rules before mutation. It stages
files, inserts the selected operational records in one SQLite transaction with
explicit id mapping, publishes canonical and kept files through their existing
atomic owners, and records idempotent receipts. Activation follows successful
database and file readback. A crash can leave one non-active repairable request,
never a partially imported project presented as ready.

### Backup and restore

An unattended backup uses an `age` public recipient stored on the server; the
matching recovery identity stays off-server. The encrypted archive contains a
consistent SQLite snapshot, a manifest of captured canonical heads, and the
append-only canonical history needed to replay those heads. It excludes Git and
provider credentials, SSH keys, source repositories, materialized outputs,
scratch, and caches. Backup does not pause dispatch or Apply and never marks an
unreachable project protected.

Restore is a console workflow with integrity checks, replay verification, an
explicit target data directory, and an operator confirmation that the old copy
of the space cannot resume serving. It preserves `space_id`, converts captured
active work to interrupted, and never claims that RCP itself can prove the old
authority is offline.

These service-account, CLI, backup/restore, desktop connection, provisioning,
and transfer workflows remain unimplemented active acceptance work. Current RCP
must not simulate them or describe partial setup or capture as authoritative.

## Deletion

Deleting a project removes its RCP catalog/control-plane data and rebuildable
caches after confirmation. It does not delete the underlying research
repositories or canonical state repository. Active work and tabs reconcile
through the existing ownership and Stop rules.

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
