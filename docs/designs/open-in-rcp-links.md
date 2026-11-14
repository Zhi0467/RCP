# Open in RCP links

Extend Copy reference into Copy link for team spaces. A member copies one
`https://` link, pastes it anywhere, and a teammate opens the item in the
desktop app or in the browser.

## Problem

- Copy reference writes the page's own URL plus an item hash (`referenceUrl`).
  In the desktop app that is a local relay origin, which opens nowhere else.
- Paper files, chats, replies, and comment threads have no copy control.
- The desktop app registers no URL scheme. A browser cannot hand an item to it.

## Settled decisions

1. **Team spaces only.** Personal spaces keep Copy reference and get no Copy link.
2. **Replace, then add.** In a team space, Copy link replaces Copy reference in
   the artifact viewer, the artifact list, and the node drawer. It is added to
   the Paper file ⋯ menu, the chat header menu, the reply action row, and the
   comment thread card.
3. **Opaque token.** The link is `https://<access_url>/open#l=<token>`. The
   server makes the token and keeps it in a `share_links` row. The URL holds no
   names or slugs. Links never expire. They die when the project leaves the space.
4. **One answer for every failure.** Readability is checked on every open. An
   unknown token, a non-member, and a deleted item all get the same 404.
5. **The /open page** uses the team sign-in card. It shows "Open in app",
   "Open here", and Remember. Remember explains itself on hover. Once
   remembered, later links skip the page. A failed link shows the same card with
   "Not available" and no buttons. Members are one lab, so the project name may show.
6. **Desktop.** The app owns the `rcp` scheme (RCP Dev owns `rcp-dev`). It
   matches the link's space to a saved team connection, queues the link until
   that session is ready, then navigates. With no matching connection it shows
   an update-notice-style banner with "Add team space" and no item details.
