# Spaces and project homes

**Status:** Confirmed working design; pre-blueprint and pre-implementation.

This document records the settled deployment and ownership model for personal,
self-hosted team, and future hosted RCP. It is an internal design input. Before
implementation, the canonical blueprint must be updated in place and the
user-visible promises below must become confirmed acceptance scenarios.

Authentication and enrollment are specified separately in
[Team authentication and membership](team-authentication-and-membership.md).
Permission admission is defined in
[Identity, permissions, and agent profiles](identity-permissions-and-agent-profiles.md),
while installation, backup, and restore mechanics belong to
[Team server operations](team-server-operations.md).

## Vocabulary

Use these terms consistently:

- A **space** is a durable RCP authority domain backed by one RCP control-plane
  data store. It owns a project catalog, execution configuration, operational
  state, and—when it is a team space—membership and credentials.
- A **personal space** is the space owned by the RCP backend on one person's
  computer.
- A **team space** is the space owned by one authoritative RCP backend on a
  shared server.
- A **hosted team space** is the same team-space model operated by a hosted RCP
  service. It is not a separate product architecture.
- A **backend process** is one transient process serving a space. A process may
  stop, restart, or be upgraded without changing the space it serves.
- A **connection** is a client's saved route and credential for one space. It
  is not a copy of that space.
- A project's **home space** is the one space allowed to admit writes for that
  project.
- A **project member** is a member of the home team space who may read and act
  inside that project. Project membership is separate from space enrollment.

A space is not a process, installation, window, hostname, port, filesystem
path, or source checkout. Those may change while the durable space stays the
same.

Personal versus team is an immutable control-plane fact stored beside
`space_id`. Existing installations migrate to personal; a team server is
initialized as team explicitly. RCP never guesses the kind from its machine,
path, credentials, or current users.

## Deployment ladder

The same ownership rule applies at all three deployment levels:

```text
personal space
  local backend + local frontend

self-hosted team space
  one lab-server backend + member frontends

hosted team space
  one managed backend authority + member frontends
```

In a personal space, the backend owns the local project catalog and may run
providers locally or through SSH machines configured in that personal space.

In a team space, one server-side backend is authoritative for the whole space.
Members' apps are clients of that backend. They are not peer authorities and do
not independently admit changes to team projects.

The self-hosted server may run an RCP release executable or a source install;
both serve the same space data and behavior.

A future hosted deployment replaces who operates the team backend and its data
store. It does not change project history, graph semantics, task contracts, or
the client-facing team API merely because hosting moved.

## The service account and the trust boundary

A team backend runs under a dedicated operating-system account, referred to here
as `rcp`. Its data directory and every canonical state repository homed on that
machine are readable and writable only by that account.

This is what makes "one authoritative backend" a structural statement rather
than a cooperative one. Members reach the team API through an SSH connection to
the lab server, so they have ordinary shell accounts on that machine. Without
the dedicated account they could read the control-plane database, append to
`.research/patches/` directly, or start a competing backend against the same
data directory and become the authority themselves. With it, none of those are
possible.

RCP does not define who may administer the machine. It **borrows the machine's
own privilege system**: whoever can become the `rcp` account—through `sudo`, or
by holding that account's credentials—can perform the server operations in
[Team server operations](team-server-operations.md). Granting that access is a
lab decision made with ordinary Unix tools, and the lab should know what it
implies: it also grants read access to every project's history and every
member's token hash.

Two consequences follow and must be stated plainly rather than discovered:

- **A member's token is not a machine credential.** A stolen or leaked member
  token yields that member's product authority—reading projects, creating them,
  inviting people, spending provider budget. It does not yield backup, restore,
  update, or member removal, because those require being on the machine.
- **A team-homed state repository stops being an ordinary shared checkout.**
  Members can no longer `cd` into it and run `git log` alongside RCP. All access
  goes through the app.

## Space identity and process identity

Every space receives a durable, randomly generated `space_id`. It is created
once and stored in the space's existing SQLite control plane. It survives:

- an ordinary backend restart;
- an RCP upgrade;
- an address, port, or hostname change;
- machine replacement; and
- an authorized full-state recovery.

Personal spaces mint a `space_id` on the same terms. They have no membership or
credentials, but they are a durable authority domain and they appear in
attribution, so they need a stable identity.

The existing process `instance_id` continues to identify a particular running
backend for singleton and stale-process handling. It changes across process
lifetimes and must not be used as the space identity. A path-derived data
directory id also cannot identify a space that has moved or been restored.

A saved client connection records the expected `space_id`. Reconnecting to the
same `space_id` after a normal restart is routine and does not require
re-enrollment. Finding a different `space_id` at a familiar address is an
identity mismatch: the client must block mutations until the human explicitly
reconnects to the newly identified space.

