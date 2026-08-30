# Team spaces use desktop-owned pinned local HTTPS origins

**Status:** accepted on 2026-08-30 after the Q11 live WKWebView drive.

## Decision

Each saved team connection receives the stable origin
`https://rcp-<connection-id-without-hyphens>.localhost:<local-port>`. The macOS
desktop owns one self-signed certificate and private key for all of those local
aliases. It stores the pair as one versioned Keychain item, exposes only the
certificate's SHA-256 fingerprint to WKWebView, and trusts that exact pin only
inside the RCP WebView. It does not install a certificate authority or trust
setting system-wide.

The main window admits only the personal backend, the Vite development origin
when applicable, and exact HTTPS origins from the validated saved-connection
registry. The Tauri capability names the bounded `rcp-*.localhost` family, but
that capability is not navigation authority. The saved-origin check and the
certificate pin are independent fences.

A malformed or unreadable Keychain identity fails app startup. RCP generates a
new identity only when its exact Keychain item is absent; it does not silently
replace an invalid record. Version 2 of the desktop connection registry is the
first format that carries these canonical HTTPS origins. The earlier format had
no production writer, so it is rejected rather than migrated through an
unverified origin rule.

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

D3-D5 still have to terminate this HTTPS endpoint over a real SSH tunnel,
establish a real team session, and drive two saved team spaces in the source-built
app. A second macOS machine and signed packaged app remain later compatibility
qualification; they do not reopen this source-built client decision.