7. **Targets.** Comment threads use the `comment` link from the comments PR
   (#400), which merges first. Chat replies get a new `turn=<operation_id>`
   route parameter.
8. **Reply Link button** sits beside the reply's hover timestamp in the Agents
   view, so a mouse can reach it.

## Mechanism

### Backend

**Table.** `share_links` in SQLite. It is not graph data and touches no Patch log.

```
share_links(token PK, project_id, kind, item_id, graph_target,
            created_by, created_at)
  UNIQUE (project_id, kind, item_id, graph_target)
```

- `kind` is `artifact`, `node`, `paper`, `comment`, `chat`, or `reply`.
  `item_id` is the artifact id, node id, Paper file id, thread id, chat id, or
  the reply's operation id. `graph_target` is `main` or `branch:<id>`.
- The token is `secrets.token_urlsafe(16)`. Copying an item again reuses its row.
- The table joins `_PROJECT_ID_TABLES` (project delete removes its links) and
  `TRANSFER_EXCLUDED_PROJECT_TABLES` (a transferred project leaves its links behind).
- Owner: a new `src/rcp/storage/share_links.py` mixin, whose
  `share_link_readable(user_id, row)` is the single readability check.

**Readability per kind.** Each kind reuses the check its own route uses.

| kind | readable when |
|---|---|
| `artifact` | `readable_artifact`, and `private_artifact_owner` is unset or the caller |
| `node` | the node is in the graph target's materialized graph |
| `paper` | the Paper file exists and is not deleted |
| `comment` | `readable_comment_target` for the thread (#400) |
| `chat` | the chat exists and `private_chat_owner` is unset or the caller |
| `reply` | the task exists and its chat passes the `chat` check |

The caller must be a project member, and a branch target must still exist.

**Routes** in a new `src/rcp/api/share_links.py`. Both answer 404 in a personal space.

- `POST /api/projects/{p}/share-links` with `{kind, item_id, graph_target}`.
  It needs project membership and a readable item, else 404. It returns
  `{url}`. A team server with no `team_access_url()` returns 409, and the Web
  app keeps Copy reference there.
- `POST /api/share-links/resolve` with `{token}`. It returns
  `{project_id, project_name, kind, title, link}`. `link` is the generic
  `#/projects/<p>/targets/<t>/<kind>/<id>` hash. Every failure returns one
  fixed 404 body. The token rides in the body so it stays out of access logs.
- `GET /open` serves `index.html` with `Cache-Control: no-cache`. Today a
  navigation 404 redirects to `/`, which would depend on the browser keeping
  the fragment. The fragment itself never reaches the server.

### Web

- **Link kinds.** `NotificationItemKind` and `LINK_PATTERN` gain `chat` and
  `reply`. `chat` opens the chats view on that chat. `reply` reads
  `GET …/tasks/{op}`, finds its chat with `chatIdForTask`, and opens the
  chats view with `chat=<id>&turn=<op>`.
- **Turn parameter.** `projectViewHash` gains `turnId`, written as `turn`. The
  transcript scrolls to that reply and flashes it, as `locateThread` does.
- **Copy.** `CopyReferenceButton` becomes the one control. In a team space with
  an access URL it shows the Link icon, posts the share link, and copies it.
  The four new surfaces call the same `copyShareLink` helper. WebKit drops
  clipboard writes that happen after an `await`, so the helper passes a
  promise to `ClipboardItem` inside the click. Drag (`setReferenceDrag`) still
  carries the reference URL.
- **/open page.** `web/src/open/OpenLinkPage.tsx` mounts when the path is
  `/open`. Like `pairingLink.ts`, it reads `#l=` once at module load, then
  clears the fragment.
  - Signed in: it calls resolve and shows kind, project, and title.
  - Not signed in: it shows the card with no item details. "Open here" goes
    through the team sign-in, which already continues to a link read at load.
  - "Open in app" sets `location` to `rcp://open?space=<space_id>&l=<token>`.
    `space_id` comes from `/api/identity`.
  - "Open here" sets the hash to `link`, and the existing resolver takes over.
  - Remember stores the choice in `localStorage` on the team origin, wrapped in
    try/catch. A remembered choice acts at load and skips the card.
  - Inside the desktop shell the page never shows. It resolves and opens here.

### Desktop

**Scheme registration: Info.plist plus `RunEvent::Opened`.** The desktop app
is macOS only (`docs/desktop.md`), and Tauri 2.12 delivers opened URLs as
`RunEvent::Opened { urls }`. The release and dev builds already ship separate
plists, so each gets its own `CFBundleURLTypes` entry: `rcp` and `rcp-dev`.
No crate, JS permission, or config is added. `tauri-plugin-deep-link` would
only add Linux and Windows registration, which RCP does not ship.

- A new `web/src-tauri/src/share_links.rs` parses `rcp://open?space=&l=`,
  rejects anything else, and never logs the token. It picks the saved team
  connection whose `expected_space_id` equals `space`.
- The link waits in a pending slot, like `pending_click` in `notifications.rs`.
  `notifications::open` moves to a shared `navigate_space(app, space, url)`.
  Once the session is established, it navigates the main window to
  `<local_origin>/open#l=<token>` and shows it.
- With no matching connection, it emits `share-link-unpaired` to the main
  window. The Web app shows a banner styled like `UpdateNotice`. "Add team
  space" opens the existing dialog in `TeamSpaceGroups.tsx`. The link stays
  pending for this app session and opens once that space is added.

## Copy surfaces

Approved mockups cover each surface, the /open card, and the unpaired banner
at desktop width and 390px. Icon buttons carry no text; their `title` says
"Copy link". Menus get a "Copy link" item.

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

- A link carries only a random token in its fragment, so the page request
  never sends it. Resolve takes it in a POST body.
- Every open rechecks membership and readability. Removing a member or deleting
  an item closes the link at once.
- The 404 is identical for every failure. Private chats and artifacts open
  only for their owner, as everywhere else.
- The unpaired banner shows no item details. The app has not checked the link.
- The `rcp:` handler only opens `/open` on a saved team origin. It never fetches
  a host named in the URL.

## Checks

**Backend** (`tests/test_share_links.py`): a stable token per item; 404 in a
personal space; resolve for each kind; byte-equal 404s for an unknown token, a
non-member, a deleted item, another member's private chat, and a removed
branch; project delete and transfer drop the rows; `/open` is `no-cache`.

**Web** (`web/tests/openLinks.test.mjs`, `notificationLinks.test.mjs`):
- `chat` and `reply` links route, and `turn` round-trips through the hash;
- the /open card states: signed in, signed out, not available, remembered;
- Copy link shows only in team spaces with an access URL.
- Run `npm --prefix web run build`.

**Desktop:** a Rust unit test for URL parsing and space matching. Then follow
the [desktop checks](../desktop.md#test-the-desktop-app) on a packaged
`RCP Dev.app` (`rcp-dev`). Open a link cold and warm, and from a space not yet
added. Quit other RCP copies first, because Candidate shares the release scheme.

**Served drive:** a throwaway team server with two members and one project.
Copy a link from every surface, open it as the other member in a browser at
390px and on desktop, then delete the item and confirm "Not available". Check
network, console, and server logs.

## Slices

| slice | owns | check |
|---|---|---|
| A. Backend | `storage/share_links.py`, `api/share_links.py`, the `/open` route and router in `api/app.py`, `_PROJECT_ID_TABLES`, `transfer/records.py` | `uv run pytest -n0 tests/test_share_links.py` |
| B. Web links | `core/notificationLinks.ts`, `core/graphTarget.ts`, the `turn` handling in the transcript, `open/OpenLinkPage.tsx` | `node --experimental-strip-types --test web/tests/openLinks.test.mjs web/tests/notificationLinks.test.mjs` |
| C. Copy surfaces | `core/CopyReferenceButton.tsx`, `core/shareLinks.ts`, the seven surface files | B's tests plus `npm --prefix web run build` |
| D. Desktop | `src-tauri/src/share_links.rs`, `notifications.rs`, `lib.rs`, both plists, the unpaired banner | `cargo test` in `web/src-tauri`, then the packaged check |

A goes first and owns the shared `web/src/core/types.ts` edits. B, C, and D
run in parallel after it. The rules move into `api-web-and-desktop-projections.md`
(links, /open, desktop shell) and `projects-spaces-and-operations.md`
(`share_links` lifecycle) before merge, and this file is deleted.
