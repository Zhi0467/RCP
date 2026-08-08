# Handoff — RCP spaces, team serving, identity, and permission profiles

**Date:** 2026-08-08
**State:** design direction confirmed through a human grilling session. This is
the single working document for the new system. It is intentionally
pre-blueprint and pre-implementation: the exact schemas, permission vocabulary,
UI paths, migration protocol, and acceptance scenarios still need dedicated
design sessions before code begins.

This handoff **supersedes the architecture and agent-ownership model** in
[`handoff-2026-08-07-actor-identity-and-permissions.md`](handoff-2026-08-07-actor-identity-and-permissions.md)
and invalidates the proposed
[`S92`](../acceptance/S92-actor-identity-and-permission-checks.md) scenario. It
does not supersede the canonical
[`research-control-panel-blueprint.md`](../research-control-panel-blueprint.md):
once this design is complete and its acceptance scenarios are human-confirmed,
the blueprint must be edited in place and version-bumped as one coherent design
change.

The existing
[`orchestrator handoff`](handoff-2026-08-07-orchestrator.md) still owns the
orchestrator's research behavior, action/epistemic authority line, budget,
termination, star mail, and staged command client. This handoff changes the
identity, profile, deployment, and authorization substrate underneath it.

---

## 1. Outcome in one page

RCP has a durable **space** boundary.

- A **personal space** is owned by the RCP backend on one person's machine. Its
  projects execute locally or on SSH machines configured in that local RCP
  space.
- A **team space** is owned by one RCP backend running on a shared lab server.
  All team-project execution happens on that server or on SSH machines reachable
  from it. The user's laptop is a frontend client, never an execution fallback.
- A future hosted service is another deployment of the same team-space backend,
  not a new product architecture.

The space is durable; a backend process is not. Restarting, upgrading, or
recovering the server does not create a new space. A new persistent `space_id`
identifies the authority domain across process lifetimes.

Every project has exactly one writable **home space**. Moving a personal project
into a team space is an explicit authority transfer. A copied project that may
diverge receives a new project identity.

Team users authenticate to the server. For the first self-hosted version, one
bearer token per user is sufficient when transported over an encrypted private
connection. The server derives identity from the token; a request never chooses
its own actor id. Every human member is equal in v1, but one member still cannot
impersonate another or act through another member's credential.

Runtime/task capability and semantic permission are separate gates. Starting a
task is itself an authority-bearing `dispatch` action checked **before any
execution begins**. A semantic graph change is checked again, against current
permission, immediately before Apply. Completed operational effects are never
retracted. Replay never consults users, profiles, tokens, or permissions.

The proposed user-owned agent directory is removed. Permission-bearing agent
concepts collapse to two profiles:

1. one space-wide **ordinary agent profile** used by ordinary agent tasks; and
2. one **project orchestrator profile** for each project that enables
   auto-research's elevated action-layer and dispatch authority.

Concrete executions still have task, campaign, worker/seat, session, and
provider ids, but none is a separate permission-bearing agent actor. A root task
records the human who authorized it. Spawned work records its parent task and
campaign, preserving one authorization lineage.

---

## 2. Why the earlier design was replaced

The earlier identity handoff assumed multiplayer could be built as several
locally authoritative RCP processes writing one shared canonical repository.
Each process would keep its own operational SQLite and locally declared actor
profiles.

That is not a coherent permission system. The graph append lock can serialize
bytes, but it cannot make independently administered permission registries
agree. Two clients could admit different actions under different profiles, and
neither would be the authority for the other. A shared SSH directory is a
storage server, but direct filesystem writers remain trusted clients and can
bypass RCP admission entirely.

The grilling therefore moved the authority boundary up one level: a team space
has one authoritative RCP backend. That backend owns both admission and the
project/task state from which admission is decided. Physical graph storage may
still be local to the backend or in a state repository reached by its existing
SSH transport. The load-bearing property is that **only the home-space backend
admits writes**, not that every byte lives on one disk.

The earlier handoff also modeled every agent as a durable actor owned by one
user. That became artificial once the following decisions were combined:

- all team members are equal in v1;
- the orchestrator belongs to the project, not to the human who presses Run;
- ordinary workers use one shared ordinary authority contract; and
- tasks already provide the concrete ids needed for sessions, mail, budget,
  recovery, and audit.

