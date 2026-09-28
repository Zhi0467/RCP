# Push notifications to the Mac and the phone

Date: 2026-09-28
Status: design settled with the human on 2026-09-28. Nothing is implemented.
This replaces the 2026-09-25 Inbox-push handoff. The Mac install and update
work moved to its own handoff and PR, which lands first: the Mac check below
runs on an app that install produced. Mac and phone push ship together in one
PR.

Close this handoff when all four hold on real hardware:

- a Mac with the app running shows a native notification for a real Proposal,
  and clicking it opens that Proposal;
- a Mac that was closed shows the backlog rules below on its next launch;
- a member's iPhone, added to the Home Screen on a team space, receives a push
  for a real Proposal and opens it;
- an iPhone paired notify-only with a personal space receives a push while the
  Mac runs RCP.

## What notifies

- New graph attention on main: pending Proposals, Decisions awaiting a choice,
  and open asserted Blockers.
- An episode whose health becomes `needs_action`.
- An episode that ends: health becomes `completed`, `stopped`, or `failed`.
- Nothing else.

Both the Mac and the phone deliver the same items, from one outbox.

## Settled

### Settings

- **Per project:** a Notifications card in Project Settings, beside the
  machine cards. One toggle per kind above, per member and per project. A new
  project starts with every kind on except "Episodes finished", which starts
  off.
- **Per device:** every space shows a **Devices** section, with one row per
  device: On, Off, or Delivery failed, plus a Test button. A device turns on
  only from a tap on its row. The phone permission prompt is never shown on
  page load.
  - A team space lists the member's sessions, as today. The Mac app is one of
    them, shown as "This Mac".
  - A personal space has no sign-in, so its rows are notification targets:
    "This Mac" and any notify-only phones. They have no Revoke; a phone row has
    Remove.

### Personal-space phones are notify-only

- **Connect a device** in a personal space issues a one-time pairing code, in
  the team code format with the same expiry and lockout. Redeeming it grants
  one thing: registering that phone's push subscription. It creates no session
  and grants no read access.
- The phone needs one HTTPS visit to the Mac to add the web app to its Home
  Screen and register. The person sets up that route, for example
  `tailscale serve` on the Mac.
- That route never reaches the owner API. A personal backend treats every
  request as the owner, so the route points at a separate listener that serves
  only the web-app shell, the manifest, the service worker, and the
  registration route.
- Tapping the notification opens nothing on the phone; the person opens the
  item on the Mac. Pushes are sent only while the Mac is awake with RCP
  running.

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
- **Mac backlog.** On launch, the Mac drops items already resolved and items
  older than 24 hours, the same TTL phone pushes use. If more than three are
  left it posts one summary that opens the Inbox; otherwise it posts each one.
  Every item is then marked delivered for that Mac.

## Design answers to the 2026-09-25 review

The first draft failed an xhigh review. Each point below answers one of its
requirements.

1. **At-least-once delivery.** There is one outbox row per (subscription,
   notification id). It records the attempt count, next attempt time, and
   last status. A stable notification id is the Web Push `Topic`, the Web
   Notification `tag` passed to `showNotification`, and the Tauri notification
   id, so a repeat replaces the old notification instead of stacking, even
   after delivery. Honor TTL and `Retry-After`, with exponential backoff. A
   404 or 410 deletes the subscription. Any other permanent failure shows as
   Delivery failed on the device.
2. **Trigger points.** Graph attention enqueues from
   `reconcile_accepted_graph_boundaries`
   (`src/rcp/runs/transition_event_reconciliation.py`), inside the same SQLite
   transaction that advances the per-target watermark. An unreachable remote
   project enqueues nothing, and is never read as empty attention. Episode
   health has many writers: the episode row, its tasks, recovery rows, and the
   provider credential store all feed it, and Stop settles some episodes with
   no task settlement. So the sender loop rechecks every unfinished episode
   about every 15 seconds with the shared health function, stores the last
   notified health per episode, and enqueues only on a change. All of its
   inputs are local.
3. **First run is a durable baseline.** Store one marker per (project, target)
   and one last-notified health per episode. On an upgrade, a new project, or
   a recovered project, record the current state as the baseline and send
   nothing. An empty baseline is still a baseline.
4. **Graph target scope.** Only main notifies. Branch attention appears in its
   episode's "finished" and `needs_action` items. Every record is keyed by
   project, target, kind, and item id, so branch notifications can be added
   later without a migration.
5. **One episode-health calculation.** Extract the precedence table behind
   `EpisodeResponse.health` (`_episode_projection` in `src/rcp/api/episodes.py`)
   into one batched function. The API projection and the recheck both call it.
6. **Runtime ownership.** The sender is a background loop owned by the
   data-directory owner. It starts after startup recovery and the effect
   fence, and stops when maintenance closes admission.
7. **Server operations.** Write the VAPID key atomically, under the data
   directory, and include it in backup. New tables get a schema migration, a
   restore fingerprint, a transfer disposition, and a boundary fixture.
   Restore and transfer detach every subscription. A lost key is recovered by
   devices subscribing again, never by silently making a new key.
8. **Subscriptions belong to a device.** On a team space, logout or session
   revocation deletes the subscription. On a personal space, Remove deletes
   it. Project membership is checked again before every send.
9. **Deep links.** Add hash routes for a Proposal, Decision, Blocker, or
   episode on its target. Handle a cold launch, an expired session (sign in,
   then continue to the link), and an item already resolved (open it read-only
   with its outcome). A notify-only phone gets no deep link.
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
- Stop on an Auto-research and an Experiment-loop episode produces one
  "finished" item; a provider sign-in lost during wrap-up produces one
  `needs_action` item.
- The Mac backlog: resolved and expired items are dropped, and more than three
  collapse into one summary.
- Logout, session revocation, Remove on a personal phone, leaving a project,
  and member removal each stop future sends.
- The personal-space phone listener refuses every route outside its allowlist,
  and a redeemed pairing code grants no read access.
- Backup and restore keep the key; restore does not resume old subscriptions.
- The payload contains only allowed sources.
- Tests assert ids, states, and counts, never wording.
- The four real-hardware journeys in the closing condition.
