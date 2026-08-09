---
id: S79-cold-desktop-launch-renders
status: implemented
tier: hermetic
driver: desktop
covered_by: none
invariants: [8]
last_passed: 2026-08-09 — two clean cold launches and one warm reopen in RCP Dev,
  with the project index painted before interaction and no top-left black tile
---

# A cold desktop launch never comes to rest on a blank window

The desktop app owns its backend, and on a cold start that backend is not
instant: `--web-assets source` runs a full frontend build before it serves. So
there is a window of seconds where the origin the window wants does not exist
yet.

Pointing a webview at an origin that is not listening produces a failed
provisional navigation, and a webview does not retry one. Nothing later corrects
it either, because the startup path treats "the target URL equals the URL we
already asked for" as "the window is already there" — which is true only when
that first load succeeded. Cold start is exactly the case where it did not, so
the recovery navigation was skipped precisely when it was needed and the window
stayed blank until the human relaunched.

**The window must not be aimed at the backend before the backend answers.** The
8s handshake reveal keeps its job of refusing to hide a failure; the promise
here is that what it reveals is recoverable rather than permanently dead.

## UI path

Confirmed with the human on 2026-08-07.

- **Cold launch** — nothing listening on 8421. The app starts, the backend
  builds and boots, and the window appears showing the project index. No blank
  frame that stays blank, no transient black patch in the top-left corner, and
  no click or relaunch needed to make the window paint correctly.
- **Warm launch** — a backend is already running. The window renders
  immediately, reusing that backend, with no second navigation and no visible
  reload flicker.
- **`tauri dev`** — the Vite dev server is the frontend, so the window still
  navigates to it eagerly at creation. Deferring does not apply.
- **`RCP_DESKTOP_FRONTEND_URL`** — an explicit approved frontend override is
  honored. An already-serving frontend such as 5173 is loaded eagerly, but an
  override resolving to the backend origin on 8421 stays on the blank
  placeholder until backend readiness is confirmed.

## How it is checked

`driver: desktop`, because the failure lives entirely in the native shell's
startup ordering. The startup milestones on stderr are the evidence:

- cold start prints that the window is waiting for the backend, then
  `backend ready`, then a navigation to the backend origin, then
  `showing the RCP window`;
- `the frontend handshake did not arrive` must be absent — its presence means
  the frontend never booted;
- before any interaction, the rendered window is confirmed to contain the
  fully painted project index, not an empty document or a transient black patch
  in its top-left corner.
