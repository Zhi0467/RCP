# Provider logins are kept alive

Confirmed by the human 2026-09-14, revised the same day after a design review
and two protocol spikes. This record reverses two documented rules:
`docs/server.md` said RCP does not log in to providers and that no token is ever
pasted into RCP; `docs/specs/server-and-machine-operations.md` said RCP does not
manage provider authentication. It also carves a narrow exception into
`docs/specs/authority-and-proposals.md`: a team member may sign a shared
provider account in or out from the product, attributed and visible to all.

## What happened

A ChatGPT or Claude login is an access token plus a single-use refresh token.
Every refresh spends the refresh token and returns a new pair that must be
written back. If two processes refresh with the same token, or one refreshes and
its write is lost, the file holds a spent token and the next process fails with
`refresh_token_reused`. The login is dead until a human signs in again.

RCP started a fresh Codex process for every turn, every recovery attempt, and
every startup skill probe, and a fresh Claude process for every turn, all reading
the same credential file. The startup gate added on 2026-09-12 serialized the
first seconds of each launch. On the team server the shared login still died on
2026-09-12 with no RCP turn running, and every episode on the machine failed
silently for a day.

The desktop Codex app never loses its login because it is one process. That was
the first design: one RCP-owned `codex app-server` per machine account with every
turn a thread. Two facts retired it for now. Work turns get their exact write
roots and `.research` protection from a named permission profile passed in
process configuration; a spike on codex 0.154.0 showed a per-thread profile in
thread configuration is ignored (`permissions: null`). And RCP proves an
operational command's authority by the invocation process's ancestry, which one
shared process would erase for every turn in it.

## The decision

Every credential gets exactly one refresh path, and RCP starts no process it
does not need.

- **Codex** stays one process per turn under the existing gate. The startup
  skill inventory and the model catalog are refreshed only when the configured
  binary or its version changed, through the gate, never concurrently with a
  turn start. No provider process is terminated before the gate's minimum hold
  has elapsed, so a refresh begun at start can finish its write.
- **Claude** runs on a one-year `setup-token` that RCP stores in the execution
  account's credential store and injects as `CLAUDE_CODE_OAUTH_TOKEN` into every
  Claude process it starts, on every machine, through one environment builder.
  A static token rotates nothing.
- **Sign-in happens in the RCP UI.** Codex through `codex login --device-auth`
  run as the execution account with the code and URL shown; Claude by pasting
  the token. Any team member may do it; RCP has owners and members and no admin.
  A sign-in counts only after one minimal real request succeeds.
- **The shared owner is a separate design track**, gated on the protocol
  carrying per-thread permissions, a per-turn command authority that does not
  rest on process ancestry, remote attachment, and interrupt semantics.

## What this costs, stated plainly

A mid-turn refresh race between two concurrent Codex turns remains possible in
principle; it is rare, it is detected and classified the moment it happens, it
never spends a paid turn on retries, and signing in again is one click. RCP now
manages provider authentication for Claude and drives Codex sign-in, which the
operator docs promised it would not. The Claude token grants model requests only
and expires yearly; RCP shows the paste date and warns ahead as an estimate. A
member can sign a shared account out under another member's running work; the
action is attributed and the blocked work resumes after the next sign-in.