## One authoritative backend for a team space

Only the team space's backend may decide whether team work starts or whether a
team project's canonical state changes. A shared directory by itself is not a
team space, because multiple independently administered RCP processes could
apply inconsistent identity or permission decisions while writing the same
files.

The backend process may be replaced, but two active backends must not serve the
same team-space state at once. Existing one-process-per-data-directory locking
continues to handle ordinary restarts on one installation.

The first implementation deliberately has a manual exclusive-recovery
limitation: before starting a restored or migrated copy of a team space, the
operator must explicitly confirm that the old copy is shut down or disconnected
and cannot still serve the space. Restoring the same `space_id` does not itself
prevent both copies from accepting work and developing different histories.
Automatic coordination between replacement servers is later hardening, not a
promise of the first team release.

## Storage ownership

The space's SQLite control plane owns mutable state whose authority is the
space, including:

- the durable `space_id`;
- the project catalog, each project's home-space registration, and project
  membership;
- team membership, credential hashes, team invitations, project invitations,
  and browser sessions;
- provider and execution-machine configuration;
- execution profiles and server-side path resolution;
- task lifecycle, recovery, usage, budgets, and related operational state; and
- other space-level admission and administration state.

Project truth remains in the project's canonical state repository. Its
append-only `.research/patches/` history and derived `.research` materialization
continue to obey the existing replay and publication rules. The state
repository may be local to the home-space backend or reached through that
backend's existing SSH state transport.

This is an ownership split, not two competing sources of truth: SQLite decides
space-level operation and authority; canonical project history decides project
truth. Replay must not consult membership, tokens, or other mutable
authorization state.

## A project carries its own identity

Today a `project_id` is derived from the project's name, state host, and
repository path, and is minted at first registration. That is sufficient while a
project's identity is a fact about one local catalog, but it cannot survive a
project moving between spaces, and two catalogs can derive the same id for two
independently writable copies.

A project therefore carries two initial facts in its own canonical history,
written in a visible identity revision at creation or legacy adoption:

- a durable, randomly generated `project_id`; and
- `home_space_id`, the one space allowed to admit writes.

The `project_id` never changes. `home_space_id` changes only through the later
visible transfer revision defined by the transfer workflow.

Every catalog caches those values; none of them owns the values. A repository
copied to any machine announces who it is and where it belongs. The existing
derived id is retained as a legacy alias for one release so old URLs and
operational rows—tasks, usage, watchers, experiment episodes, chat session
contexts, paper drafts, and writing sessions—survive the atomic catalog
migration.

This is a nameplate, not version control. RCP's append-only patch log already
lives inside a git repository, and the project deliberately does not add a third
layer of history, branching, or merge semantics on top of them.

**Replay records home; it never refuses on it.** The low-level replay engine
must still materialize the graph in the wrong space for recovery or forensics.
That does not create an ordinary read-only project mode: the product refuses to
register the repository, and canonical admission refuses writes when
`home_space_id` is not the backend's own `space_id`. Replay never consults local
space, membership, or permission state.

## One writable project home

Every project has exactly one writable home space. Opening a team project from a
member's local app switches the API authority to the team backend; it does not
copy the project into the personal backend or make the laptop another writer.

Any team-space member may create a project in that space and becomes its first
project member. Every project member has the same project role and may invite
another existing space member to join. Space membership alone does not admit
project reads, dispatch, or changes; the authoritative backend checks current
project membership. Exact catalog visibility before joining and leave/removal
semantics still require acceptance design.

**A duplicate is refused, not resolved.** When a backend meets a repository
whose canonical history names a different home space, it declines to register it
and says which space owns it. There is no fork action: no second identity is
minted, no divergent copy is blessed, and no branching or merge concept enters
the product. Minting a fresh identity for a deliberately separated copy is a
rare console operation, not a product feature, and is unbuilt until someone
needs it.

## Project transfer is one-way

The only supported transfer is **personal space → team space**. There is no
team-to-team transfer, and a team project does not move back into a personal
space through the product.

Team-to-team transfer has no use that the product needs. Team-to-personal has a
real but rare use—a member leaving, or a lab winding down—and it carries an
authority problem the equal-member model cannot answer: any single member could
otherwise pull a shared project out of the team space unilaterally. Releasing a
project from a team space is therefore a console operation for whoever
administers the server, and it is unbuilt in the first release.

**Transfer moves authority and ownership; it does not move files.** The
canonical state repository keeps its path. What changes is:

