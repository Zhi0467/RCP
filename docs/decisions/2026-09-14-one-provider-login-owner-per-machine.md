# One provider login owner per machine account

Confirmed by the human 2026-09-14. This record reverses two documented rules:
`docs/server.md` said RCP does not log in to providers and that no token is ever
pasted into RCP; `docs/specs/server-and-machine-operations.md` said RCP does not
relocate or manage provider authentication.

## What happened

A ChatGPT or Claude login is an access token plus a single-use refresh token.
Every refresh spends the refresh token and returns a new pair that must be
written back. If two processes refresh with the same token, or one refreshes and
its write is lost, the file holds a spent token and the next process fails with
`refresh_token_reused`. The login is dead until a human signs in again.

RCP started a fresh Codex process for every turn, every recovery attempt, and
every startup skill probe, and a fresh Claude process for every turn. All of them
read the same credential file and any of them could rotate it. The 2026-09-12
startup gate serialized the first seconds of each launch and left the rest of the
race in place. On the team server the shared login died on 2026-09-12 with no RCP
turn running, and every episode on the machine failed silently for a day.

OpenAI closed the report of this race as not planned and named the remedy: one
shared app-server process. The desktop Codex app is exactly that, one
`codex app-server --listen stdio://`, and it does not lose its login. Anthropic
has seven open duplicates of the same race for Claude Code and documents a
one-year non-rotating `setup-token` for headless use.

## The decision

Per machine account, at most one process holds a provider credential.

- **Codex.** RCP owns one long-lived `codex app-server` child per machine
  account over stdio, under an RCP-owned `CODEX_HOME`. Every turn is a thread in
  it, every continuation a `thread/resume`, every probe a request to it. RCP
  multiplexes the single stdio connection itself. `exec` and the per-turn
  app-server runtime are retired for Codex with no silent fallback; a turn
  admitted while the owner is down is refused with that reason and the owner is
  restarted. Sign-in is the device-code flow driven through the owner and shown
  in the RCP UI.
- **Claude.** RCP stores a `claude setup-token` in its own credential store and
  injects it as `CLAUDE_CODE_OAUTH_TOKEN`. Claude processes stay per turn because
  a static token has nothing to rotate. The token is pasted into the RCP UI once
  a year.
- **Any team member may sign in**, attributed and visible to all. RCP has owners
  and members and no admin, and refusing a sign-in helps nobody when the account
  is already shared.

## What this costs, stated plainly

RCP now manages provider authentication, which the operator docs promised it
would not. A machine where the Codex desktop app also runs needs one extra
Codex login for RCP's own `CODEX_HOME`. The Claude token grants model requests
only and expires yearly; RCP warns a month ahead and on a classified failure. A
single owner process is a single point of failure per machine; supervision and a
refused launch replace a per-turn process that would fail later anyway.
