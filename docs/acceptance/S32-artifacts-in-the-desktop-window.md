---
id: S32-artifacts-in-the-desktop-window
status: pending
tier: hermetic
driver: desktop
covered_by: none
invariants: [10e]
---

# A preview opens and a download lands, and the isolation is stronger than the browser's

The artifact contract does not change on the desktop: the preview is optional,
the answer and the graph do not depend on it, and a preview that cannot be shown
says so without touching the reply or the verdict
([S16](S16-chat-artifact-contract.md)). What changes is the machinery underneath,
and what a breach would cost.

Today the Open control asks for a new browser window, nulls its opener, and
navigates it to the preview URL — and if it does not get a usable window handle
back, it marks the artifact unavailable. That fallback is correct and it is also
a trap: in an embedded webview the human would see a tidy "Preview unavailable"
and reasonably conclude the backend lost the file. The same applies to Download,
which relies on the browser honoring a download attribute.

The isolation itself is two layers (the wrapper builder in
[artifacts.py](../../src/rcp/artifacts.py)):
an RCP-origin wrapper document holding an `<iframe sandbox="allow-scripts"
srcdoc="…">` with no `allow-same-origin`, so the artifact sits in an opaque
origin and reaches the wrapper only by `postMessage` carrying a per-response
secret. That design was sound against a browser threat model, where the worst
case of a navigation is that a tab you already opened goes to a website.

Inside an application, both halves get worse. The preview window is an
application webview, so whether it can reach the native layer depends entirely on
how capabilities are scoped — a permissive window pattern would hand
agent-authored content a path to the shell, which has no analogue in a browser
because there is nothing there to reach. And the wrapper's one privileged
action, navigating itself to any `http`/`https` URL an artifact names, would put
a remote page inside the application wearing whatever the app grants its own
windows. That second one is reachable today by an artifact emitting a single
reference link — the existing, intended feature.

So the desktop preview is held to a stronger boundary than the browser one, in
proportion to what a breach reaches.

## UI path

Confirmed with the human on 2026-07-31.

- **Open shows the preview in a second native window** RCP owns, falling back to
  a panel inside the main window if the isolation guarantees cannot be met
  natively.
- **Preview windows get an explicitly empty capability set.** No IPC bridge, by
  configuration rather than by default, and the scenario asserts it.
- **A reference link leaves the application.** The `rcp-reference` action stops
  calling `location.assign` and hands the URL to the system browser instead. This
  changes the **web** entrance's behavior too — clicking a reference link today
  navigates the preview window in place — and the change is deliberate: a link
  out of an artifact should leave RCP in both entrances.
- **An app-level navigation rule** so no RCP-owned webview can end up on a remote
  origin regardless of what any document asks for.
- **Download** writes the file where the human chooses, and reports failure
  rather than failing silently.
- **Bytes stay temporary.** Nothing is copied into canonical state, the
  transcript, or durable app storage by previewing or downloading.

The navigation-policy comment in [artifacts.py](../../src/rcp/artifacts.py)
records that Chromium does not enforce `navigate-to`, leaving the opaque
sandbox as the real boundary. Whether WebKit behaves the same is a fact to
establish when the shell first runs, not to assume; if it differs, it differs in
our favor.

Deliberately not possible: a preview that renders inside the RCP document, a
preview window with any IPC capability, an RCP-owned webview on a remote origin,
a download that lands somewhere the human did not choose, and a shell limitation
reported as an expired artifact.

## Drive

1. Run a chat turn that produces an HTML artifact and one unsupported file.
2. Press Open, then Download.
3. From inside the preview, attempt to reach the parent, reach the native layer,
   open a popup, submit a form, and start a download.
4. Click a reference link in the artifact, in both entrances.
5. Let an artifact expire, then press Open and Download again.
6. Repeat the whole drive against a remote execution host.

## Assert

- `preview_opens_in_a_surface_rcp_owns`
- `preview_window_has_no_ipc_bridge`
- `preview_cannot_reach_or_navigate_the_parent`
- `preview_cannot_open_popups_submit_forms_or_start_downloads`
- `inline_javascript_still_runs`
- `a_reference_link_leaves_the_application`
- `no_rcp_owned_webview_reaches_a_remote_origin`
- `download_lands_where_the_human_chose`
- `download_failure_is_reported_not_silent`
- `an_expired_artifact_still_reads_as_unavailable`
- `a_shell_limitation_is_never_reported_as_expiry`
- `no_artifact_byte_enters_canonical_state_or_the_transcript`
- `preview_outcome_changes_no_reply_verdict_or_graph`

## Failure means

The preview silently degrades to "unavailable" and the human debugs the backend
for a frontend reason. Or the preview keeps working and quietly loses its
isolation — and in a native shell the thing on the other side of that boundary is
no longer a browser tab.
