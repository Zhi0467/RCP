# Install without Open Anyway, and push to the Mac and the phone

Date: 2026-09-28
Status: scope confirmed by the human on 2026-09-28. This replaces the
2026-09-25 Inbox-push handoff, which covered the phone only. Design only.
Nothing is implemented. The design gets one xhigh review before code starts.

Close this handoff when all three hold on real hardware:

- a fresh Mac installs RCP with one pasted command and opens it with no
  System Settings approval;
- a Mac with the app running shows a native notification for a real Proposal,
  and clicking it opens that Proposal;
- a member's iPhone, added to the Home Screen on a team space, receives a push
  for a real Proposal and opens it.

## What it does

1. **One-command install.** The README's first install step becomes a single
   `curl … | sh` line. The script downloads the app from the latest
   `desktop-vX.Y.Z` pre-release, checks its `.sha256`, and puts `RCP.app` in
   `/Applications`. A file fetched by `curl` carries no quarantine flag, so
   macOS does not block the first launch.
2. **Push to the Mac.** The desktop app shows native macOS notifications.
3. **Push to the phone.** A phone added to the Home Screen on a team space gets
   Web Push notifications.

Both push paths deliver the same items, from one outbox.

## Settled

### Install

- The script is a checked-in file, `scripts/install-macos.sh`, served from the
  latest release. It is not hand-copied into docs or a command string.
- It refuses anything but Apple Silicon macOS 13 or later. Intel Macs are not
  supported.
- It verifies the published `.sha256` before touching `/Applications`. A
  mismatch leaves the existing app untouched.
- If RCP is running, it asks the person to quit with Cmd+Q and stops. It never
  kills the app or its backend.
- The manual zip download stays documented as the alternative, with its
  one-time **Open Anyway** step.
- This amends the
  [desktop installs decision](../decisions/2026-09-26-desktop-installs-follow-releases.md),
  which accepted the approval step as the install cost. The same PR updates it.
- Not in scope: Linux desktop, Windows, Intel, Homebrew, and the Tauri updater
  applying updates in place. The updater is the follow-up that stops manual
  updates from asking again.

### What notifies

- New graph attention: pending Proposals, Decisions awaiting a choice, and
  open asserted Blockers.
- An episode whose health becomes `needs_action`.
- An episode that ends: health becomes `completed`, `stopped`, or `failed`.
  Added 2026-09-28.
- Nothing else.

### Where the settings live

- **Per project:** a Notifications card in Project Settings, beside the
  machine cards. It holds one toggle per kind above. Settings are per member
  and per project. A new project starts with every kind on except "Episodes
  finished", which starts off.
- **Per device:** the existing profile **Devices** list gains a notification
  state per device: On, Off, or Delivery failed. It also gains a Test button.
  The Mac shows up in the same list as "This Mac".
- A device turns on only from a tap in that list. The phone permission prompt
  is never shown on page load.

### Payload

- A generic reason from a fixed set, the project name, and a deep link. No
  Proposal text, node titles, or other authored text.
- The project name shows on the lock screen. The Notifications card says so.

### Transport

- **Phone:** standard Web Push (RFC 8030, 8291, 8292) with VAPID. No Apple or
  Google account. The server makes outgoing HTTPS requests to each
  subscription's push service; it needs no public address or open port.
- **Dependency:** `cryptography` only, sent through the existing `httpx`. Not
  `pywebpush`, which brings `requests` and `aiohttp`. Measured on the team
  server host from the v0.4.1 wheel and lock file: 46 MB, then 62 MB with
  `cryptography`.
- **Mac:** the desktop shell adds `tauri-plugin-notification`. The shell
  subscribes to its own backend's outbox as a "desktop" delivery target and
  posts each item natively. For a team space, the target is registered through
  the existing SSH tunnel. Mac delivery happens only while the app runs.
  Closing the window hides the app and keeps it running.
- **Personal spaces:** the Mac path works. Phone push does not, because a
  personal space has no phone route. That stays out of scope.

## Design answers to the 2026-09-25 review

The first draft failed an xhigh review. Each point below answers one of its
requirements.

1. **At-least-once delivery.** There is one outbox row per (subscription,
   notification id). It records the attempt count, next attempt time, and
   last status. A stable notification id is used as the Web Push `Topic` and
   the Tauri notification id, so a repeat replaces the old notification
   instead of stacking. Honor TTL and `Retry-After`, with exponential backoff.
   A 404 or 410 deletes the subscription. Any other permanent failure shows
   as Delivery failed on the device.
2. **Trigger from accepted boundaries.** Enqueue from
   `reconcile_accepted_graph_boundaries`
   (`src/rcp/runs/transition_event_reconciliation.py`), inside the same SQLite
   transaction that advances the per-target watermark. Episode items enqueue
   from the task settlement that changes episode health. There is no poll.
   An unreachable remote project enqueues nothing, and is never read as empty
   attention.
3. **First run is a durable baseline.** Store one marker per (project, target).
   On an upgrade, a new project, or a recovered project, record the current
   attention as the baseline and send nothing. An empty baseline is still a
   baseline.
4. **Graph target scope.** Only main notifies. Branch attention appears in its
   episode's "finished" and `needs_action` items. Every record is keyed by
   project, target, kind, and item id, so branch notifications can be added
   later without a migration.
5. **One episode-health calculation.** Extract the precedence table behind
   `EpisodeResponse.health` (`src/rcp/api/episodes.py`) into one batched
   function. The API projection and the enqueue path both call it.
6. **Runtime ownership.** The sender is a background loop owned by the
   data-directory owner. It starts after startup recovery and the effect
   fence, and stops when maintenance closes admission.
7. **Server operations.** Write the VAPID key atomically, under the data
   directory, and include it in backup. New tables get a schema migration, a
   restore fingerprint, a transfer disposition, and a boundary fixture.
   Restore and transfer detach every subscription. A lost key is recovered by
   devices subscribing again, never by silently making a new key.
8. **Subscriptions belong to a device session.** Logout or device revocation
   deletes the subscription. Project membership is checked again before every
   send.
9. **Deep links.** Add hash routes for a Proposal, Decision, Blocker, or
   episode on its target. Handle a cold launch, an expired session (sign in,
   then continue to the link), and an item already resolved (open it read-only
   with its outcome).
10. **Browser lifecycle.** Web manifest identity, scope, icons, and standalone
    display. The service worker calls `showNotification` inside
    `event.waitUntil`. Reconcile `getSubscription` on each signed-in visit.
    Detect support by capability and display mode, not by device brand.
11. **Crypto tested against outside vectors.** Use RFC 8291's published
    vectors and one independent implementation as a test-only oracle. Keep
    separate signing and encryption keys, bound the plaintext, and redact
    endpoint credentials from logs.

## Checks

- First, before any other code: confirm that the ad-hoc-signed prebuilt app
  can post a native notification through `tauri-plugin-notification`. If it
  cannot, stop and bring the Mac path back for a decision.
- A crash between send and record, and one between record and send; partial
  success across two devices; first run on an empty and a non-empty project;
  an item that closes and then reopens.
- Logout, device revocation, leaving a project, and member removal each stop
  future sends.
- Backup and restore keep the key; restore does not resume old subscriptions.
- The payload contains only allowed sources.
- The install script refuses a checksum mismatch, Intel, and a running app,
  and leaves the existing app untouched in each case.
- Tests assert ids, states, and counts, never wording.
- The three real-hardware journeys in the closing condition.
