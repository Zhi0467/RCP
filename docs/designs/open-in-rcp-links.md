# Open in RCP links

Extend Copy reference into Copy link for team spaces. A member copies one
`https://` link, pastes it anywhere, and a teammate opens the item in the
desktop app or in the browser.

## Problem

- Copy reference writes the page's own URL (`referenceUrl`). In the desktop app
  that is a local relay origin, which opens nowhere else.
- Paper files, chats, replies, and comment threads have no copy control, and
  the desktop app registers no URL scheme.

## Settled decisions

1. **Team spaces only.** Personal spaces keep Copy reference and get no Copy link.
2. **Replace, then add.** In a team space, Copy link replaces Copy reference
   everywhere it appears, and is added to four more surfaces (see Copy surfaces).
3. **Opaque token.** The link is `https://<access_url>/open#l=<token>`. The
   server makes the token and keeps it in a `share_links` row. The URL holds no
   names or slugs. Links never expire. They die when the project leaves the space.
   The app hand-off is `rcp://open?space=<space_id>&host=<host>&l=<token>`.
   `host` is the access URL's host. It is not secret.
4. **One answer for every failure.** Readability is checked on every open. An
   unknown token, a non-member, and a deleted item all get the same 404.
5. **The /open page** uses the team sign-in card. It shows "Open in app",
   "Open here", and Remember (its note on hover). Once remembered, later links
   skip the page. A failed link shows the card with "Not available" and no
   buttons. Members are one lab, so the project name may show. A signed-out
   visitor sees no item details, and "Open here" goes through the team sign-in.
6. **Desktop.** The app owns the `rcp` scheme (RCP Dev owns `rcp-dev`). It
   matches the link's space to a saved team connection, queues the link until
   that session is ready, then navigates. With no match it shows an
   update-notice-style banner naming `host`, with "Add team space" (prefilled).
