# The phone works, and Chats shows every agent at a glance

Date: 2026-09-25
Status: design confirmed by the human on 2026-09-25 against a rendered mockup of
the Agents panel, then revised after an xhigh design review whose findings were
verified against the code. Implementation started 2026-09-25 on this pull
request; nothing has landed yet.

One pull request, in this order:

1. **Style foundation.** Size tokens, one phone width and one tablet width,
   one file per view. Desktop looks the same afterwards, apart from accepted
   1px text shifts.
2. **Phone pass.** Inbox, Runs, Chat, and Settings work at phone width.
3. **Agents panel.** The Chats list becomes an agent-hub list: grouped by what
   needs you, with a state and a reason on every row.

The Agents panel includes one backend change: the agent task list keeps every
chat whose latest turn still needs a human, so Needs you never loses one.

Inbox push is a separate, later pull request with its own handoff,
`handoff-2026-09-25-inbox-push.md`.

Close this handoff when all three parts have landed and the checks below pass,
including one real iPhone on a team space.

## Why

Two reports from the human, 2026-09-25:

- On a phone over the tailnet, tapping the chat box zooms the page. The page
  then scrolls sideways and its edge is not fixed.
- The Chats sidebar does not show what the agents are doing. Orca and Herdr,
  two multi-agent tools, both make the sidebar the dashboard: every agent has a
  state, and the ones that need you sort first.

## Part 1: style foundation

### What the code does today

- `web/src/styles.css` is 13,818 lines. It imports Tailwind, then
  `themes/aqua.css`. `AppearancePicker.css` and `WorktreeControls.css` are
  imported by their components. Aqua changes geometry and shadows as well as
  colors, and wins by selector specificity, so its import position and
  specificity must stay.
- Colors are already tokens on `:root`. Themes swap them.
- Sizes are not tokens. `styles.css` has 493 `font-size` declarations: 474 in
  literal pixels across 25 distinct sizes, the rest in `em`, `rem`, `clamp()`,
  or `font` shorthands.
- The body font is 14px. Most inputs inherit it; the chat composer sets 14px
  explicitly and some fields set smaller sizes. iOS Safari zooms any focused
  field under 16px. That is the zoom in the report.
- There are 20 width media queries at eight widths (560, 640, 680, 700, 720,
  820, 920, 1180px) plus five reduced-motion queries. `AppearancePicker.css`
  adds one more at 680px.
- JavaScript checks width in one hook, `useNarrowViewport` (560px), used by
  Chats and the graph view.
- Phone and desktop are the same pages. The browser picks the layout by its own
  width. How the device connected never matters.

### The change

1. **Mechanical split first, as its own commit.** Rules for one destination
   are interleaved across the whole file (chat rules alone span about 6,000
   lines), so regrouping them would reorder the cascade. The split instead cuts
   `styles.css` into twelve contiguous files under `web/src/styles/`, named for
   what each mostly holds, and imports them in the original order after
   Tailwind and aqua. Done when the built CSS is byte-identical to `main`'s.
2. **Size tokens, as a second commit.** A type scale and a spacing scale on
   `:root`. The 25 pixel sizes map to about eight tokens through an explicit
   old-to-new table in the pull request. Shorthands and fluid `clamp()` values
   are converted by hand or left with a note. Spacing moves to tokens only
   where a rule is already touched.
3. **Named breakpoints.** Phone is `max-width: 560px` (`PHONE_MAX_WIDTH_PX`
   in `useNarrowViewport`), tablet is `max-width: 920px`. Every other width
   query (640, 680, 700, 720, 820, 1180px) sits inside one component's rules
   and exists for that component's content, so none is folded: moving any of
   them would change layout at the widths in between. A test fails when a
   stylesheet adds a width outside this set.
4. **One phone overrides file.** `web/src/styles/13-phone.css` is imported
   last, so its rules win over same-specificity component rules. It holds the
   16px field rule (with `!important`, because component rules set smaller
   sizes at varying specificity) and the 44px tap-target rule. The project
   header keeps its packed one-row layout: its icon buttons gain height, not
   width.

### Settled

- Pinch zoom stays enabled. The 16px field rule fixes the zoom without it.
- No user-agent checks or separate phone build. Where behavior depends on the
  device, check the capability (width, pointer, display mode), not the brand.
- Accepted 1px desktop text shifts from snapping, reviewed by screenshot.

## Part 2: phone pass

At phone width these screens must work: Inbox with Proposal and Decision
choices and Sync, Runs with episode cards and recovery dialogs, Chat including
the Agents panel and composer, Settings, and the pairing and login screens.

For each:

- no horizontal page scroll, and no control clipped by `overflow: hidden`;
- no zoom when a field is focused;
- primary actions reachable without horizontal panning, including with the
  on-screen keyboard open; and
