# Team spaces use desktop-owned pinned local HTTPS origins

**Status:** accepted on 2026-08-30 after the Q11 live WKWebView drive; hostname,
certificate lifecycle, and installed-app navigation verified on 2026-09-01.

## Decision

Each saved team connection receives the stable origin
`https://rcp-<connection-id-without-hyphens>.rcp.localhost:<local-port>`. The
macOS desktop owns one self-signed certificate and private key for all of those
local aliases. It stores the pair in one authenticated encrypted file with mode
`0600`; a separate 32-byte AES-256-GCM sealing key is the only identity material
stored in Keychain. It exposes only the certificate's SHA-256 fingerprint to
WKWebView and trusts that exact pin only inside the RCP WebView. It does not
install a certificate authority or trust setting system-wide.

The additional `rcp.localhost` label is deliberate. A certificate SAN of
`*.localhost` did not cover the generated host consistently in the production
WKWebView, while `*.rcp.localhost` is an ordinary one-label wildcard for every
saved connection. The main window admits only the personal backend, the Vite
development origin when applicable, and exact HTTPS origins from the validated
saved-connection registry. The Tauri capability names the bounded
`*.rcp.localhost` family, but that capability is not navigation authority. The
saved-origin check and the certificate pin are independent fences.

The macOS bundle's App Transport Security configuration names only
`rcp.localhost`, includes its direct connection subdomains, and enables the
per-domain exception Apple requires before an app may loosen server trust for a
self-signed HTTPS server. It does not create an HTTP entrance: the local proxy
speaks TLS, the main-window navigation owner admits only exact saved HTTPS
origins, and the challenge handler accepts only the pinned leaf after normal
hostname, validity, and server-use evaluation. No app-wide or Web-content-wide
ATS exception is enabled.

A malformed, unreadable, or unauthenticated encrypted identity fails app
startup. So does either half of a partial record: an identity without its exact
Keychain sealing key, or a key without its identity file. RCP generates a new
identity only when both are absent; it does not silently replace an invalid or
partial record. The source-built app asks Apple's signed `/usr/bin/security`
tool to read and write the short sealing key through pipes. The key is hex
encoded in memory, never placed in argv, and its legacy-Keychain ACL trusts only
that Apple tool with the stable `apple-tool:` partition rather than one changing
ad-hoc app cdhash. Version 2 of the desktop connection registry is the first
format that carries canonical HTTPS origins. Its exact previously shipped
`rcp-<id>.localhost:<port>` origin is migrated in place to
`rcp-<id>.rcp.localhost:<port>` without changing connection identity, port,
server identity, or member credential. Earlier registry formats still have no
production writer and remain rejected rather than guessed through an
unverified origin rule.

The encrypted identity record is version 4. It records certificate expiration,
uses a 365-day self-signed leaf that explicitly carries `CA:FALSE`, TLS
server-auth extended usage, and digital-signature/key-encipherment usage, and
rotates atomically during startup in the last seven days before expiry. Valid
version 1 through version 3 identity records are reissued through the same
authenticated storage owner; malformed, unauthenticated, unknown-version, or
partial records still fail closed.

This source-mode ACL is a rebuild-stability boundary, not same-account process
isolation. Because it authorizes the general-purpose `/usr/bin/security`
executable, another process running as the same macOS user could invoke that
tool for a known service and account. That is within RCP's current cooperative
provider threat model, which does not promise read secrecy from hostile
same-account processes. Before wider public distribution, a signed build must
replace this source-only compromise with a stable app identity and app-bound
credential access.

The pre-team source build's direct `desktop-identity/v1` Keychain record is not
migrated: a rebuilt ad-hoc app cannot reliably read that cdhash-bound value
without an access prompt. No team session used that unshipped record. The new
storage pair therefore starts at its own versioned names, while the old record
is left untouched for source rollback rather than deleted or silently exported.

## Why

Team sessions use an HTTP-only, `Secure`, `__Host-` cookie. Ports do not isolate
cookies, and a real WKWebView drive proved that plain HTTP on both generated
`.localhost` aliases and exact `localhost` loses that cookie. The same drive
proved that app-scoped HTTPS pinning preserves it, isolates two team hosts,
survives an app restart, keeps it unreadable to JavaScript, and refuses an
allowlisted origin presenting another certificate. Each run first proves the
certificate is not trusted by the host, so the result does not depend on a
system trust mutation.

The deterministic hostname binds browser state to the durable connection
identity while leaving the local port available to the desktop-owned proxy. A
single desktop identity is sufficient because origin isolation comes from the
hostnames; the pin authenticates the local proxy boundary, not an individual
team server.

## Rejected alternatives

- Different ports on `127.0.0.1`: cookies ignore ports and would collide.
- Plain HTTP `.localhost`: the required `Secure` cookie failed in WKWebView.
- Extra loopback addresses: stock macOS could not bind them without privileged
  network mutation, and that would not repair the HTTP cookie failure.
- A system-installed certificate authority: broadens trust beyond RCP and adds
  machine administration the product does not need.
- A custom protocol or full request bridge: materially larger than the proven
  server-served-UI design.

## Remaining qualification

D3 now terminates this HTTPS endpoint over the desktop-owned real SSH tunnel.
The retained real WKWebView probe passes login and restart with the repaired
wildcard hostname, preserves two isolated cookies, and refuses a second
certificate. The production leaf profile and narrow per-domain ATS exception
were then verified by opening the real enrolled team project from the installed
source-built app. The unbundled probe did not expose that the installed app's
default ATS policy still blocked its otherwise accepted manual trust decision.
D4-D5 are implemented and hermetically verified, and one real team member
connection is enrolled and stored, but the exact visible two-team-space
switching drive remains open. A second macOS machine and signed packaged app
remain later compatibility qualification; they do not reopen this source-built
client decision.
