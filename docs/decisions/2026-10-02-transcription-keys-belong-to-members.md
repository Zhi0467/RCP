# Transcription keys belong to members

Date: 2026-10-02. Status: active. The work is planned in the
[model-backed dictation handoff](../handoffs/handoff-2026-10-02-model-backed-dictation.md).

## Decision

A transcription service key is a **member's service connection**. Each member
connects their own keys. The RCP backend stores them, privately and outside
backups, and calls the service for that member. Keys never reach a client.

Audio goes from the client to the RCP backend, then to the service. The backend
holds it only in memory for that one request.

A service connection is not a provider login. It does not use
`ProviderCredentialStore`, its account gate, or its readiness state.

## Why

- **Billing and privacy are personal.** Dictation audio is one person's voice,
  and the bill is one person's. A shared key would make one member pay for
  everyone and see everyone's usage. RCP has no administrator to own a shared
  key.
- **Provider logins are shared for a different reason.** A provider login
  belongs to an execution account that runs everyone's tasks. Transcription has
  no shared executor: each request is one member's.
- **One path for every client.** The desktop, browser, and phone clients all
  upload audio the same way. A client-side key would need the desktop Keychain,
  which caps values at 64 bytes, and the web and phone clients have no Keychain.
- **The key stays in one place.** A backend call keeps the key out of the
  browser, where any page script could read it.

## What this gives up

- **The service account can read every member's keys.** Per-member storage
  separates members in the product, not on the machine. Members are already
  trusted with that account (see the
  [member terminal decision](2026-09-19-a-member-terminal-inherits-the-work-trust-boundary.md)).
- **Audio passes through the RCP server.** On a team space, the team server
  sees each segment in memory. A direct client-to-service path would avoid that,
  but would need client-held keys or short-lived service tokens per service.
- **A restore loses connections.** Members connect again after a restore, as
  they sign providers in again.
- **No outbound address fence against members.** A custom server URL may be
  `https` anywhere or `http` on loopback. RCP does not block a member from
  pointing it at an internal address, since that member already has the same
  reach from a project terminal.
