# Push notifications to the Mac and the phone

Date: 2026-09-28
Status: design settled with the human on 2026-09-28, then revised the same day
for a second xhigh review (Mac adapter, outbound push limits, watcher-free
graph reconciliation, ended episodes, sign-in during wrap-up).
Implementation started in this PR on 2026-09-28. Done: the Mac adapter
(`web/src-tauri/src/notifications.m`) and its gate probe, below; slice 2's
SQLite preferences/devices/outbox, migration and persistence boundaries,
shared episode health, watcher-independent main reconciliation, owner loop,
and preferences/desktop delivery APIs; slice 3's Web Push encryption, VAPID key,
outbound limits, and phone routes; slice 4's personal pairing code, loopback
phone listener, service worker, manifest, and pairing page. Remaining: the
Notifications card and Devices section, deep-link routes, the Mac shell's
outbox client, and the real-hardware journeys below.
This replaces the 2026-09-25 Inbox-push handoff. The Mac install and update
work moved to its own handoff and PR, which landed first (#216): the Mac check below
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
- An episode whose health becomes `needs_action`, or one wrapping up that is
  blocked on a provider sign-in (`health='wrapping_up'`,
  `blocked_reason='sign_in'`). Both use the same "Needs you" toggle.
- An episode that ends: health becomes `completed`, `stopped`, or `failed`.
- Nothing else.

Both the Mac and the phone deliver the same items, from one outbox.

The implemented backend contract and routes are in
[API, Web, and desktop projections](../specs/api-web-and-desktop-projections.md#desktop-notification-delivery).
Slice 2 does not yet post notifications through either client. Its backend
checks include an ephemeral served HTTP register/Sync/pull/acknowledge journey,
plus recovery, retry, session, migration, restore, and transfer regressions.

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
- **Outbound limits.** A subscription endpoint is only a destination the
  server posts to, so registration validates it before storing it, and every
  send validates it again:
  - `https` only, default port, on a fixed allowlist of push-service hosts
    (Apple, Google, Mozilla, Microsoft), kept in code beside the sender.
  - Every resolved address must be public. Loopback, private, link-local,
    and unique-local addresses are refused, and the connection uses the
    address that was checked.
  - Redirects are never followed.
  - Timeouts, the request body, and the stored subscription size are bounded
    in `limits.py`.
  - A refused endpoint is rejected at registration and makes no connection.
- **Mac:** `tauri-plugin-notification` cannot be used. On desktop it passes
  only title, body, icon, and sound; it drops the notification id and click
  data, and it discards delivery errors. The desktop shell instead gets a
  small native adapter over macOS `UNUserNotificationCenter`, an Objective-C
  file compiled by `build.rs` like the dictation bridge. It sets the stable notification id as the request
  identifier, carries the deep link in `userInfo`, routes clicks through the
  notification-center delegate (including a click that launches the app), and
  reports delivery errors back to the outbox. The shell
  subscribes to its own backend's outbox as a "desktop" delivery target and
  posts each item natively. For a team space, the target is registered through
  the existing SSH tunnel. Mac delivery happens only while the app runs.
  Closing the window hides the app and keeps it running.
- **Mac backlog.** On launch, the Mac drops items already resolved and items
  older than 24 hours, the same TTL phone pushes use. If more than three are
  left it posts one summary that opens the Inbox; otherwise it posts each one.
  An item is marked delivered for that Mac only after macOS accepts its
  request; a summary accepted marks every item it covers. A rejected request
  or summary leaves its items for retry.

## Design answers to the 2026-09-25 review

The first draft failed an xhigh review. Each point below answers one of its
requirements.

1. **At-least-once submission.** For a phone, a push service's success
   response means it accepted the message, not that the phone showed it;
   the service can still drop or expire it. So the outbox guarantees
   at-least-once submission to the push service across local crashes, and
   display on the phone is best effort. The Mac is the same: success means
   `UNUserNotificationCenter` accepted the request, and Focus or other
   settings may still hide it. There is one outbox row per (subscription,
   notification id). It records the attempt count, next attempt time, and
   last status. A stable notification id is the Web Push `Topic`, the Web
   Notification `tag` passed to `showNotification`, and the Mac request
   identifier, so a repeat replaces the old notification instead of stacking, even
   after delivery. Honor TTL and `Retry-After`, with exponential backoff. A
   404 or 410 deletes the subscription. Any other permanent failure shows as
   Delivery failed on the device.
2. **Trigger points.** Graph attention has its own reconciliation, not the
   watcher one. Today `reconcile_accepted_graph_boundaries` is reached only
   for targets with an active graph watcher (`watchers.py`
   `evaluate_graph_wake_boundary` and the startup sweep), so a project with
   no watchers would never notify. Notification reconciliation runs for every
   project's main target where a member has a graph kind on. It runs after
   each accepted main transition and at startup, and a failed attempt
   retries on the next sender pass. A pass with no change replays nothing. It
   reuses the same pure boundary result, and enqueues inside the same SQLite
   transaction that advances its own per-target marker. An unreachable remote
   project enqueues nothing, and is never read as empty attention.
   Episode health has many writers: the episode row, its tasks, recovery
   rows, and the provider credential store all feed it, and Stop settles some
   episodes with no task settlement. So the sender loop rechecks episodes
   about every 15 seconds with the shared health function. It stores the last
   notified observation, `(health, blocked_reason)`, per episode and enqueues
   only on a change. The recheck covers every unfinished episode, and also
   every ended episode whose stored observation is not yet its terminal one.
   An ended episode leaves the set only once its terminal observation is
   recorded, so a finish between two passes, or during downtime, still
   notifies. All of its inputs are local.
3. **First run is a durable baseline.** Store one marker per (project, target)
   and one last-notified observation per episode. On an upgrade, a new
   project, or a recovered project, record the current state as the baseline
   and send nothing. An empty baseline is still a baseline. An episode created
   after its project's baseline starts with no observation, so its first
   recheck notifies even if it has already ended.
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
   Restore detaches every subscription. Transfer of one project deletes only
   that project's notification preferences and outbox rows; device
   subscriptions belong to the space and stay. A lost key is recovered by
   devices subscribing again, never by silently making a new key.
8. **Subscriptions belong to a device.** On a team space, logout, session
   revocation, or session expiry deletes the subscription. Every team send
   first requires the owning session to be active and unexpired; a send that
   finds it expired deletes the subscription instead. Pushes alone do not
   refresh a session, so a phone that is never opened stops receiving pushes
   after `TEAM_SESSION_IDLE_DAYS`, and the Devices card says so. On a personal
   space, Remove deletes it. Before every send, including a retry, project
   membership and the member's current toggle for that project and kind are
   checked again, and so is the item itself: a resolved Proposal, Decision,
   or Blocker, or an episode no longer in the observed state, no longer
   qualifies. A send that finds the toggle off or the item no longer
   qualifying drops the row.
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

- First, before any other code: in the ad-hoc-signed prebuilt app, prove the
  native adapter can post a notification, replace it by reposting the same
  id, open the exact item from a click while running and from a click that
  launches the app, and report a delivery error. If any of these fails, stop
  and bring the Mac path back for a decision. **Passed 2026-09-28** in an
  ad-hoc-signed probe app built from the adapter file: each item held, and a
  banner showed once Focus was off. Repeat it in the packaged RCP app at the
  end.
- Registration refuses a non-`https` endpoint, a host off the allowlist, a
  host that resolves to a private or loopback address, and an oversized
  subscription, each without opening a connection; a redirect from a push
  service is not followed.
- A project with no graph watchers notifies for a new Proposal, and again
  after a restart that follows a canonical append made while RCP was down.
- An episode created and finished between two passes, and one that finished
  while RCP was down, each produce one "finished" item; an episode that
  already ended at upgrade produces none.
- A crash between send and record, and one between record and send; partial
  success across two devices; first run on an empty and a non-empty project;
  an item that closes and then reopens.
- Stop on an Auto-research and an Experiment-loop episode produces one
  "finished" item; a provider sign-in lost during wrap-up produces one
  "Needs you" item from `blocked_reason='sign_in'`, with health still
  `wrapping_up`.
- The Mac backlog: resolved and expired items are dropped, and more than three
  collapse into one summary.
- Logout, session revocation, session expiry, Remove on a personal phone,
  leaving a project, and member removal each stop future sends.
- The personal-space phone listener refuses every route outside its allowlist,
  and a redeemed pairing code grants no read access.
- Backup and restore keep the key; restore does not resume old subscriptions.
  Transferring one project leaves the space's other projects notifying.
- Turning a kind off stops a row that is already queued or retrying.
- The payload contains only allowed sources.
- Tests assert ids, states, and counts, never wording.
- The four real-hardware journeys in the closing condition.
