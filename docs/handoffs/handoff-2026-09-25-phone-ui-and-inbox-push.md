# The phone works, and it tells you when you are needed

Date: 2026-09-25
Status: design draft, not yet confirmed by the human. Nothing is implemented.

The plan is three parts on one pull request, in this order:

1. **Style foundation.** Size tokens, one set of breakpoints, one file per view.
   Desktop looks the same afterwards.
2. **Phone pass.** The screens a notification opens work at phone width:
   Inbox, the Proposal and Decision flow, episode cards, and chat.
3. **Inbox push.** A phone or browser that opts in gets a Web Push notification
   when something new needs this member.

Close this handoff when all three have landed, the checks below pass, and one
real iPhone on a team space has received a push and opened the item it names.

## Why

Two reports from the human, 2026-09-25:

- On a phone over the tailnet, tapping the chat box zooms the page. The page
  then scrolls sideways and its edge is not fixed.
- There is no way to learn that an episode or Proposal needs you without
  opening RCP and looking.

Orca, a multi-agent desktop tool, makes "your phone pings when a decision is
needed" its main selling point. RCP already decides exactly what needs a human.
It only lacks a way to say so.

## Part 1: style foundation

### What the code does today

- One stylesheet, `web/src/styles.css`, of about 13,800 lines. Two small
  component files and one theme file sit beside it.
- Colors are already tokens on `:root`. Themes swap them. This part works.
- Sizes are not tokens. There are 474 hard-coded `font-size` values in 26
  distinct sizes, from 7px to 38px, plus a few `em` and `rem` values.
- The body font is 14px and every input inherits it. iOS Safari zooms any
  focused field under 16px. That is the zoom in the report.
- There are 25 media queries at 9 different widths (560, 640, 680, 700, 720,
  820, 920, 1180px). There is no single definition of "phone".
- JavaScript checks width in one hook, `useNarrowViewport` (560px), used by
  Chats and the graph view.
- Phone and desktop are the same pages. The browser picks the layout by its own
  width. How the device connected (tailnet, SSH tunnel, loopback) never matters.

### The change

1. **Size tokens.** A type scale (`--text-2xs` … `--text-2xl`) and a spacing
   scale on `:root`. Every hard-coded `font-size` uses a token. Spacing moves to
   tokens where a rule is already being touched; a full spacing sweep is not
   required.
2. **One breakpoint set.** Phone is `max-width: 560px`, the width the spec and
   the hook already use. Tablet is `max-width: 920px`. The other seven widths
   fold into these two. CSS cannot read a variable inside `@media`, so the two
   widths are written once in CSS and once in `useNarrowViewport`, with a test
   that they agree.
3. **Phone mode is token overrides.** One `@media` block at the phone width
   resets the tokens. Inputs, textareas, and selects are at least 16px there.
   Per-view phone layout rules live beside that view's rules.
4. **One file per view.** `styles.css` splits into a base file (tokens, reset,
   shared controls) and one file per destination (Inbox, Chats, graph, runs,
   paper, settings, …). The split moves rules without editing them, so review
   can check it by diff.

### Settled

- Do not block pinch zoom in the viewport tag. The 16px input rule fixes the
  zoom without taking zoom away from people who need it.
- No device detection, user-agent checks, or separate phone build.

## Part 2: phone pass

Scope is the screens a notification can open, plus the chat composer from the
report:

- Inbox, with Proposal approve/reject and Decision choice.
- Runs, with episode cards and their recovery controls.
- Chat, including the composer and the Discuss/Work switch.
- Settings, only for the new notification control.

At 375px wide, each of these must:

- show no horizontal page scroll;
- not zoom when a field is focused;
- keep primary actions reachable without horizontal panning; and
- keep tap targets at least 44px tall.

The graph views, paper editor, terminals, and setup wizards are out of scope.
They must not get worse; they do not have to get better.

## Part 3: Inbox push

### What counts as "needs you"

Push uses the facts RCP already projects. It adds no new judgment.

- A new pending Proposal, a Decision awaiting choice, or an open asserted
  Blocker. These are the members of `project_graph_attention` in
  `src/rcp/core/attention.py`, the same set the Inbox shows.
- An episode that reaches `needs_action`. The text is its `blocked_reason` lead
  sentence (`reauthorize`, `sign_in`, `repeated_failure`), the same sentence the
  card shows.

Nothing else notifies: no graph updates, finished tasks, or mail. An item
notifies once, when it first enters attention. It does not notify again while it
stays there.

