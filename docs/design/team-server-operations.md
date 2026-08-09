# Team server operations

**Status:** Confirmed working design. This document is pre-blueprint and
pre-implementation: it records choices that are settled enough to design
acceptance scenarios and implementation, but it is not yet the canonical RCP
specification.

This document defines how a lab installs, runs, updates, backs up, and restores
the backend that owns a team space. Connection negotiation is in
[Team API compatibility](team-api-compatibility.md). Identity, enrollment, and
permission behavior are in
[Team authentication and membership](team-authentication-and-membership.md) and
[Identity, permissions, and agent profiles](identity-permissions-and-agent-profiles.md).
The authority moved or recovered by these operations is defined in
[Spaces and project homes](spaces-and-project-homes.md).

## Operating model

A team space is served by one authoritative RCP backend on a lab-controlled
Linux machine. The durable space survives process restarts and upgrades; the
process is only its current server.

**RCP requires a dedicated operating-system account**, referred to here as
`rcp`. The backend runs as that account, and its data directory and every
locally homed canonical state repository are readable and writable only by it.
This is not an administrative preference: members reach the team API over their
own SSH logins to the same machine, so without the dedicated account a member
could read the control-plane database, append to `.research/patches/` directly,
or take the singleton lock and become the authority. The rationale is in
[Spaces and project homes](spaces-and-project-homes.md#the-service-account-and-the-trust-boundary).

The server may be installed in either supported form:

- a published RCP release executable containing the backend and web interface,
  without requiring a source checkout or separate Python installation; or
- an RCP checkout whose web interface and Python environment are built and run
  from source.

Both forms serve the same team-space behavior. A source installation does not
bypass data-format checks.

RCP runs as a background service. The service starts after a machine reboot and
restarts after an unexpected process exit. Restarting the service does not
create a new space or require members to enroll again. Durable task state, not
the lifetime of one process, determines what can be recovered after restart.

## Server operations are console operations

The operations in this document—backup, restore, update, and member removal—are
performed on the server by whoever can become the `rcp` account, through `sudo`
or that account's own credentials. They are not product actions and no RCP
member role grants them.

RCP does not define who that is. It **borrows the machine's privilege system**,
so the lab's existing administration decides. Running the console commands
through `sudo -u rcp` rather than distributing the account's credentials keeps
the administrator set equal to the sudoers list, revocable, and logged.

This is what lets every RCP member remain equal without exposing dangerous
operations to a stolen credential. A member token cannot redirect backups,
install an update, restore over the space, or remove another member.

The lab should understand the corresponding fact: **granting someone machine
privilege on this server also grants them read access to every project's history
and every member's token hash.** That is a property of root, not a weakness in
RCP, and there is no point pretending otherwise.

In the app, these appear as read-only status: the last successful backup, the
latest failure, and whether a newer RCP release exists.

Removing a member has product consequences beyond the machine, and its required
behavior—reporting what it will stop, stopping it, revoking the token and
sessions, and leaving completed effects and authored history intact—is specified
in
[Team authentication and membership](team-authentication-and-membership.md#member-removal).

## Provider credentials

Provider CLI credentials live in the executing operating-system account, just as
they do when the provider CLI is run directly. They are resolved from that
process's `$HOME`, not from the location of the provider binary, so the
execution account—not a configured path—selects which provider identity a run
uses.

Team runs execute as the service account. The lab therefore authenticates each
provider CLI once, as `rcp`, during installation. An execution machine reached
over SSH for team work must be reachable **as** that service account and have
its own provider logins, so a lab with separate compute performs this step in
more than one place. The reason this is forced rather than chosen is in
[Spaces and project homes](spaces-and-project-homes.md#team-runs-execute-as-the-spaces-service-account).

These credentials are not RCP member credentials and do not become project data.
They are deliberately excluded from RCP backups, so a restored server retains its
provider and machine configuration while still requiring the operator to
authenticate the relevant provider CLIs again.

Several members' team work therefore shares one provider login per execution
machine. That is the arrangement RCP already runs under: concurrent agent tasks
against one account are implemented and verified
([S65](../acceptance/S65-concurrent-agent-tasks.md)), and a team space differs
only in volume.

## Updates

The server checks automatically whether a newer stable RCP release is available
and reports that state. It does not install an update merely because one exists.
Installing an update is always initiated by a human at the console.

For a release installation, the updater may download and install a verified
GitHub release after that human action. For a source installation, RCP reports
the available version and the required pull, build, and restart steps; it does
not overwrite the lab's checkout.

Because every member's window loads the interface served by this backend, an
update moves the whole space at once. No member is left running an older
interface against a newer server.

Before an update, RCP creates a backup using the same format and safety rules as
the scheduled backups below. What happens when that pre-update backup fails, and
rollback behavior, remain to be settled in acceptance and implementation design.

## Backup format and destination

The operator configures one destination path for team-space backups. It may be
on local storage or a filesystem mounted by the lab; RCP does not require a
particular cloud or backup vendor.

A destination on the same physical disk is allowed and still protects against
some accidental deletion or corruption. RCP warns that it does not protect
against loss of that disk. RCP never assumes that a member's personal computer is
the destination.

Each backup is a structured, versioned archive rather than an unlabelled copy of
the data directory. It has enough manifest information to identify its space,
creation time, format version, and included components before restoration. The
archive is compressed and encrypted by default.

Encryption must work unattended on a schedule without keeping the recovery
secret beside the archives. That constraint forces a public-key scheme: the
server holds only what it needs to encrypt, and restoration requires a recovery
key held elsewhere. During backup setup, RCP creates that recovery key and
requires the operator to confirm it was saved away from the server. An
unencrypted destination remains an explicit choice with a warning.

The archive contains the state needed to restore the RCP-owned team space:

- a consistent snapshot of the team server's operational database, including
  the durable space identity, member and token records, project catalog,
  RCP-owned configuration, and durable task state; and
- each reachable registered project's committed canonical RCP history and the
  committed project metadata needed to reconstruct its RCP state.

The archive does not contain:

- source repositories or other files merely referenced by a project;
- provider CLI credentials, SSH private keys, or other credentials belonging to
  an operating-system account;
- provider conversation logs or native provider-session storage;
- materialized outputs such as `graph.json` or `research.md`, which are
  regenerated by replay;
- a server executable, source checkout, Python or JavaScript dependencies, or
  operating-system service configuration; or
- temporary run scratch, transient previews, caches, build output, incomplete
  backup staging, or other regenerable material.

Consequently, restoration recovers the RCP authority domain and committed RCP
history, not the whole Linux machine or every external resource its projects
reference.

## When backups run

RCP creates backups in four circumstances:

- once per day on the configured schedule;
- whenever a human requests one;
- immediately before an RCP server update; and
- immediately before a project transfer that changes its authoritative home.

Automatic retention keeps 7 daily, 4 weekly, and 12 monthly recovery points. One
completed archive may satisfy more than one tier. Pruning happens only after a
new backup is complete. RCP removes only completed backup archives that it can
positively identify as its own and that fall outside every retained tier; it
never treats the configured destination as a directory it may clean arbitrarily.
Exact calendar bucket boundaries and behavior when the destination is full or
unavailable remain implementation details to settle and test.

Server Settings shows the last successful backup and the latest failure. A
missing destination does not stop ordinary RCP work; the failed backup is
reported and the schedule tries again at its next configured time.

## Backups do not pause work

A backup does not delay dispatch and does not delay applying a result. Both
halves of the archive are consistent by construction, so no quiescing window is
needed.

**The operational database** is in WAL mode, so an online snapshot yields a
consistent point-in-time copy while writers continue.

**Canonical history is append-only.** The archive records each project's head
revision in its manifest and copies `patches/` up to that revision. Anything
appended later is simply beyond this backup's horizon rather than a
half-captured state. The existing Sync design already makes the awkward case
safe: a batch is staged as a hidden `.batch-*` directory that replay ignores and
becomes visible through an atomic rename, so a copy running during a Sync sees
either a batch that is not yet there or one that is complete.

This is why materialized outputs are excluded rather than merely regenerable:
capturing `graph.json` beside an earlier head revision would be the one way to
produce an inconsistent archive. Restoration replays instead.

A backup therefore contains work recorded before its capture point and does not
claim to preserve an in-memory execution. When such a backup is restored, tasks
that were running are marked interrupted. Their recorded receipts and committed
results remain, but RCP does not pretend the old process is still running.

## A partial backup is honest, not failed

If a registered project's canonical history cannot be reached—a machine
rebooting, a network path down—RCP captures everything else and records that
project's status in the archive manifest, naming the reason and time. The
archive is then honestly partial rather than absent.

An all-or-nothing failure is the wrong shape for a lab whose projects live on
machines that reboot: it converts one unreachable host into no backup at all.
What must never happen is an archive that claims completeness it does not have,
which is why per-project status is recorded in the manifest and surfaced in
Server Settings.

## Restore and the authority boundary

A full restore preserves the durable space identity. That is necessary for a
replacement server to remain the same team space, but it also means that the
original server and a restored copy would both claim the same authority if
started together.

This self-hosted design has no separate machine or hosted service that can
choose which copy should accept work. The operator must ensure that the old
server is offline and cannot resume serving before bringing the restored server
online. RCP must present this responsibility plainly: `space_id` alone cannot
stop two restored copies from accepting changes and developing different
histories.

The restore acceptance design still needs to settle archive/key selection,
integrity and format checks, installation prerequisites, how external project
paths are reconnected, and the exact confirmation that the old server is offline.
Those details must not weaken the operator-owned exclusion rule above.

## Details still to settle before implementation

The following are intentionally left for acceptance scenarios and implementation
design:

- the Linux service manager commands and supported distributions;
- the release/source install and upgrade commands;
- the exact console command surface for backup, restore, update, member removal,
  and lockout recovery, and its audit trail;
- how update availability and maintenance progress appear to a member;
- the public-key encryption algorithm, key creation, storage, rotation, and
  recovery flow;
- the archive manifest and version-migration schemas, including per-project head
  revisions and capture status;
- calendar bucket boundaries and backup failure/retry reporting;
- restore validation, external-path repair, and interrupted-task presentation;
  and
- the exact ownership-handover protocol used by project transfer.

These choices may define mechanics and UI, but they may not silently add
provider credentials or source repositories to the backup, install updates
without a human, reintroduce a pause on running work merely to take a backup, or
claim that a restored server can independently prove the old authority is gone.
