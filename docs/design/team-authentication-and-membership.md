# Team authentication and membership

**Status:** Confirmed working design, and now implemented. Both halves are
canonical in the blueprint — authentication and enrollment under
[Team space enrollment and sessions](../research-control-panel-blueprint.md#team-space-enrollment-and-sessions),
and project membership in the same section. This document remains the fuller
specification behind them.

**What is built (2026-08-15):** `rcp space init --team`, the single-use
bootstrap claim, member-created invitations, permanent `rcp_`-prefixed tokens,
the `/api/team/*` exchange, logout, rotate and revoke routes, and the browser
login boundary that gates a team space
([S96](../acceptance/S96-joining-a-team-space.md)). Also project membership
itself ([S101](../acceptance/S101-project-membership.md)) — seated on creation,
enforced on every project-scoped route and again at Apply — and how it changes
([S122](../acceptance/S122-project-invitations.md)): project invitations
carrying no credential, accept and decline on the project index, leaving from
Project Settings, the last-member refusal, and the Stop-style fence on losing
membership.

**What is not built:** the desktop **Add team space** form, SSH transport,
operating-system credential storage, and removing *another* person, which is a
console operation
([S103](../acceptance/S103-server-operations-are-console-operations.md)).
A *personal* space still shows the reserved **Join team space**,
**Accept invitation**, and **Invite member** seam as visibly disabled: a personal
space cannot reach a team space from inside this build. That seam is not a
fallback authentication path.

The durable deployment and authority boundary is specified in
[Spaces and project homes](spaces-and-project-homes.md).
Authorization after authentication is specified in
[Identity, permissions, and agent profiles](identity-permissions-and-agent-profiles.md),
and connection negotiation is specified in
[Team API compatibility](team-api-compatibility.md).

## Security boundary

A team backend must authenticate each human request. A request never supplies an
actor id and asks the server to trust it; the server derives the human user from
a credential or browser session that the server validates.

An SSH connection is the default encrypted route for the first self-hosted
version. Members already need SSH access to the lab server, and RCP can carry
its API traffic through that connection without exposing a new HTTP port to the
wider network. SSH protects the connection, but it does not establish the RCP
identity: the personal RCP token still tells the team backend which member made
the request.

Direct HTTPS or a VPN-protected connection may be supported later without
changing the identity model. Private-network placement alone is not encryption,
and a bearer credential must never travel over plaintext HTTP.

That rule is enforced, not merely stated: a team space refuses to serve on
anything but a loopback host. Adding direct HTTPS therefore means adding a
deliberate way to declare that the connection is already encrypted, because
today a routable bind address and an unprotected credential are the same thing
as far as the server can tell.

The operating-system boundary that this authentication rests on—the dedicated
`rcp` service account and its exclusively owned data directory—is specified in
[Spaces and project homes](spaces-and-project-homes.md#the-service-account-and-the-trust-boundary).
Without it, a member with a shell could bypass every check below.

## One credential per human

The first team version uses one permanent, high-entropy bearer token for each
human member. Tokens are individual credentials; there is no shared lab token.
The server stores only a secure hash and the metadata needed to identify,
revoke, and rotate the credential.

Each token must be:

- generated with cryptographically adequate entropy;
- stored in secure credential storage on the member's computer after enrollment;
- independently revocable and rotatable without changing another member's
  credential;
- absent from URLs and query parameters;
- absent from ordinary application and SSH logs;
- absent from prompts, task receipts, project configuration, and canonical
  project history; and
- sent only through an encrypted connection to the team backend.

The token is a human credential, not an agent credential. Team execution never
runs on a member laptop, so there is no worker or device token in v1. Tasks and
provider sessions have execution identities and authorization lineage, but they
do not receive a second bearer credential representing the human.

A token grants **product** authority only. It never grants backup, restore,
update, or member removal; those require machine privilege on the server. The
blast radius of a leaked token is therefore bounded: the holder can read
projects, create them, invite people, and spend provider budget, but cannot
exfiltrate the space or lock the lab out.

Per-device credentials, federated login, and organization identity providers are
possible later hardening. They are not required to make the first private team
release coherent.

### When secure storage is unavailable

**Confirmed 2026-08-12.** Enrollment still succeeds. RCP writes the token to a
permission-restricted file and says so **loudly and persistently** — at
enrollment, and as a standing visible state afterwards, not a dismissible notice
seen once.

This is a deliberate trade and the cost is named rather than softened: anything
running as that user can read the token. On a shared machine that is the
credential model gone, and the bound on a leaked token — product authority only,
never backup, restore, update, or removal — is the only thing still limiting the
damage.

The reason for taking it: a source build on a lab Linux box with no keyring is a
normal way to run RCP, and refusing to enroll there would push people toward
storing the secret somewhere worse than a mode-0600 file. The failure must never
be silent, because a warning nobody sees converts a stated trade into an
accident.

The scenario must assert the warning is present, persistent, and accurate about
what is exposed, and that the file is unreadable by other accounts.

## Equal members without impersonation

All human members have equal team-space authority in v1. There is no ordinary
owner/admin/member rank hierarchy and no PI role. Any member may perform the
shared space-level operations the product makes available, including creating a
project and creating an invitation for another person to join the team space.

Equal authority does not merge identity. A member may not:

- authenticate as another member;
- read or use another member's token;
- rotate another member's token as though they possessed it;
- submit a task attributed to another member; or
- rewrite the recorded human authorizer of existing work.

The backend attributes a root action to the member authenticated for that
request. Child work preserves the task and campaign lineage from that root;
clients and agents never choose a different human attribution.

There is no RCP administrator role, and equality is preserved by keeping the
dangerous operations out of the product entirely rather than by ranking members.
Backup, restore, update, and member removal are console operations performed by
whoever administers the machine; see
[Team server operations](team-server-operations.md). In the app those appear as
read-only status.

## Project membership

Space enrollment and project membership are different layers:

- any space member may create a project and becomes its first project member;
- every member of one project has the same project role;
- any project member may invite another existing space member to that project;
- the project invitation appears on the invited member's **project index**; and
- accepting it adds project membership without issuing a new bearer token or
  changing space membership.

The invitation appears on the project index rather than in the Inbox because
Inbox is a destination inside the project shell, reachable only once a person is
already a member of that project. The index is the only surface that exists
before membership, and it is already the one screen presenting more than one
space at a time.

A project invitation is not the secret code used to enroll a new team-space
member. It is an authenticated in-product request addressed to an existing
member identity. Project actions derive both the authenticated space member and
current project membership on the server; a client cannot claim either in its
request body.

### Leaving, and the last member

**Confirmed 2026-08-12.** Leaving a project is always possible. A person is never
held in a project because nobody else is in it.

When the last member leaves, the project **archives by default**. It keeps its
canonical history and its identity; it stops being an active project in the
space. Whoever administers the machine may delete it, as a console operation
alongside the others in
[Team server operations](team-server-operations.md).

This keeps equality intact. The alternative — refusing the last member's
departure — would have made "the only remaining member" into a role with an
obligation attached, which is a project owner by another name.

Two things are deliberately not decided here and must be settled in the
acceptance scenario rather than improvised: whether an archived project can be
re-admitted and by whom, and whether space members can see that an archived
project exists at all. Neither may reintroduce a ranked project role.

Exact pre-membership project discoverability and invite decline also remain to be
designed, under the same constraint.

## Server bootstrap and first member

`rcp space init --team` creates a new team space and prints its one-time
bootstrap code to the interactive terminal of the person who ran that command.
Starting or serving the backend never prints the code. A team backend normally
runs under systemd without an interactive terminal, so printing a secret on
first server start would put it in the journal—the same ordinary server log in
which credentials are forbidden above.

If initialization is interrupted before the code is delivered, rerunning the
same command and name is allowed only while the team still has no member. It
invalidates any unseen bootstrap code and prints one replacement, so a terminal
failure cannot leave the new space permanently unclaimable.

The first member uses the app to:

1. install or build RCP on their own computer;
2. choose **Add team space**;
3. supply the team server's SSH address and their SSH username;
4. enter the one-time bootstrap code through an interactive secret input; and
5. receive their permanent personal token after the server consumes the code.

The RCP client stores the permanent token in secure credential storage on that
computer and saves the connection's expected `space_id`. The bootstrap code is
not a reusable team password and stops working after its successful use.

Starting a replacement backend process for the same durable space is a normal
restart. It preserves users and token hashes, so members do not repeat bootstrap
or enrollment merely because RCP was upgraded or restarted.

## Inviting later members

Any existing member may choose **Invite member**. The server creates a
short-lived, single-use invitation code. It is an enrollment credential, not the
inviter's permanent token and not a credential that members share after joining.

The inviter gives the new member the connection information and invitation code.
This may be copied as a small block containing the non-secret team name and SSH
coordinates alongside the secret code, but v1 does not put the secret in a
clickable URL. That avoids accidental retention in browser history, referrer
data, and routine URL logs.

The invited person installs the desktop app or runs a source build and opens
**Add team space**. The form asks for:

- the server's SSH address;
- the member's SSH username; and
- the invitation code.

Their RCP client opens the SSH connection and sends the invitation directly to
the team backend. The person chooses their member name, the server atomically
consumes the invitation and creates the member record, and the client receives
and securely stores that member's permanent personal token. The invitation
cannot be used again. Later connections use the permanent token; the member does
not re-enter the invitation.

SSH access and RCP membership remain distinct. Possessing an invitation does not
supply an operating-system SSH account, and having an SSH login does not by
itself make someone an RCP member.

## Member removal

Removing a member is a console operation on the server, alongside the other
operations in [Team server operations](team-server-operations.md). Leaving a
space voluntarily remains available in the app, because a person may always give
up their own access.

Removal must report what it is about to end before it acts—running tasks and
active campaigns, named by project—and then:

- stop that member's running tasks and campaigns;
- drop their project memberships;
- revoke their token; and
- invalidate their browser sessions.

Two things must not happen. **Completed operational effects are not undone**:
repository writes, external calls, and compute already spent stand exactly as
they did. And **authored history is untouched**: the member's name remains on
every patch they authorized, because canonical history is append-only and
immutable. Removing someone from a space does not remove them from the record of
what they decided.

Stopping their work is deliberate rather than incidental. Because permission is
rechecked at Apply, a campaign left running after its authorizer was removed
would continue spending provider budget for hours and then have every resulting
Patch rejected.

## Local app behavior

Each member runs an ordinary RCP desktop app or source build on their own
computer. That installation owns a personal space for local projects while
acting as a client of one or more team servers.

Personal work stays with the local personal backend. Selecting a team space
connects to and authenticates with that team's backend. The local backend does
not execute team work, apply team changes, or silently fall back to local
execution when the server is unavailable.

A release build and a source-development build use the same team enrollment,
credential, and API rules. Building from source does not bypass authentication.

There is no application-level `rcp` CLI in this release. The CLI remains the
server launcher plus the console operations above; see
[Team API compatibility](team-api-compatibility.md#the-cli-is-not-an-application-client).

## Where the token lives

The permanent token is never held in browser JavaScript storage, and the
interface never attaches it to a request.

Because selecting a team space navigates the application window to that team
server's own interface, the credential only has to be presented once, by the
part of the application that is not a web page:

1. the desktop shell holds the token in operating-system credential storage;
2. it opens the SSH connection to the team server;
3. it posts the token to the team backend's exchange endpoint and receives a
   secure, `HttpOnly` session cookie;
4. it places that cookie in the window's cookie jar for that origin; and
5. it navigates the window to the team server's interface.

From then on the page is same-origin with the backend answering it, and it
authenticates with an ordinary session that scripts cannot read. The token never
enters a URL, a page, or a log.

A member reaching the same server-supplied interface in an ordinary browser
authenticates through the same exchange endpoint at a controlled login boundary.
Both clients use one mechanism.

Token revocation must invalidate the corresponding ability to create or continue
sessions. Exact cookie lifetime, renewal, logout, and CSRF protection are
implementation details that require explicit security tests before release.

## Remaining acceptance and implementation work

These are required details for turning the confirmed design into code. They are
not entries for `open-questions.md` and do not reopen the identity model above.

- Define the user, token-hash, invitation, bootstrap-code, and session SQLite
  schemas and their atomic state transitions.
- Choose the token hashing/KDF, token format, entropy, rotation procedure, and
  revocation/session-invalidation mechanics.
- Choose secure credential storage for notarized desktop builds and supported
  source-development environments, with an explicit failure mode when secure
  storage is unavailable.
- Specify bootstrap and invite expiry, failed-attempt limits, replay behavior,
  and safe terminal/UI display rules.
- Define project membership and invitation records, project-index delivery,
  join, decline, leave, and last-member behavior without introducing ranked
  project roles.
- Specify the console operations and their audit trail without turning them into
  an ordinary privileged product account.
- Implement SSH host-key verification, connection lifecycle, reconnect behavior,
  and clear separation between SSH login failures and RCP authentication
  failures.
- Define session cookie scope, lifetime, renewal, logout, CSRF defenses, XSS
  assumptions, and the token-to-session exchange, including how the desktop
  shell injects the session before navigating.
- Write acceptance scenarios for first-member bootstrap, invitation enrollment,
  one-time and expired codes, restart without re-enrollment, token rotation and
  revocation, equal-member actions, impersonation rejection, and member removal
  with running work.
- Verify that tokens and invitations are redacted from URLs, logs, prompts, task
  receipts, configuration exports, diagnostics, and canonical history.
