# Team API compatibility

**Status:** Confirmed working design. This document is pre-blueprint and
pre-implementation: it records choices that are settled enough to design
acceptance scenarios and implementation, but it is not yet the canonical RCP
specification.

This document defines how a member's RCP client reaches a self-hosted team
server. Server lifecycle, updates, backups, and restore are in
[Team server operations](team-server-operations.md). Enrollment, credentials,
identity, and permissions are in
[Team authentication and membership](team-authentication-and-membership.md) and
[Identity, permissions, and agent profiles](identity-permissions-and-agent-profiles.md).
The durable identities checked during connection are defined in
[Spaces and project homes](spaces-and-project-homes.md).

This document is deliberately small. An earlier version specified a
current-plus-previous API version window, a compatibility test matrix, a
feature-discovery representation, and a browser interface acting as a version
fallback. Two decisions removed the skew that machinery existed to manage, and
the machinery went with it.

## Default connection

The first self-hosted team version uses an RCP-managed SSH connection by
default. A member supplies the lab server's SSH address and their SSH username;
the local RCP app opens the encrypted connection and carries team API traffic
through it. The lab does not have to expose an RCP web port to the wider network
or configure a public web certificate for this default path.

SSH protects and routes the connection. It does not identify the member to RCP.
The member's separate RCP credential authenticates each session to the team
space, and the team server derives the acting user from that credential.

Members install RCP on their own computers as a packaged desktop release or a
source build. They remain responsible for the local personal space and may also
save connections to team spaces. Opening or selecting a team space switches API
authority to the team server; the local backend does not become an execution
fallback for team work.

A directly reachable HTTPS or VPN-protected deployment can be supported later,
but it is not required for the first connection path.

## Skew cannot happen for the application

Every RCP backend serves its own interface. Selecting a team space navigates the
application window to that team server's interface through the SSH connection,
so the screen a member is looking at is always served by the backend answering
it. A member cannot be running an old interface against a new server, and a
server update moves the whole space at once.

That is why there is no compatibility window, no negotiated API version carried
on later requests, no per-pair test matrix, and no "which side needs updating"
interface. The one narrow client that is not the served page—the desktop shell,
which performs the credential exchange and the cross-space project listing—is
covered by a minimum-version field in the handshake below.

## One JSON API

Team mode extends RCP's existing JSON API. It does not introduce a second,
team-specific protocol. Existing project, graph, chat, task, and Settings
operations acquire the authentication and permission checks appropriate to the
space, while connection, space membership, team enrollment, project membership
and invitations, and server-status operations join the same API family.

## The connection handshake

The first request when opening a saved team connection reports what a client
must know before anything else. Conceptually:

```json
{
  "space_id": "durable-space-id",
  "name": "Vision Lab",
  "rcp_version": "release-version",
  "minimum_shell_version": "shell-version"
}
```

The field names and endpoint path are illustrative until the schema is designed.

`space_id` lets the client detect that a familiar address now serves a different
authority domain, which blocks mutations until the human explicitly reconnects.
`minimum_shell_version` lets the server refuse a desktop shell too old to
perform the credential exchange correctly, rather than failing obscurely.

## The CLI is not an application client

There is no application-level `rcp` CLI in this release, and the earlier promise
that "every meaningful operation available in the app must have a stable CLI
form" is withdrawn. It was a standing obligation—every future route growing a
CLI verb, forever—attached to a feature that has nothing to do with team spaces.

Three surfaces were conflated under one word, and only the first two are built:

- **The launcher.** `rcp serve` and `rcp open`. Exists today.
- **Console operations.** Backup, restore, update, member removal, and lockout
  recovery, run on the server by whoever has machine privilege. Small and closed;
  specified in [Team server operations](team-server-operations.md).
- **An application client** for driving projects, tasks, and the graph from a
  terminal or a local general-purpose agent. Not built.

Letting a local agent operate RCP remains a reasonable future feature, and it is
independent of team spaces: it could be built before, after, or never without
changing anything above. When it is wanted, the cheap form is a generic
authenticated passthrough over the OpenAPI schema the application already
publishes, plus a small set of ergonomic human commands—not a hand-written
parallel surface maintained in step with every route.

Enrollment follows the same rule. The server prints its bootstrap code to its own
terminal, and members join through the desktop app.

## Three independent versions

RCP tracks three different kinds of change:

| Version             | What it describes                                   | When it changes                                                                             |
| ------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| RCP release         | A shipped server or client build                    | When an RCP release is published                                                            |
| Stored graph schema | Durable graph and Patch representation              | When persisted graph history or materialized data requires a new schema or migration rule   |
| Team API            | The messages understood between a client and server | When an old client can no longer make requests or interpret responses safely and truthfully |

A release may change without either other version changing. A stored graph
schema change may require migration or replay work without changing what clients
see. Adding the attribution block described in
[Identity, permissions, and agent profiles](identity-permissions-and-agent-profiles.md#provenance)
is a stored-graph schema change, not a team API change.

The distinction still matters even without a compatibility window, because the
stored graph schema outlives any particular release and governs whether an
archive from last year can still be replayed.

## Details still to settle before implementation

The following are intentionally left for acceptance scenarios and implementation
design:

- the exact connection-info endpoint and request/response schema;
- SSH host-key trust, connection lifecycle, reconnect behavior, and error UI;
- how the desktop shell obtains and injects the session before navigating, and
  how it presents a failed exchange;
- how a shell below `minimum_shell_version` is told to update; and
- how stored-graph-schema migrations are applied to a restored archive.

These details may refine the message format and UI, but they may not reintroduce
a negotiated per-request API version, let a source build skip the handshake, or
require the local backend to proxy team requests.
