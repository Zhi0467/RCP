# The personal backend requires an owner session

Date: 2026-10-04. Status: active. Confirmed by the human on 2026-10-04.
Current behavior is in the
[projects spec](../specs/projects-spaces-and-operations.md#personal-sign-in).

## Decision

A personal backend refuses `/api` requests that carry no owner session, except
`/api/health` and the phone-pairing listener's routes. It reuses the team
session machinery. The desktop app exchanges a Keychain-held owner secret for a
session. A plain browser signs in through a one-time URL that `rcp serve` prints.
No agent launch receives an owner credential.

## Why

- **Agents are local processes.** The old rule assumed only the owner can reach
  loopback. On 2026-10-04 a Codex Work turn read the project list from inside
  its sandbox with no credential. The same path can approve a Proposal, which only
  a human may do.
- **The browser makes it one click.** An agent's browser could open RCP and press
  Approve. Blocking RCP's address inside the browser would not help, because the
  shell already reaches the API. The fix belongs in the API.
- **The machinery exists.** Team spaces already have hashed session tokens,
  cookies, exchange, and single-use codes.

## What this gives up

- **A determined same-account agent.** Claude and OpenCode Work shells are
  unbounded. An agent that deliberately reads the desktop's cookie store from
  disk could still act as the owner. This closes the accidental and trivial paths,
  not that one.
- **Zero-step local access.** A plain browser now needs the sign-in URL once, and
  `rcp` commands that open a project hand off to the signed-in UI.