- tap targets at least 44px tall.

The graph views, paper editor, terminals, and setup wizards are out of scope.
They must not get worse.

## Part 3: Agents panel

The Chats list (`ChatsWorkspace.tsx`) becomes the panel in the confirmed
mockup. Runs is not touched. The composer and the conversation body stay as
they are today.

### Rows

- **Groups, in order:** Needs you, Working, Recent. Counts in each group
  header.
- **State icon per row**, from the latest task of the conversation:
  - needs you: `failed`, or `awaiting_human` (sign-in, reauthorization);
  - paused: `paused`;
  - working: `active` or `queued`;
  - unread result: finished and in the existing unread set (title in bold);
  - idle: everything else.
- **Reason line** on a needs-you or paused row: the backend's `status_label`,
  verbatim. `failure_kind` is sealed in the web client, and chat tasks export no
  recommendation, so a specific reason such as "Sign in to Codex" needs a new
  backend field and is not part of this pull request.
- **Title** wraps to two lines instead of truncating.
- **Meta line:** provider (`runtime_label`) · Discuss or Work
  (`request.mode`) · repository. While working, the live phase and elapsed
  time replace the repository.
- **Right edge:** time since last activity, or "live" while working.
- The `Node`/`Project` label moves from the row to the conversation header.

### Top of the panel

- Search over loaded conversation titles.
- Filter chips: All, Needs you, Working, with counts.
- The existing New chat action.

### Conversation header

One line with the title, and one line with provider, model, mode, repository
and worktree, and node or project scope. A colored banner shows the backend label
and Resume or Retry when the task's `can_resume` or `can_retry` offers it.

### Data

Rows come from fields the web client already has: chat summaries and the
agent task list, merged by `groupChatConversations`. Both are bounded. Chat
summaries are paged by recency, and `GET /api/projects/{id}/tasks` returns only
the newest `AGENT_TASK_LIST_DEFAULT_LIMIT` (20) tasks of any kind. A chat whose
last turn failed or paused therefore drops out of Needs you once 20 newer tasks
exist.

**Backend fix, in scope.** The task list also returns, beyond that limit, the
latest task of every chat whose latest task is failed, paused, or awaiting a
human. A failed turn followed by a later turn in the same chat is not included.
The extra rows are bounded by a new limit in `limits.py`. The store query in
`src/rcp/storage/agent_tasks.py` owns the selection, so every consumer of the
list sees the same set.

At phone width the panel keeps today's behavior: it starts closed behind the
Chats disclosure and closes after choosing a conversation. Wider views keep the
resizable list and its saved width and collapse preference.

## Checks

- **Desktop unchanged.** Before and after screenshots of each destination at
  1440px, and on both sides of each kept threshold (1180, 920, 700, 640px), in
  all four theme and color-mode combinations. Differences are only the accepted
  1px shifts and the new Chats panel.
- **Split is mechanical.** After the first commit, `npm --prefix web run build`
  emits CSS byte-identical to `main`'s.
- **Phone.** A browser test at 375px on Inbox, Runs, Chat, and Settings asserts
  no horizontal page scroll, focused-field font size of at least 16px, and 44px
  tap targets on primary actions. Assertions use geometry, ids, and roles,
  never wording.
- **Existing mobile behavior.** `web/tests/mobileWorkspace.browser.test.mjs`
  keeps passing: the disclosure and the saved width.
- **Breakpoints stay named.** `web/tests/breakpoints.test.mjs` checks every
  width query against the phone width, the tablet width, and the listed
  content thresholds.
- **Agents panel.** A web test that each task-state combination lands in the
  right group with the right icon, and that filters and search narrow the
  list. Assertions use state and ids, not wording.
- **Old needs-you chats.** A Python test that a chat whose latest turn failed
  is still returned after more than 20 newer tasks, that a failed turn followed
  by a later turn in the same chat is not, and that the extra rows respect
  their limit.
- **Real device.** On an iPhone over the tailnet: focus the composer without
  zoom, move between Inbox, Runs, and Chats, approve a Proposal through Sync,
  and open a conversation from the Agents panel.

## Owners

- `web/src/styles.css` and its split files, the two component stylesheets,
  `web/src/themes/aqua.css`, and `web/src/hooks/useNarrowViewport.ts`.
- `web/src/views/ChatsWorkspace.tsx`, `web/src/chatWorkspace.ts`, and the
  conversation header, for the Agents panel.
- `src/rcp/storage/agent_tasks.py` and `src/rcp/limits.py`, for the task-list
  fix; `docs/specs/api-web-and-desktop-projections.md` for its contract.
- `docs/specs/interface-and-visual-design.md` for the breakpoints, phone rules,
  and the Agents panel, updated in the same pull request.