7. **Targets.** Comment threads use `#/projects/<p>/targets/<t>/comment/<thread_id>`
   from the comments PR (#400), which merges first. Chat replies get a new
   `turn=<operation_id>` route parameter.
8. **Reply Link** sits beside the reply's hover timestamp in the Agents view.
9. **Undo Remember.** The Appearance pane of Space Settings gains an "Open share
   links" row: In app, Here, or Ask. No new screen.
10. **In place.** A share link to the current team space, clicked inside RCP
   (desktop or browser), resolves in place. Links to other spaces go through
   their /open page and `rcp://`.

## Mechanism

### Backend

**Table.** `share_links` in SQLite, outside graph data and Patch logs.
```
share_links(token PK, project_id, kind, item_id, graph_target,
            created_by, created_at)
  UNIQUE (project_id, kind, item_id, graph_target)
```

- `kind` is `artifact`, `node`, `paper` (file id), `comment` (thread id),
  `chat`, or `reply` (operation id). `graph_target` is `main` or `branch:<id>`.
- The token is `secrets.token_urlsafe(16)`. Copying an item again reuses its row.
- It joins `_PROJECT_ID_TABLES` and `TRANSFER_EXCLUDED_PROJECT_TABLES`, so
  delete and transfer both drop the links.
- A new `storage/share_links.py` mixin owns it. Its
  `share_link_readable(user_id, row)` is the single readability check.

**Readability per kind** reuses each item's own route check.

| kind | readable when |
|---|---|
| `artifact` | `readable_artifact`, and `private_artifact_owner` is unset or the caller |
| `node` | the node is in the graph target's materialized graph |
| `paper` | the Paper file exists and is not deleted |
| `comment` | `readable_comment_target` for the thread (#400) |
| `chat` | the chat exists and `private_chat_owner` is unset or the caller |
| `reply` | the task exists and its chat passes the `chat` check |

The caller must also be a project member, and a branch target must exist.

**Routes** in a new `src/rcp/api/share_links.py`. Both answer 404 in a personal space.

- `POST /api/projects/{p}/share-links` with `{kind, item_id, graph_target}`
  needs membership and a readable item, else 404. It returns `{url}`. With no
  `team_access_url()` it returns 409, and the Web app keeps Copy reference.
- `POST /api/share-links/resolve` with `{token}`. It returns
  `{project_id, project_name, kind, title, link}`. `link` is the generic
  `#/projects/<p>/targets/<t>/<kind>/<id>` hash. Every failure returns one
  fixed 404 body. The token rides in the body so it stays out of access logs.
- `GET /open` serves `index.html` with `no-cache`. Today a navigation 404
  redirects to `/`, which would depend on the browser keeping the fragment.

### Web

- **Link kinds.** `NotificationItemKind` and `LINK_PATTERN` gain `chat` and
  `reply`. `chat` opens the chats view on that chat. `reply` reads
  `GET …/tasks/{op}`, finds its chat with `chatIdForTask`, and opens the
  chats view with `chat=<id>&turn=<op>`.
- **Turn.** `projectViewHash` gains `turnId` (`turn`). The transcript scrolls
  to that reply and flashes it, as `locateThread` does.
- **Copy.** `CopyReferenceButton` becomes the one control. In a team space with
  an access URL it shows the Link icon and copies the share link. The four new
  surfaces call the same `copyShareLink` helper. WebKit drops
  clipboard writes that happen after an `await`, so the helper passes a
  promise to `ClipboardItem` inside the click. Drag keeps the reference URL.
- **/open page.** `web/src/open/OpenLinkPage.tsx` mounts when the path is
  `/open`. Like `pairingLink.ts`, it reads `#l=` once at load and clears it.
  - Signed in, it calls resolve and shows kind, project, and title. Signed out,
    it shows no item details. "Open here" then goes through the team sign-in,
    which already continues to a link read at load.
  - "Open in app" sets `location` to the `rcp://open` URL. `space_id` comes
    from `/api/identity`, `host` from `location.host`.
  - "Open here" sets the hash to `link`, and the existing resolver takes over.
  - Inside the desktop shell the page never shows. It resolves and opens here.
- **Choice.** One `localStorage` key on the team origin holds `app`, `here`, or
  `ask` (default), wrapped in try/catch. Remember writes it, and a stored
  choice acts at load. The "Open share links" row in `AppearancePicker`
  edits the same key. It shows in a team space's browser only, because the
  desktop shell never shows the card.
- **In place.** `installLinkGuard` (`core/linkGuard.ts`) already catches every
  link click that would leave the app. It first checks for a share link: same
  origin in a browser, or the space's `access_url` (`/api/team/space`) in the
  desktop shell. It reads the token and runs the "Open here" path in place.
  Other share links keep today's path: a browser tab whose /open page hands
  off through `rcp://`. Saved connections hold no access host to match.

### Desktop

**Scheme: Info.plist plus `RunEvent::Opened`.** The desktop app is macOS only,
and Tauri 2.12 delivers opened URLs as `RunEvent::Opened { urls }`. The release
and dev builds already ship separate plists, so each gets a `CFBundleURLTypes`
entry, `rcp` or `rcp-dev`, with no new crate or permission.
`tauri-plugin-deep-link` would only add Linux and Windows, which RCP does not ship.

- A new `web/src-tauri/src/share_links.rs` parses `rcp://open?space=&l=`,
  rejects anything else, and never logs the token. It picks the saved team
  connection whose `expected_space_id` equals `space`.
- The link waits in a pending slot, like `pending_click` in `notifications.rs`.
  `notifications::open` moves to a shared `navigate_space(app, space, url)`.
  Once the session is established, it navigates the main window to
  `<local_origin>/open#l=<token>` and shows it.
- With no match, it emits `share-link-unpaired` with `host`. The Web app shows
  a banner styled like `UpdateNotice`. "Add team space" opens the dialog in
  `TeamSpaceGroups.tsx` with SSH target set to `host`, still editable. The link
  stays pending this app session and opens once that space is added.

## Copy surfaces

Mockups cover each surface at desktop width and 390px. Icon buttons carry no
text and a "Copy link" `title`. Menus get a "Copy link" item.

| surface | file | personal space | team space |
|---|---|---|---|
| Artifact viewer | `web/src/artifacts/ArtifactViewer.tsx` | Copy reference | Copy link |
| Artifact list | `web/src/artifacts/Artifacts.tsx` | Copy reference | Copy link |
| Node drawer | `web/src/graph/DetailDrawer.tsx` | Copy reference | Copy link |
| Paper file ⋯ menu | `web/src/paper/PaperFiles.tsx` | none | Copy link |
| Chat header menu | `web/src/chat/ChatHeaderMenu.tsx` | none | Copy link |
| Reply row | `web/src/agentWindow/AgentWindowTranscript.tsx` | none | Link beside `chat-line-time` |
| Comment thread card | `web/src/comments/react/CommentThreadCard.tsx` | none | Link icon |

## Readability and privacy

- The token rides in the fragment and the resolve body, never a request path.
- Every open rechecks membership and readability, so removing a member or
  deleting an item closes the link at once. Private items open only for their owner.
- The unpaired banner shows only `host`. The `rcp:` handler opens `/open` only
  on a saved team origin and never fetches `host`.

## Checks

**Backend** (`tests/test_share_links.py`): a stable token per item; 404 in a
personal space; resolve for each kind; byte-equal 404s for an unknown token, a
non-member, a deleted item, another member's private chat, and a removed
branch; project delete and transfer drop the rows; `/open` is `no-cache`.

**Web** (`web/tests/openLinks.test.mjs`, `notificationLinks.test.mjs`):
`chat`, `reply`, and `turn` routes; the /open card signed in, signed out, not
available, and per choice; the guard resolves same-space links in place and
opens others in a tab; Copy link and the preference row show only where they
apply. Then `npm --prefix web run build`.

**Desktop:** a Rust test for URL parsing and space matching, then the
[desktop checks](../desktop.md#test-the-desktop-app) on a packaged `RCP Dev.app`:
open a link cold, warm, from an unadded space, and inside the app. Quit other
RCP copies first, because Candidate shares the release scheme.

**Served drive:** a throwaway team server, two members, one project. Copy from
every surface, open as the other member in a browser at 390px and on desktop,
delete the item, and confirm "Not available". Check network, console, and logs.

## Slices

| slice | owns | check |
|---|---|---|
| A. Backend | `storage/share_links.py`, `api/share_links.py`, the `/open` route and router in `api/app.py`, `_PROJECT_ID_TABLES`, `transfer/records.py` | `uv run pytest -n0 tests/test_share_links.py` |
| B. Web links | `core/notificationLinks.ts`, `core/graphTarget.ts`, `core/linkGuard.ts`, the `turn` handling in the transcript, `open/OpenLinkPage.tsx`, the `AppearancePicker` row | `node --experimental-strip-types --test web/tests/openLinks.test.mjs web/tests/notificationLinks.test.mjs` |
| C. Copy surfaces | `core/CopyReferenceButton.tsx`, `core/shareLinks.ts`, the seven surface files | B's tests plus `npm --prefix web run build` |
| D. Desktop | `src-tauri/src/share_links.rs`, `notifications.rs`, `lib.rs`, both plists, the unpaired banner, the SSH target prefill | `cargo test` in `web/src-tauri`, then the packaged check |

A goes first and owns the `web/src/core/types.ts` edits; B, C, and D follow in
parallel. Before merge the rules move into `api-web-and-desktop-projections.md`
and `projects-spaces-and-operations.md`, and this file is deleted.
