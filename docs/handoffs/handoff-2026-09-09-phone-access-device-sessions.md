# Phone access: device sessions and mobile rendering

Date: 2026-09-09
Status: partly implemented on `feat/team-device-sessions`. Sessions carry an
independent public UUID through storage migration 14, the member-scoped list and
revoke routes exist, and the identity panel has a Devices section. Stored device
labels and the two mobile fixes below are in progress on the same branch. All
three open questions are settled; see "Settled decisions". Nothing has been
verified against a real phone yet, because no pairing flow exists.

Close this handoff when a member can see their own connected devices in the
identity panel, revoke any one of them without disturbing the others, and reach
that panel from a phone. The last of those depends on the transport work, which
is deliberately excluded here — this handoff stays open until it is resolved
elsewhere or the closure condition is narrowed by a human.

## Why this exists

A member wants to reach RCP from a phone. The team space already carries
everything the phone needs except two things: the member cannot see what is
connected, and the narrow-width layout has two defects.

Remote access itself is not part of this handoff. The transport question (SSH
tunnel, tailnet, or public TLS) is separate and unsettled; see "Deliberately out
of scope".

## Settled decisions

These were decided by the human on 2026-09-09 and are not open for
reinterpretation during implementation.

- **A phone holds a session, not a credential.** The member token stays on the
  machine that enrolled. The phone receives the ordinary `__Host-rcp_session`
  cookie and never stores the long-lived token. A lost phone therefore leaks a
  session, which idles out, and not a credential.
- **The phone is subordinate in lifecycle, not in network path.** It talks to
  the server directly. Routing it through a member's desktop is rejected: it
  would put a sleeping laptop in the critical path.
- **Authority stays per-human.** A phone session carries the same authority as
  any other session for that member. Device-scoped capability is rejected;
  approving the queue while away from the desk is the point of the feature.
- **No per-device tokens.** One active credential per member stays as it is.
  Revocation granularity is solved at the session layer, which needs no change
  to the credential model.
- **No cap on concurrent sessions.** The human rejected a cap. Visibility and
  revocation are the requirement.
- **A phone session is an ordinary session.** Do not add a device `kind`,
  `surface`, or equivalent selector to session rows or to the resolve path. The
  subordinate model works precisely because the session type is uniform.
- **A device label is text a human typed.** Never infer one. No user-agent
  parsing, no request fingerprint, no name generated from the session id. A
  browser cannot obtain the device model anyway: iOS Safari reports a generic
  `iPhone` with no model and does not implement `navigator.userAgentData`, so
  "iPhone 16e" can only come from the person holding it.
- **Naming is mandatory in the pairing UI, optional in the API.**
  `POST /api/team/session/exchange` already requires a team-shell protocol
  declaration on an installed server, so a newly required body field would break
  shells at existing protocol versions and force a bump. The route accepts a
  label; the pairing screen is what refuses to continue without one. A label is
  display text with no authority, so client-side enforcement is sufficient.
  Sessions created without one store the literal `Unnamed device`, as do rows
  backfilled by the migration.

## Verified current behavior

Read from the code on 2026-09-09. These are the facts the plan builds on.

- One member token backs unlimited concurrent sessions. `create_team_session`
  inserts a row and neither consumes nor rotates the token
  (`src/rcp/storage/spaces.py`).
- The only uniqueness constraint is on tokens, not sessions:
  `team_member_tokens(user_id) WHERE revoked_at IS NULL`
  (`src/rcp/storage/base.py`).
- Sessions expire on a sliding 14-day idle window. `resolve_team_session`
  rewrites `expires_at` on every request, and the middleware re-sets the cookie
  on every authenticated response, so browser and server slide together.
- `rotate_team_token` and `revoke_team_token` both run
  `DELETE FROM team_sessions WHERE user_id = ?`. Revocation today is
  all-or-nothing for a member.
- `detach_space_authentication_for_restore` deletes every session. A restore
  un-pairs all devices; an ordinary restart or release update does not.
- The team route surface is nine routes. None list or revoke an individual
  session; `logout` ends only the caller's own.
- Nothing in the web client or the native shell calls
  `/api/team/credential/rotate`. Its only callers are
  `tests/test_team_authentication.py` and the route inventory.

## Mobile scan, 2026-09-09

Measured against the running app at 390x844, and at 360x780 where noted. Each
view was probed for page overflow, elements wider than the viewport, multi-column
grids that fail to collapse, and control heights below the 44px touch guideline.

No view overflows the page horizontally. `documentElement.scrollWidth` equalled
the viewport width on every surface below, and no element measured wider than the
viewport.