### Who receives it

Every current member of the project, on every device where that member turned
notifications on. A removed member's subscriptions are deleted with their
membership.

### How it is sent

- Standard Web Push (RFC 8030/8291) with VAPID keys. RCP creates its key pair
  on first use and keeps it in the data directory. It needs no Apple or Google
  account.
- The server makes one outgoing HTTPS request to the push service named in the
  subscription (Apple, Google, or Mozilla). It needs no public address and no
  open port. Every RCP server already has outgoing internet for the providers.
- The payload is encrypted to the device. It carries only the project name, the
  one-line reason, and a link into the item. No research content.
- A dead subscription (HTTP 404 or 410 from the push service) is deleted. Other
  failures are logged and retried on the next pass. A failed push never blocks
  a transition.

### Where the trigger lives

Attention is a projection computed on read, so no single write path "creates"
it. One reconciler owns push:

- A background loop, every 30 seconds per project, computes the current
  attention set: graph attention ids plus `needs_action` episode ids.
- It compares that set with a stored `push_notified` table and sends for new
  members only. Sending and recording happen together per item, so a restart
  never re-sends and never skips.
- An item that leaves attention is removed from the table, so a Decision
  reopened later notifies again.
- When the loop first runs on an existing space, it records the current set
  without sending. Upgrading must not fire a burst of old items.

This keeps push out of transitions, episode code, and routes. It is one owner
with one table.

### The browser side

- A web app manifest and a service worker, served from the site root. The
  service worker only receives pushes and opens the link on tap. It does not
  cache the app.
- **Turn on notifications** in Settings, per device. Its states: on, off,
  blocked by the browser, and "not installed" on iPhone.
- On iPhone, Web Push works only for a site added to the Home Screen
  (iOS 16.4+). When RCP sees an iPhone browser tab that is not installed, the
  control explains the three steps (Share → Add to Home Screen → open from the
  icon) instead of offering a button that silently fails.
- The macOS desktop app's web view does not support Web Push. The control is
  hidden there. Native desktop notifications are out of scope.

### Scope

Push is for team spaces, reached from a phone through the tailnet's HTTPS front.
A personal space on loopback over plain HTTP cannot register a service worker,
so the control is hidden there.

## Open questions for the human

1. **Font scale snapping.** Folding 26 sizes into about 8 tokens moves some
   desktop text by 1px (for example 9.5px becomes 10px). Recommendation: accept
   these 1px shifts and review them by screenshot. The alternative, one token
   per current size, keeps pixels identical but leaves the scale as messy as
   today.
2. **Push library.** Web Push needs P-256 key exchange and AES-GCM, which RCP
   does not have today. Recommendation: add `pywebpush`, the standard library
   (it brings `cryptography`, `requests`, `http-ece`, `py-vapid`). The
   alternative is about 100 lines of our own on `cryptography` alone, using the
   existing `httpx`. Either way the wheel and the desktop PyInstaller build grow.

## Checks

- **Desktop unchanged.** Before and after screenshots of each destination at
  1440px, in both themes, reviewed by the human. Differences are only the
  accepted 1px shifts.
- **Phone.** A browser test at 375px on Inbox, Runs, and Chat asserts no
  horizontal page scroll and focused-input font size of at least 16px.
- **Breakpoints agree.** A test that `useNarrowViewport` and the CSS phone width
  are the same value.
- **Reconciler.** Python tests: a new attention item sends once; a restart does
  not re-send; an item that leaves and returns sends again; the first run on an
  existing space sends nothing; a 410 deletes the subscription; a removed member
  receives nothing.
- **Payload.** A test that the encrypted payload holds only the project name,
  reason, and link fields.
- **Real device.** One iPhone added to the Home Screen on a team space receives
  a push for a real Proposal and opens it.

## Owners

- `web/src/styles.css` and its split files, `web/src/hooks/useNarrowViewport.ts`:
  parts 1 and 2.
- A new `src/rcp/push/` package: VAPID keys, subscriptions, the reconciler, and
  sending. It reads `project_graph_attention` and the episode projection; it
  writes only its own tables.
- A new subscription route under `src/rcp/api/`, checked for membership like
  every other project route.
- `web/public/` for the manifest and service worker, and a Settings control.
- `docs/specs/interface-and-visual-design.md` for the breakpoints and phone
  rules, and `docs/specs/api-web-and-desktop-projections.md` for push, updated
  in the same pull request.
