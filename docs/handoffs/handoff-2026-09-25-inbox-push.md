# RCP tells your phone when you are needed

Date: 2026-09-25
Status: confirmed by the human on 2026-09-25 as a separate pull request after
[the phone UI and Agents panel](handoff-2026-09-25-phone-ui-and-agents-panel.md).
An xhigh design review found the first draft not ready. This file records the
settled decisions and the requirements that review added. The design must be
rewritten against them and reviewed again before implementation starts.
Nothing is implemented.

Close this handoff when a member's iPhone, added to the Home Screen on a team
space, receives a push for a real Proposal and opens that Proposal, and the
checks below pass.

## What it does

A member turns on notifications per device. When something new needs a human
in a project they belong to, each of their enabled devices gets a Web Push
notification. Tapping it opens the item.

## Settled

- **Standard Web Push** (RFC 8030, 8291, 8292) with VAPID. No Apple or Google
  account. The server makes outgoing HTTPS requests to the push service in each
  subscription; it needs no public address or open port.
- **Dependency: `cryptography` only**, sent through the existing `httpx`. Not
  `pywebpush`, which brings `requests` and `aiohttp`. Measured on the team
  server host by rebuilding a venv from the v0.4.1 wheel and its lock file with
  `uv pip install --no-deps -r requirements.lock.txt`: 46 MB, and 62 MB after
  adding `cryptography`. `pywebpush` measured about 19 MB on macOS.
- **What notifies:** new graph attention (pending Proposals, Decisions awaiting
  choice, open asserted Blockers) and episodes whose health becomes
  `needs_action`. Nothing else.
- **Payload:** a generic reason chosen from a fixed set, and a link. No
  Proposal action lines or other authored text. The project name may appear on
  the lock screen; the settings control says so.
- **Team spaces** are the product path, reached over the tailnet's HTTPS front.
- The macOS desktop app's web view is out of scope; native notifications are a
  separate question.

## Requirements from the design review

Each must be answered in the rewritten design.

1. **Delivery is at least once, not exactly once.** HTTP delivery and SQLite
   cannot commit together. Keep a per-subscription outbox with progress, a
   stable notification id so a repeat replaces rather than stacks, TTL,
   `Retry-After` and backoff, and visible permanent failures. Delete a
   subscription on 404 or 410.
2. **Trigger from accepted boundaries, not a 30-second poll.** Sampling misses
   changes between samples and costs a remote read per project. Reuse the
   existing transition-boundary reconciliation and its durable per-target
   watermarks (`src/rcp/runs/transition_event_reconciliation.py`), task
   settlement, and the display-cache head probe. Never read unavailable remote
   state as empty attention.
3. **First run is a durable per-project, per-target marker.** Upgrading, adding
   a project, or recovering an unreachable one records a baseline without
   sending. An empty baseline is still a baseline.
4. **Graph target scope.** Decide whether branch Inboxes notify, and key every
   record by project, target, kind, and item id.
5. **One episode-health calculation.** Episode attention is
   `EpisodeResponse.health`, not the stored status, and some `needs_action`
   results have no `blocked_reason`. Expose one shared, batched calculation for
   both episode modes rather than a second copy.
6. **Runtime ownership.** The sender starts only after startup recovery and the
   effect fence, stops for maintenance, and runs once per data-directory owner.
7. **Server operations.** The VAPID private key is written atomically and
   included in backup. New tables get a schema migration, a restore fingerprint,
   and a transfer disposition. Restore and transfer detach existing
   subscriptions rather than keep sending from two places. A lost key is
   recovered by re-subscribing, never by silently replacing it.
8. **Subscriptions belong to a device session.** Logging out or revoking a
   device deletes its subscription. Project access is rechecked before every
   send; leaving one project does not silence another.
9. **Deep links.** Add hash routes that open a specific Proposal, Decision,
   Blocker, or episode on its graph target, and handle a cold launch, an
   expired session, and an item that is already resolved.
10. **Browser lifecycle.** Manifest identity, scope, icons, and standalone
    display. Ask permission only from a tap. The service worker always calls
    `showNotification` inside `event.waitUntil`. Reconcile the stored
    subscription with `getSubscription` on each signed-in visit. Detect support
    by capability and display mode, not by device brand.
11. **Our own crypto is tested against outside vectors.** RFC 8291's published
    vectors and an independent implementation, not only our own round trip.
    Separate signing and encryption keys; bounded plaintext; redact endpoint
    credentials from logs.

## Checks

- Crash between send and record, and between record and send; partial success
  across two devices; first run on an empty and a non-empty project; an item
  that closes and reopens.
- Logout, device revocation, leaving a project, and removal each stop future
  sends.
- Backup and restore keep the key; restore does not resume old subscriptions.
- The payload contains only allowed sources.
- Tests assert ids, states, and counts, never wording.
- The real-device journey in the closing condition.