- `home_space_id` in canonical history, appended as a patch;
- registration in the target space's catalog;
- **ownership of the canonical state repository and of every truth-scope
  repository the project's Work tasks must write**, which passes to the target
  space's service account; and
- execution configuration, which must be re-established in the target space.

The ownership handover is the part a person is most likely to be surprised by,
because a directory that was theirs stops being theirs while staying exactly
where it is. Transfer must show which directories change hands before it is
confirmed.

The implementation must also stop new source-space admission, settle active work
under a defined policy, verify the transferred history, and leave the source
entry non-writable.

## Execution belongs to the home space

Provider and machine configuration belongs to the space that owns the project. A
project refers only to providers, credentials, paths, readiness information, and
execution machines configured in its home space.

Personal-space tasks may run on the person's computer or on SSH machines
reachable from that personal backend. Every team-space task runs either:

- on the team server itself; or
- on an SSH execution machine reachable and configured from the team server.

There is no laptop execution fallback for team work. If a repository or provider
is available only on a member's laptop, the team project cannot use it until it
is made available on a server-reachable machine and configured in the team
space. The member may instead keep that work in a personal project.

### Team runs execute as the space's service account

Provider credentials are resolved from the executing process's `$HOME`, not from
the provider binary's location. RCP launches providers without overriding the
environment, and a remote run goes out through a login shell, so **the execution
account—not the configured binary path—selects which provider identity is
used.** A machine's `host` field is what chooses that account.

Team runs execute as the `rcp` account, and this is forced rather than chosen.
RCP hands the agent *paths* to canonical `.research` and to run-scope
repositories; it deliberately never copies them into scratch. So an agent
running as any other account cannot read the graph it was launched to work on,
and a Work task additionally has to write repositories the service account owns.
Widening filesystem permissions does not fix this, because it would have to be
done per task contract.

Three setup consequences follow:

- the lab authenticates each provider CLI once, on the `rcp` account;
- an execution machine used by a team project must be reachable **as** that
  service account, so a lab with separate compute creates the account in more
  than one place; and
- personal projects are unaffected and continue to run as their owner, with
  that person's provider logins.

Moving a project does not make a personal-space provider login, local path, or
machine definition valid in the target space. Target-space execution settings
must be established explicitly.

## Multi-space client boundary

One installed desktop app or source build may use its local personal space and
save connections to multiple team spaces. Their authority remains separate:

- personal project requests go to the local personal backend;
- team project requests go to that team's authoritative backend;
- selecting a space determines which API, identity, project catalog, task
  state, and execution configuration apply; and
- failure or incompatibility of one team connection must not stop personal work
  or silently reroute it elsewhere.

The local backend must not proxy-authorize, execute, or become a writable cache
for team projects.

**Selecting a space navigates to that space's own backend.** Every RCP backend
serves its own interface, so a team space is reached by pointing the application
window at the team server through the SSH connection. The screen a member is
looking at is always served by the backend answering it. That removes the
possibility of client/server skew for the application, removes any need for the
frontend to hold or attach a credential, and means space switching is a page
load rather than a tab switch.

The project index is the one screen that shows more than one space at a time, so
it stays local and displays saved connections with cached project cards,
reconciling in the background. It is also where pending project invitations
appear, because it is the only surface reachable before a person is a member of
the project in question.

Credential handling for that navigation is specified in
[Team authentication and membership](team-authentication-and-membership.md).

## Remaining acceptance and implementation work

S111, S97, S99, and S112 now implement the durable space, project nameplate,
base attribution, and human-identity foundations above. The remaining work does
not reopen those contracts:

- Add the later project-membership and invitation records.
- Define the later Patch that changes project home during transfer.
- Specify and test normal restart, address change, unexpected-`space_id`, and
  manual exclusive-recovery behavior.
- Write or confirm the remaining scenarios for full team durability, team-only
  execution, the multi-space project index, offline team connections, and
  project transfer.
- Write acceptance scenarios for project creation, invitation delivery through
  the project index, joining, membership-gated access, and equal project-member
  actions.
- Define the quiesce, verification, ownership-handover, rollback, and source
  tombstone mechanics of project transfer, including what the human is shown
  before confirming.
- Design the Settings UI that makes home-space paths and execution machines
  unambiguous.
- Apply the backup and restore mechanics in
  [Team server operations](team-server-operations.md) to SQLite without
  collapsing its authority boundary into canonical project history.
- Define server-side SSH credential ownership, host-key verification, and
  readiness reporting for execution machines.
- Decide whether a client should detect that a familiar `space_id` has been
  rolled back to an older restored archive. The manual exclusive-recovery rule
  above accepts split-brain; rollback detection is additional machinery and is
  not yet decided.