The replacement is authorization lineage: who authorized the root task, which
task spawned which child, and which profile each task ran under.

---

## 3. Canonical vocabulary

Use these terms consistently. Several current documents use **agent profile**
to mean provider/model/reasoning/machine configuration; that existing concept
must be called an **execution profile** when this design is integrated.

### Space

A durable RCP authority domain backed by one RCP data store.

```text
Space
  id: durable random space_id
  mode: personal | team
  users and credentials
  permission profiles
  project catalog
  provider and machine registry
  operational task state
```

A space is not a process, hostname, port, window, desktop installation, or
filesystem path. Those may change while the space remains the same.

### Backend process

One transient process serving a space. The existing process-level
`instance_id` remains useful for stale-process and takeover protection. It must
not become the durable team-space identity.

### Connection

A frontend's saved route and credential for one space. A personal frontend may
show project cards from its local personal space and may also connect to one or
more remote team spaces. Clicking into a team space switches the API authority;
it does not copy the project into the local backend.

### User

An authenticated human member of a team space, or the sole declared owner of a
personal space. Team requests derive the user from the authenticated session,
never from a client-supplied user id.

### Execution profile

The current Project Settings concept from
[`S55`](../acceptance/S55-project-owned-agent-profile.md): provider, model,
reasoning, and execution machine. It says **where and how** a provider task
runs. It grants no semantic authority.

### Agent profile

A permission-bearing agent role. It says **what semantic or orchestration
actions** an agent task may request. In the agreed v1 model there are only:

- the space-wide ordinary agent profile; and
- one project-scoped orchestrator profile per project.

In product language, it is acceptable to call the profile the **agent**. There
is no separate durable agent actor record merely to restate the same authority.

### Task contract

The active surface-specific contract for one task: Discuss, Work,
Seed/Refresh, Experiment loop, orchestrate, Paper coach, correction, and so on.
It exposes a bounded set of commands/channels and fixes the runtime execution
envelope. A task contract may narrow an agent profile; it never invents
permission the profile does not carry.

### Task, campaign, and worker/seat

Concrete execution identities with lifecycle state. They are addressable and
auditable but are not permission profiles.

- A task records one invocation lineage and durable recovery state.
- An orchestrator campaign is the bounded project-level auto-research episode.
- A worker/seat is a child task the orchestrator can address and manage.
- Provider process, native session, model, and machine remain execution
  provenance only.

### Authorization lineage

The chain that answers who caused execution:

```text
human authorizes root task
  -> root task dispatches child task
    -> child task produces operational effects and/or a Patch candidate
```

It replaces agent ownership. The root names its authorizing user. Every child
names its parent task and campaign. The lineage never grants authority by
itself; each authority-bearing command is still admitted under its active
profile, task contract, scope, and budget.

### Project home

The one space with write authority for a project. The project has a durable
`project_id` and one `home_space_id`. Source repositories are not governed by
this identity and may follow their own Git/storage topology.

---

## 4. Deployment architecture

### 4.1 Personal space

The current local product remains the personal deployment:

- the local backend owns the data directory and project catalog;
- the frontend and backend normally run on the same computer;
- provider processes may run locally or through existing SSH execution;
- provider paths, provider readiness, machine definitions, operational SQLite,
  and project configuration belong to that local space; and
- the sole local human is effectively the administrator.

Personal mode may begin as declared local identity because the operator already
controls the process and data. Permission controls are primarily confusion and
agent-overreach controls, not a defense against the local owner.

### 4.2 Team space

A team space is one RCP backend deployed on a shared lab machine.

The team backend owns:

- durable space identity;
- users, bearer-token records, and sessions;
- membership and permission profiles;
- project catalog and project-home truth;
- provider and machine registry;
- project execution profiles and server-side paths;
- background task lifecycle, usage, budgets, and messages;
- permission admission; and
- canonical project reads and writes, whether the state repository is local to
  the server or reached through the server's configured SSH transport.

The team frontend runs on each member's machine as a browser or desktop client
and talks to the shared backend. It is not an owner of tasks or canonical state.

### 4.3 Hard execution boundary

Every task inside a team space executes:

- on the lab server itself; or
- on an SSH execution machine reachable and configured from the lab server.

