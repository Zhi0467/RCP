# The native team entrance negotiates one thin protocol range

**Status:** accepted on 2026-09-01 after a code-backed design grilling.

## Decision

The source-built desktop and a team server each publish one inclusive integer
range for the native team entrance. The desktop selects the highest common
integer. Protocol 1 covers only health/source/space identity, enrollment or
permanent-token exchange, returned member identity, the bounded project-card
read, and HTTP-only browser-cookie installation. The desktop sends the selected
integer in `RCP-Team-Shell-Protocol` on those requests and accepts the entrance
only when the server echoes that exact value.

The first range is `[1, 1]`. A missing server range, no overlap, or a missing or
different echo fails closed before cookie installation or navigation. The
diagnostic names the desktop build's exact source commit and the installed
server's reported running commit. If the server range ends below the desktop
range, it says to update the server from current `origin/main`; if the desktop
range ends below the server range, it says to update and rebuild the desktop.
Protocol selection, not Git ancestry or app semantic version, decides whether
the entrance is compatible.

Each advertised version has an immutable checked-in contract fixture consumed
by the existing Python, Web, and Rust test jobs. A breaking request, response,
or meaning adds a new version before either side advertises it. A release may
temporarily advertise an overlap such as `[1, 2]`; the selected value is `1`.
Retirement deliberately narrows the range. There is no automatic
current-plus-previous window, time window, device inventory, or generic
feature-capability registry.

Compatibility is current connection evidence, not saved connection truth.
Desktop connection registry version 3 removes the previously shipped
`minimum_shell_version` field. Its automatic version-2 migration preserves the
connection id, SSH target and port, expected space id, pinned local origin,
cached cards, optional operator route, and the independent Keychain account.
Unknown registry versions and unknown fields still fail closed.

The ordinary server-served Web/API surface and server operator capability are
outside this protocol. Project provisioning, transfer, provider execution,
backup, update, and other features continue to use their owning contracts after
the native shell has established the browser session.

## Why

The desktop-to-server shell boundary changes less often than the Web app but can
still become unsafe across source updates. A one-number minimum ties compatibility
to release chronology and gets saved as stale authority. Commit comparison is
not a protocol, while a feature-by-feature capability map would turn a five-step
entrance into a second product API. A small overlap range expresses exactly the
compatibility question the shell has to answer and nothing more.

## Rejected alternatives

- A minimum Git commit: commits are not globally ordered and do not state a wire
  contract.
- Application semantic-version comparison: release identity and entrance
  compatibility are separate facts.
- One capability per server feature: server-served features are not native
  shell responsibilities.
- Silent legacy fallback: either side missing the range is the initial cutover
  case and must be updated.
- Persisting the negotiated range or a minimum version: every connection proves
  the current pair again.
