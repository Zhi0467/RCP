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

The current product has no project transfer workflow, team-to-team transfer,
multi-space desktop switcher, or fresh-identity fork. Those future journeys stay
active as pending acceptance work and are not implied by copying a repository.

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

A task request RCP persisted is read back tolerantly. Request models forbid
unknown fields so a live caller cannot smuggle one past validation, but a stored
request is RCP's own record of work it already admitted, so a field this build no
longer declares must not make that task permanently unrecoverable. Reading one
drops exactly the keys the model no longer declares, logs each drop with the
operation id, and validates every remaining field as strictly as before. Recovery
therefore survives a request-schema field being removed; a live request carrying
an unknown field is still refused.

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

## Server and machine operations

The current product does not define an RCP administrator member role. Dangerous
machine-wide operations—installation, backup, restore, update, credential
provisioning, and removing another human—belong to whoever has operating-system
authority on the server. Product credentials do not grant that privilege.

Current source and desktop launch paths serve the same JSON API and Web
application. Provider credentials belong to the execution operating-system
account and do not become RCP member credentials or project data.

The fully specified self-hosted service-account, automated backup/restore,
update, multi-space client, and project-transfer workflows remain unimplemented
active acceptance work. Current RCP must not simulate them, expose ordinary app
controls that claim machine authority, or describe an incomplete backup as
authoritative recovery.

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
and [S122 project invitations](../acceptance/S122-project-invitations.md).

Pending scenarios in that list describe intended future promises, not current
implemented behavior.