It never executes through the user's personal RCP backend and never falls back
to the user's laptop. A local repository that the team server cannot reach is
not available to a team project. The human must first place it on a configured
server-reachable machine or keep that work in a personal project.

This eliminates the local-worker protocol considered during the grill. There is
no **Enable this computer for execution** flow and no worker credential on a
member's laptop in the agreed architecture.

### 4.4 Provider and machine ownership

Avoid the phrase **lab-managed provider**. Provider registry, paths, readiness,
credentials, and execution-machine configuration belong to whichever RCP space
owns the project:

- a personal backend stores them for personal projects;
- a team backend stores them for team projects.

A project refers to provider/machine configuration known to its home space.
Moving the graph into a team space does not make a laptop path or provider login
valid on the lab server; execution configuration must be re-resolved there.

### 4.5 Seam to hosted RCP

The self-hosted lab machine is already a server, just one administered by the
lab rather than a third party. A hosted RCP product can run the same team-space
service with a managed database, managed secrets, and a stronger identity
provider.

The intended deployment ladder is therefore:

```text
personal space
  local backend + local frontend

self-hosted team space
  lab backend + member frontends

hosted team space
  managed backend + member frontends
```

Patch replay, graph semantics, task contracts, and frontend API concepts should
not change across that ladder.

---

## 5. Durable space and project identity

### 5.1 `space_id`

Every space needs a durable random `space_id` persisted in its authoritative
state.

It survives:

- backend process restart;
- application upgrade;
- host/port or URL change;
- machine replacement; and
- an authorized full-state restore.

The current [`ServerMetadata.instance_id`](../../src/rcp/server_runtime.py) is
per-process and intentionally changes on restart. The current `data_dir_id` is
a hash of a local path and cannot identify a moved or restored team space.
Neither can serve as `space_id`.

Clients save the expected `space_id` with a connection. Seeing an unexpected
space at a familiar address is a hard identity mismatch and blocks mutations
until the human explicitly reconnects. A normal restart changes process
identity but not space identity, so saved user credentials continue to work.

### 5.2 One writable project home

One project may have only one writable home space at a time.

Moving a personal project into a team space requires an explicit authority
transfer:

1. stop admission of new source-space work;
2. settle, pause, or cancel active tasks under a specified migration policy;
3. transfer and verify canonical history and required project metadata;
4. register the project under the target `space_id`;
5. establish target-space execution configuration; and
6. mark the old project entry as moved rather than leaving a writable clone.

An intentional divergent copy is a fork with a new `project_id`. It does not
share the old project's identity.

### 5.3 Recovery caveat still open

A backup that preserves `space_id`, users, and token hashes gives seamless
recovery. Starting both the original and restored copies would create two
writers claiming the same space identity. The design still needs a restore and
migration protocol that makes this split-brain risk explicit. Do not assume
`space_id` alone prevents it.

---

## 6. Team authentication and membership

### 6.1 Authentication floor

Team mode cannot use L0 client-declared identity. The server must derive the
user from a credential it validates.

For the first private, self-hosted version, one bearer token per human user is
an acceptable authentication mechanism. No local execution worker exists, so a
second worker credential is unnecessary.

Minimum token properties:

- unique per user, never shared by the lab;
- generated with adequate entropy and shown only through an enrollment flow;
- stored hashed by the server;
- individually revocable and rotatable;
- never accepted in URL parameters;
- never written into project configuration, prompts, task receipts, or logs;
- transported only over HTTPS, a VPN-protected channel, or an SSH tunnel; and
- exchanged for a secure, HttpOnly browser session rather than retained in
  ordinary browser JavaScript storage.

Private-network placement by itself is not encryption and does not make a
bearer credential safe to send over plaintext HTTP.

### 6.2 Equal human members in v1

All human members have equal shared-space authority in the first team version.
There is no everyday owner/admin/member rank hierarchy yet.

Equality covers shared lab operations such as projects, execution configuration,
profiles, and membership. It does not permit one member to:

- authenticate as another member;
- read, use, or rotate another member's token;
- submit a task attributed to another member; or
- rewrite the recorded human authorizer of an existing task.

A host-console recovery mechanism may repair an accidental membership or token
lockout without creating a hidden privileged user in ordinary product flows.

### 6.3 Enrollment remains to be designed

The leading bootstrap shape is:

1. first server start exposes a one-time console-local enrollment secret;
2. the first member creates time-limited, single-use invitations;
3. an invite is exchanged for that user's bearer credential; and
4. any member may invite another member under the equal-members rule.

This flow was discussed but not confirmed in detail. Token display, recovery,
revocation, browser session lifetime, CLI/headless access, TLS bootstrap, and
member removal require their own design and acceptance scenarios.

---

## 7. Permission model

### 7.1 Keep execution capability and semantic permission separate

Do not replace current surface capability with profiles. They answer different
questions.

- **Task/runtime capability** says what the launched task can physically do and
  which RCP channels or commands it receives: scratch access, repository access,
  Patch channel, orchestration client, paper read-only behavior, and so on.
- **Agent profile** says which semantic or orchestration actions an agent task
  is eligible to request.
- **Task scope** says which project, node, Experiment episode, campaign, or
  worker targets the current task may affect.
- **Human authorization** says who admitted the root execution and which budget
  or campaign it belongs to.

An action is admissible only when every relevant gate permits it:

```text
profile permits the action
AND active task contract exposes the action
AND target is inside task scope
AND current project/campaign state permits the action
AND required budget/dispatch authorization remains valid
```

No prompt, skill, manifest, provider, model, machine, native session, or task
payload may widen any gate.

### 7.2 Dispatch is an authority-bearing action

Checking permission only at Patch Apply is unsafe. An unauthorized task could
otherwise spend provider budget, read protected project context, reach SSH
credentials, disclose repository content, or let Work perform operational side
effects before its eventual Patch is rejected.

`dispatch` is therefore a first-class action checked before the server records
and starts execution. Admission must bind at least:

- authenticated human authorizer or authorized parent task;
- home space and project;
- agent profile;
- task contract/mode;
- execution profile and target machine;
- project/node/campaign scope;
- budget or invocation authorization; and
- parent/campaign lineage for spawned work.

The durable task record exists before provider-side effects begin.

Every continuation that spends a new invocation—ordinary continuation,
watcher wake, graph wake, message wake, spawned worker, Retry, Resume, or
handoff—must point to the bounded authorization that permits that invocation or
pass a fresh dispatch admission. Do not let a continuation inherit unbounded
authority merely because an earlier process once ran.

### 7.3 Apply-time semantic admission

Graph and project-truth permissions are checked against current state
immediately before Apply. A launch-time semantic check may improve diagnostics,
but it does not reserve authority.

If permission is removed while a task runs:

- a later Patch requesting the removed action is rejected at Apply;
- already completed repository writes, external API calls, compute use, and
  other operational effects are not retracted; and
- stopping or cancelling an active task is a separate operational action.

The server must make the permission check and canonical append one serialized
admission path. If canonical state lives over SSH, the same authoritative
backend still owns the permission lock and the remote publication operation;
another client never performs the append directly.

### 7.4 Replay never authorizes

Replay does not touch the permission system at all.

Once an action is admitted to canonical Patch history, later profile,
membership, token, or server-auth changes cannot make that Patch invalid.
Replay validates graph semantics and append-only history only. It never loads a
current profile and never re-decides whether the historical author was allowed
to act.

Consequences:

- remove permission decisions such as `author == "human"` from the replay path;
- perform those checks only in live admission before append;
- preserve legacy patches exactly and replay them exactly as today;
- do not record a permission snapshot merely to replay it later; and
- do not let missing identity-directory data halt graph materialization.

Audit and replay are distinct: provenance explains who or what caused an
admitted Patch, while replay trusts that admission already occurred.

### 7.5 Action vocabulary remains unfinished

The system still needs one closed, named action vocabulary. The old handoff's
starting taxonomy remains useful but is not final:

- epistemic: standing, ResearchQuestion/Hypothesis status, belief acceptance;
- action layer: Decision status/selection, Experiment status, Blocker status;
- structural: create/update/remove nodes and edges, with type-aware bounds;
- project: truth-scope membership, Proposal decisions, Settings and project
  administration; and
- orchestration: dispatch, address/message, pause/resume/stop, graph watch, and
  campaign reauthorization.

Do not encode this as arbitrary per-field ACLs. The intended granularity remains
semantic action/type groups plus named exceptional fields where authority
actually changes meaning.

---

## 8. Agent-profile model

### 8.1 Ordinary agent profile