| Surface | Verdict | Detail |
| --- | --- | --- |
| Landing index | Good | Single column. 4 controls under 44px. |
| Overview | Good | 11 controls under 44px. |
| Inbox (`attention`) | Good | Verified populated, 3 open blockers. |
| Research (`scientific`) | Good | 15 controls under 44px. |
| Runs (`execution`) | Good | 11 controls under 44px. |
| Paper | Good | 12 controls under 44px. |
| Settings | Acceptable | Dense; 101 controls under 44px. `agent-usage-grid` holds three columns at ~338px total. |
| Node detail window | Good | Fixed, 360x714 at (18,78), fully on screen. Action buttons 34px. |
| Identity panel | Good | 358px wide, fits. Device list belongs here. |
| Chats | **Defect** | See below. |
| DAG | **Defect** | See below. |

**Chats does not stack.** `.chats-workspace` is a three-column grid. Its 560px
breakpoint narrows the conversation list to `110px` instead of collapsing it, so
the composer measures 214px at 390px viewport and 184px at 360px. The fix is a
narrow-width rule that drops to one column with the list behind a toggle, not a
smaller fixed column.

**The DAG canvas is below the fold.** `dag-controls` occupies 356px, so
`dag-scroll` begins at y=618 and receives 370x523 for a 38-node, 58-edge graph.
The controls need to collapse at narrow widths before the canvas is usable.

**Touch targets are globally undersized.** Most controls render 32-38px against
a 44px guideline. This is a consistent system-wide value, not a per-view bug, so
it belongs to the visual design spec rather than to a view fix.

## Implementation plan

### Storage

Add an opaque public identifier to `team_sessions`. Follow the established
pattern: `_run_storage_schema_migration` at `version=14`, using `_ensure_column`
for `session_id TEXT`, backfilling existing rows, and adding a unique index. The
current ledger head is 13.

Never expose `session_hash` or any value derived from the session token. The
public identifier must be independent of the secret.

Add store methods for listing a member's own sessions and deleting one by public
identifier, scoped to that member's `user_id`.

### API

Two routes, both inside the existing team authentication envelope:

- `GET /api/team/sessions` — the acting member's own sessions only.
- `POST /api/team/sessions/{session_id}/revoke` — delete one row belonging to
  the acting member.

A member must never see or revoke another member's sessions. A revoke naming an
identifier outside the acting member's set returns the same not-found result as
an unknown identifier; do not distinguish the two.

Both routes must be added to `tests/test_route_inventory.py`, in the route tuple
list and in the handler-to-file map.

### Projection

The response exports the decision; the web layer never derives it. Each entry
carries its public identifier, `created_at`, `last_seen_at`, `expires_at`,
`is_current`, and `can_revoke`. The current session reports `can_revoke: false`
— ending it is Logout, which already exists.

Restate the shape once in `web/src/types.ts`. That file is a shared contract and
is edited serially by the main agent, not by an implementation subagent.

### Web

Add a devices section to `web/src/components/LandingIdentityMenu.tsx`, beside
Team invitations and under the same team-space condition. It lists each session
with its last-seen time, labels the current one, and offers Revoke on the others.
The panel already fits at 390px.

### Tests

- Backend: list returns only the acting member's sessions; revoke removes one row
  and leaves the others usable; a member cannot revoke another member's session;
  the current session is reported as not revocable; the response carries no
  token-derived value.
- Web: the section renders for a team space, is absent for a personal space, and
  the revoke control is absent on the current session.

## Resolved on 2026-09-09

All three questions this handoff opened with are settled.

1. **No acceptance scenario.** The human chose to retire the acceptance system
   entirely rather than extend it. That retirement is a separate change; nothing
   here adds to `docs/acceptance/`.
2. **Device labels are typed by a human.** See "Settled decisions".
3. **Both mobile fixes belong on this branch.** Chats stacking and the DAG
   control collapse ship here. The global 32-38px control height against a 44px
   touch guideline does not; it changes every screen including desktop and needs
   its own decision.

## Deliberately out of scope

- **Transport.** How a phone reaches the server is unsettled. The team listener
  is loopback-only and `docs/server.md` states that opening 8421 publicly is not
  supported. A tailnet keeps that property; a public TLS front changes the threat
  model and needs its own decision record. Nothing here depends on which is
  chosen.
- **Personal spaces.** A personal space has no authentication at all
  (`IdentityAccess.acting_user` returns the local owner directly), so it has no
  sessions to list. This feature is team-space only.
- **Session caps and eviction.** Rejected by the human on 2026-09-09.
- **A rotate control.** Rotate has no caller in any client. Per-session revoke
  covers the device case, so a rotate button is not required by this work.
- **The pairing screen.** It belongs with the transport work, and it is where
  mandatory naming is enforced. Until it ships, every row reads `Unnamed device`
  and devices are told apart by last-seen time and the current-device marker.
- **A rename route.** Deliberately omitted. Naming happens once, at pairing.
  Adding rename would reintroduce the label lifecycle this design removes.
- **The desktop shell supplying its own machine name.** That is a
  `web/src-tauri/` change needing a Tauri rebuild. Not required for the list to
  work.