There is one ordinary agent profile shared across the space. It captures the
semantic ceiling for ordinary agent work. It is not cloned per user.

Discuss, Work, Seed/Refresh, Experiment-loop, Paper, and correction tasks still
have different task contracts. Those contracts can expose different channels
and narrower semantic subsets while using the same ordinary profile.

Examples:

- Discuss exposes no graph-change channel.
- Paper remains read-only.
- Work exposes its current operational capability and optional semantic Patch.
- Seed/Refresh exposes its ingestion Patch contract.
- An Experiment task is narrowed to its bound episode and control node.

The ordinary profile does not erase these distinctions.

### 8.2 Project orchestrator profile

Every project that supports auto-research has one project-owned orchestrator
profile. It is not owned by the member who starts a campaign.

The profile carries the elevated authority settled in the orchestrator handoff:

- direct action-layer authority over Decision, Experiment, and Blocker state;
- orchestration commands such as dispatch and worker management;
- the fixed prohibition on changing ResearchQuestion framing or accepting
  epistemic conclusions; and
- the one project/campaign budget boundary.

The orchestrator acts through its dedicated bounded **orchestrate task
contract**. Running the same profile through an ordinary Work contract would
not expose orchestration-only commands or elevated fields. Conversely, the
ordinary profile cannot enter the orchestrate path and acquire its authority.

### 8.3 Spawned workers

When a project orchestrator starts a Blocker worker or bounded Experiment loop,
the child task uses the ordinary agent profile. It does not use an Alice-owned
or Bob-owned agent profile because those do not exist.

Its record retains:

- project and campaign;
- ordinary profile id/version;
- parent orchestrator task;
- resolved node/episode scope;
- the root human authorizer;
- task contract and execution profile;
- budget consumption; and
- provider/session/machine provenance.

The orchestrator's ability to dispatch the worker is checked at the command.
The worker's later Patch is admitted under the ordinary profile and its own
narrow scope, not under the orchestrator's elevated profile.

### 8.4 No agent owner or agent actor directory

The following fields and concepts from the old handoff are removed:

- `Actor.kind == "agent"` as a durable permission principal;
- `owner_actor_id` for every agent;
- one default agent actor per user;
- effective permission as an agent/owner-profile intersection; and
- global addressability based on an agent-owner directory.

Concrete task/worker ids remain because lifecycle and messaging require them.
They do not become permission-bearing identities.

This changes the premise of
[`open question Q9`](../open-questions.md#q9--how-does-peer-to-peer-agent-mail-work-once-rcp-is-multiplayer).
Future peer mail must reason from project/campaign scope, task lineage, recipient
budget, and human consent—not from an owning-user field that no longer exists.

---

## 9. Provenance and canonical history

The exact envelope schema is not decided, but the record must distinguish
authorization, execution, and authorship without making replay depend on them.

At minimum, a live admitted action should be traceable to:

- authenticated human user for a direct human action;
- root `authorized_by_user_id` for an agent campaign/task;
- agent profile used;
- task and parent/campaign ids;
- task contract and resolved scope;
- provider/model/machine/native-session provenance where applicable; and
- canonical source operation/idempotency identity already required by the run
  system.

`Patch.author: "human" | "agent"` must remain readable for legacy history. Do
not rewrite or backfill `.research/patches/`. Whether new Patch envelopes keep
that binary as the primary field, add the provenance above directly, or refer to
an immutable canonical task receipt is still a schema decision.

The old proposal to ship an empty signature field is no longer an agreed
requirement. A server-owned admission path changes the authentication model, and
Patch signing must be reconsidered against hosted/self-hosted server audit
requirements. Whatever is chosen, signature verification must not become replay
validity unless the human deliberately reverses the confirmed replay ruling.

Operational SQLite alone is not sufficient provenance for facts that must
survive project transfer. The next design pass must decide which lineage fields
enter append-only project history and which remain space-operational receipts.

---

## 10. Main flows

### 10.1 Open a personal project

```text
local frontend
  -> local personal-space backend
    -> local or configured SSH execution
    -> personal project canonical state
```

No team token or remote team service participates.

### 10.2 Open a team project

```text
member frontend
  -> authenticate to saved team-space connection
  -> verify durable space_id
  -> team backend reads project/task state
  -> frontend renders the team project
```

The local personal backend does not proxy, cache-authorize, or execute the team
project.

### 10.3 Start ordinary team work

```text
authenticated member request
  -> team backend derives user from session
  -> dispatch admission checks project + ordinary profile + task contract
  -> durable task record is created
  -> provider runs on server or server-reachable SSH machine
  -> optional Patch candidate returns
  -> current semantic permission is checked at Apply
  -> canonical append succeeds or Patch is rejected
```

An Apply rejection never rewrites the assistant answer or claims to retract
operational side effects.

### 10.4 Start auto-research

```text
authenticated member presses project auto-research
  -> dispatch admission creates one bounded campaign
  -> project orchestrator profile + orchestrate contract become active
  -> orchestrator task may act on the action layer
  -> orchestrator may dispatch ordinary-profile child tasks
  -> every child spends campaign budget and records parent lineage
  -> each Patch is admitted under the producing task's own profile and scope
```

The campaign records which human started it, but the orchestrator profile
belongs to the project and is unchanged when a different equal member starts a
later campaign.

### 10.5 Restart the lab server

```text
backend process stops
  -> durable space state remains
  -> replacement process starts with a new process instance_id
  -> durable space_id and user/token records remain
  -> clients reconnect and resume polling/task views
  -> server-owned recovery resumes or exposes durable task state
```

Users do not re-enroll merely because the process restarted.

---

## 11. UI work deliberately not designed yet

No implementation should improvise these surfaces. Each needs a human-confirmed
acceptance scenario with its real UI path.

### Space and connection UI

- How personal project cards and team spaces coexist in the project index.
- How a user adds, names, removes, or reconnects a team-space connection.
- How `space_id`, URL, TLS trust, version compatibility, and identity mismatch
  are shown without exposing internal ids as primary UI.
- What the desktop shell does when the team server is offline while the personal
  space remains usable.

### Team setup and membership UI

- First-server bootstrap and console recovery.
- Invitation creation and acceptance.
- Bearer-token rotation/revocation and browser session management.
- Member directory, equal-member controls, removal, and audit.
- Whether a team space needs subgroups or per-project membership at all.

### Project movement UI

- Move personal project into team space.
- Explain which canonical state moves and which execution paths/configuration do
  not.
- Quiesce active work, show verification, and prevent a writable local clone.
- Fork/export deliberately under a new project identity.

### Execution Settings UI

- Make clear that paths and provider readiness are resolved on the owning
  backend's machines.
- Prevent a member from interpreting a laptop path as a lab-server path.
- Preserve the current Settings-owned execution-profile model from S55 while
  renaming it clearly enough not to collide with permission profiles.

### Permission and provenance UI

- Ordinary versus project-orchestrator profile visibility.
- The human authorizer and parent/campaign lineage in Runs and History.
- Dispatch denial before launch versus Patch denial at Apply.
- Permission revocation while operational work is in flight.

### Orchestrator UI

The existing orchestrator handoff sketches Runs, budget, Stop, steering mail,
and graph occupancy. Its S77/S78 UI path remains unconfirmed. Team spaces add
the need to show which member started a campaign and which shared execution
configuration it uses.

---

## 12. Open design questions, in priority order

These are not implementation details; they can change schemas and security
boundaries.

1. **Space persistence and storage schema.** Where `space_id`, users, token
   hashes, profiles, and project homes live; atomic write and backup behavior.
2. **Recovery and split brain.** How a full restore or host migration proves the
   old server is no longer authoritative while preserving user credentials.
3. **Exact authentication protocol.** TLS bootstrap, PAT exchange, browser
   sessions, token recovery, CLI/headless access, and CSRF/XSS boundaries.
4. **Membership topology.** Whether every team member sees every project, or
   whether groups/project membership still exist. The old `Group` proposal is
   not carried forward automatically.
5. **Closed permission vocabulary.** Exact actions, targets, scope rules,
   configuration administration, and orchestration verbs.
6. **Profile mutability.** Whether the ordinary profile is a fixed product
   contract, editable space configuration, or versioned template; likewise what
   project-specific data the orchestrator profile may carry besides its fixed
   authority.
7. **In-flight revocation.** Dispatch and Apply are settled, but user removal,
   campaign continuation, message wakes, and cancellation policy require exact
   state transitions.
8. **Canonical provenance schema.** Which human/profile/task/campaign fields
   live on Patch history versus immutable task receipts; how project transfer
   remains self-explaining.
9. **Project transfer protocol.** Quiescing, copying, verifying, target
   configuration, rollback, and source tombstone/link behavior.
10. **Multi-space frontend architecture.** Whether the existing frontend talks
    directly to multiple base URLs, whether desktop supplies connection
    storage, and how cross-space navigation avoids stale authority.
11. **Server-side provider secrets.** Storage, OS isolation, backup, rotation,
    and what lab administrators are honestly trusted to access.
12. **Server reach to SSH execution machines.** Credential ownership, host-key
    verification, machine registration, and path/readiness visibility.
13. **Team server availability and upgrades.** Version negotiation, durable
    tasks across deployment, frontend/backend compatibility, and maintenance
    mode.
14. **Human-to-human messaging and directory.** These remain desired team
    interactions but have no confirmed surface or retention contract.
15. **Peer agent mail.** Q9 remains open and must be reformulated around task
    lineage and campaign/budget consent rather than agent ownership.
16. **Hosted identity/federation.** A person joining two unrelated spaces has
    two local user identities in v1. Cross-space identity is deliberately later.

---

## 13. Question-by-question decision record

This preserves every branch of the grilling session, including proposals later
replaced. Later rows win when they conflict with earlier rows.

|   # | Question                                                                        | Resolution                                                                                                                                                                  |
| --: | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
|   1 | Are runtime capability and semantic authority independent gates?                | **Yes.** Surface/task capability and permission profiles remain separate; neither widens the other.                                                                         |
|   2 | Should profile changes affect old Patch replay?                                 | **No.** Replay never consults the permission system at all.                                                                                                                 |
|   3 | Who may assign or change profiles?                                              | Initial answer: local humans are effectively admins before a server. **Refined later:** a team space has equal human members; a personal space has its local owner.         |
|   4 | Is active human identity app-wide or project-specific?                          | One declared identity per personal space; team identity comes from the authenticated team-space connection. Identity is never selected separately per project.              |
|   5 | Which permission state governs an action from a running task?                   | Current permission at admission/Apply time. Completed operational effects are not retracted.                                                                                |
|   6 | Can the authority registry be local while multiplayer shares graphs?            | **No.** That would diverge. Team authority must live in one shared authoritative backend.                                                                                   |
|   7 | Must all multiplayer canonical state live on one shared host?                   | **Refined.** Every project has one home-space backend. Physical state may be local to it or reached through its SSH state transport; only that backend may admit writes.    |
|   8 | Is the shared machine passive storage or an active RCP service?                 | Active RCP service. Direct writable SSH storage is not an enforceable permission boundary.                                                                                  |
|   9 | Does a member need a local execution worker?                                    | **No; superseded.** All work inside a team space executes on the team server or its reachable SSH targets.                                                                  |
|  10 | Should team execution begin as a server-recorded task?                          | **Yes in the resulting architecture.** Dispatch is admitted and recorded before execution. No execute-first/report-later path exists.                                       |
|  11 | Is `dispatch` a first-class pre-execution permission?                           | **Yes.** Apply-time graph checks alone are unsafe.                                                                                                                          |
|  12 | Does team mode require authenticated identity?                                  | **Yes.** Client-declared actor ids are insufficient.                                                                                                                        |
|  13 | Does every device/worker need a separate token?                                 | **Superseded.** There is no member-side worker. One token per user is acceptable for the self-hosted v1; per-device tokens are later hardening.                             |
|  14 | Should a user explicitly enable a laptop for execution?                         | **No longer applicable.** Team projects never execute on member laptops.                                                                                                    |
|  15 | Is team space a hard execution boundary?                                        | **Yes.** No fallback to the personal backend.                                                                                                                               |
|  16 | Whose provider credentials does a team task use?                                | Corrected vocabulary: execution configuration belongs to the owning RCP space. The team backend stores/resolves providers, machines, paths, and readiness for its projects. |
|  17 | Is one transient backend process the authority domain?                          | **No.** The durable authority domain is the RCP space; processes may restart.                                                                                               |
|  18 | Does the space need a durable identity?                                         | **Yes.** `space_id` survives process restarts, upgrades, address changes, and authorized restoration.                                                                       |
|  19 | Can one project be writable in multiple spaces?                                 | **No.** Exactly one home space; divergent export creates a new project id.                                                                                                  |
|  20 | Does team v1 need owner/admin/member ranks?                                     | **No.** Keep all human members equal for now.                                                                                                                               |
|  21 | Does equal membership permit impersonation or use of another member's identity? | **No.** Shared authority is equal; credentials and attribution remain individual.                                                                                           |
|  22 | Is agent identity attached to task type?                                        | **No.** The discussion first considered durable default-agent actors, then simplified further in question 25.                                                               |
|  23 | Is a default agent profile the ordinary ceiling narrowed by task type?          | The gate logic remains useful, but per-user default-agent identities were removed. One space-wide ordinary profile is narrowed by each task contract.                       |
|  24 | How does the orchestrator retain elevated authority under intersection?         | It has a dedicated project orchestrator profile **and** bounded orchestrate task contract that both permit the elevated action-layer/dispatch actions.                      |
|  25 | Should agent ownership be replaced by authorization lineage?                    | **Yes.** Root human authorizer plus parent task/campaign lineage; permissions attach to the ordinary or project-orchestrator profile.                                       |

---

## 14. Required documentation and acceptance work before implementation

Do not implement this handoff directly. The repository's acceptance-first rule
applies, and the current design is too large for one scenario.

The next design session should split at least these promises:

1. **A durable space survives backend replacement.** Process restart, identity
   verification, saved connection, and recovery boundary.
2. **A user joins a team space and remains individually attributable.** Token
   bootstrap, encrypted session, equal membership, reconnect, and revocation.
3. **A project has one writable home.** Move from personal to team without a
   writable clone or lost canonical history.
4. **A team task executes only inside its home space.** Server or server-reachable
   SSH execution, with no laptop fallback and truthful Settings paths.
5. **Unauthorized execution never starts.** Dispatch admission happens before
   task creation/provider side effects.
6. **Unauthorized truth never applies.** Current permission is rechecked at
   Apply, while operational effects remain visible and unretracted.
7. **Replay is permission-independent.** Legacy and new history materialize
   without user/profile/token lookups.
8. **Ordinary and orchestrator profiles remain distinct.** One shared ordinary
   profile, one project orchestrator profile, dedicated orchestrate contract,
   and ordinary-profile workers with preserved lineage.
9. **The orchestrator is usable and inspectable.** Confirm or rewrite S77/S78's
   UI path in the team-space architecture.

Once those scenarios are confirmed, update the canonical blueprint in place,
including its version and changelog. Remove or rewrite the old identity proposal
and S92 rather than attempting to preserve both models as alternatives.

---

## 15. Implementation seams to preserve during later planning

These are architecture constraints, not authorization to start coding.

- Keep process identity (`instance_id`) and durable space identity (`space_id`)
  separate.
- Keep one backend owner per space data store; extend rather than bypass the
  existing singleton and durable-task lifecycle.
- Put all live permission decisions behind one admission service used by routes,
  orchestrator commands, wakes, and Apply.
- Keep replay/materialization free of permission-service dependencies.
- Keep execution profiles (provider/model/reasoning/machine) separate from agent
  permission profiles.
- Keep personal and team task execution paths explicit; never add an invisible
  local fallback for team work.
- Bind every team request to server-authenticated identity, not body fields.
- Record tasks before effects, use current idempotency/receipt patterns, and
  retain unknown-effect reconciliation.
- Preserve append-only Patch history and never backfill identity fields.
- Land shared schema contracts serially before fanning out API, storage, runs,
  and web consumers, per `AGENTS.md`.

---

## 16. Suggested skills for the next sessions

- **`grill-me` / `grilling`** — continue resolving one consequential branch at
  a time, starting with recovery/split brain or token enrollment.
- **`codex-security:threat-model`** — after the product choices above are
  confirmed, model the self-hosted team trust boundary, bearer-token handling,
  server-side provider secrets, direct filesystem bypass, and project transfer.
- **`research-loop`** — only if a design branch needs external prior art or a
  defended comparison of self-hosted collaboration architectures.
- **`frontend-design`** — when the team-space enrollment, connection switcher,
  permission/provenance, or orchestrator UI enters acceptance-scenario design.

Do not use implementation or security-fix skills until the scenarios and
blueprint have been settled. This document records the design frontier; it is
not an implementation plan.
