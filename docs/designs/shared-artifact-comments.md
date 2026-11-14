# Shared comments

Extend the artifact viewer's comment tray into one shared comment module for
every commentable surface. A comment becomes a saved thread with an author.
Threads support replies, Resolve, and @mentions that reach the Inbox. Send to
agent sends a whole thread.

## Problem

Comments today are prompt inputs, not shared records.

- **Artifact viewer:** the tray keeps filed comments in the browser's
  `localStorage`, per artifact (`src/rcp/artifact_comment_panel.js`).
- **Inline artifacts and chat replies:** comments wait in the chat composer.
  They reach the turn as `artifact_context` or as message text
  (`admit_chat_artifact_comment`, `web/src/chat/answerComments`).
- **Paper preview:** comments wait in the Coach composer
  (`web/src/paper/previewComments.ts`).
- **Duplicated code:** the selection shape is defined three times and box
  drawing twice.

Space agent threads are private to one member and keep their composer comments.
This design covers project surfaces.

Nobody can leave a note that another member sees. There is no reply or resolve,
and PDFs take no comments. Only Bulletin posts can @mention a member.

## Settled decisions

These were decided with the human in chat:

1. **Saved threads.** A comment is a saved record with an author. Every member
   who can read the target sees it. A comment starts a thread, and members reply
   in it. Replies form one flat column.
2. **Resolve.** A member can Resolve a thread or reopen it.
3. **Send.** Send to agent sends the whole thread with its quote and place. The
   thread then moves into the Resolved fold with a "Sent to agent" mark. Anyone
   can reopen it.
4. **Mentions.** `@member` notifies that member and lists the thread in a new
   Inbox Mentions section. `@` also offers the target's agent. Picking the agent
   sends the thread.
5. **Re-finding.** Paper anchors re-find their quote after edits.
6. **Click to locate.** Clicking a thread scrolls to its anchor and flashes it.
   Clicking a pin opens its thread. Opening a mention runs the same path.
7. **Targets.** Every target is covered: artifacts wherever they show, Paper
   files, and chat replies. Text, box, and element selection work where the
   type can show them. Whole-target comments and PDFs work everywhere.

## Owners

| Owner | Holds |
|---|---|
| `src/rcp/comments/models.py` | `CommentAnchor`, `CommentTarget`, the API bodies, the media-type sets |
| `src/rcp/comments/render.py` | `render_anchor(anchor) -> str` and `render_thread(thread) -> str`, the only place a thread becomes agent text |
| `src/rcp/storage/comments.py` | `migrate_comments`, `CommentStoreMixin`, `comment_target_readable(user_id, kind, id)`, and `mark_threads_sent(ids, operation_id)` |
| `src/rcp/api/comments.py` | the comment routes |
| `web/src/comments/` | the rail, pins, composer, picker, shared box tool, `locateThread`, and the anchor type |

- The web anchor type is written by hand. A parity test checks it against
  `CommentAnchor.model_json_schema()`.
- `artifact_comment_panel.js` is deleted. The comment module also builds as a
  self-contained bundle, `comments.js` and `comments.css`, through a plugin
  that copies `web/build/artifactRenderer.mjs`.
  - `artifact_comments.comment_panel` reads the bundle from `web_dist_path()`
    and inlines it into the viewer shell, as `artifact_renderer.renderer_assets`
    does. The shell's `script-src 'unsafe-inline'` CSP stays unchanged.
  - A missing bundle raises the same "Rebuild the Web assets" error.
  - Python tests that open a viewer read the built bundle, as the Paper HTML
    preview tests read `artifact-renderer.js`. One test points
    `web_dist_path` at a missing folder to check the error.
  - The viewer, the inline frames, and Paper therefore run one module. Author
    HTML stays in its opaque nested frame (invariant 10e).

## Data model

Comments are project data stored in SQLite. They are not graph data. They never
touch Patch logs, authority, or Proposals (invariants 1 and 3).

```
comment_threads(
  thread_id PK, project_id, target_kind ('artifact'|'paper'|'reply'),
  target_id, graph_target, drawn_version NULL, anchor_json,
  state ('open'|'resolved'),
  created_by, created_at, closed_by NULL, closed_at NULL,
  sent_operation_id NULL, send_key NULL)
  INDEX (project_id, target_kind, target_id, state)
comment_posts(
  post_id PK, project_id, thread_id, author_id, text, created_at, edited_at NULL)
  INDEX (thread_id, created_at)
comment_mentions(
  project_id, post_id, thread_id, user_id, created_at, observed_at NULL,
  PRIMARY KEY(post_id, user_id))
  INDEX (project_id, user_id)
```

**Why these tables are separate from Bulletin's:**
- Bulletin prunes old threads.
- Bulletin lists by graph target.
- A comment thread has an anchor and a state.
- Mentions get their own table so Bulletin code needs no `comment:` branches.

**Ids:** ids are random hex. `graph_target` is the target's branch key, used to
build links.

**Targets:**

| `target_kind` | `target_id` | readable by | its agent |
|---|---|---|---|
| `artifact` | artifact id | `readable_artifact` and `private_artifact_owner` | the artifact's origin chat |
| `paper` | Paper file id | project members, while the file exists | the sender's open Coach chat |
| `reply` | the reply turn's operation id | `private_chat_owner` of its chat | that chat |

`comment_target_readable(user_id, kind, id)` is the single readability check. It
combines the column above for one user. Every comment route, the picker, the
mention parser, the Inbox query, the notifier, and the delivery guard call it.
The comment routes carry thread or post ids, which the path-based private check
in `require_project_membership` never sees, so they call it themselves and
answer 404.

**Deletion:**
- Deleting or expiring an artifact hard-deletes its threads, posts, and
  mentions in the same transaction. The artifacts spec says so.
- Deleting a post deletes that post and its mentions. Deleting the first post
  deletes the thread.
- Paper threads stay when their file is deleted, because Paper file ids survive
  delete and restore (CRDT identity). While the file is missing, its threads are
  hidden, its mentions are not listed or delivered, and the delivery check
  treats them as unreadable.
- Only the author can edit or delete a post.

**Table lists:**
- All three tables join `_PROJECT_ID_TABLES`, the project purge, the
  legacy-id merge, and `TRANSFER_EXCLUDED_PROJECT_TABLES`, as the Bulletin
  tables do.
- They are part of the database backup like every table.

**Limits** live in `limits.py`:
- `COMMENT_POST_MAX_CHARS = 2048`.
- `COMMENT_THREAD_MAX_CHARS = 6000`. Saving a post runs `render_thread` on the
  thread with the new post and refuses it with 422 if the result is longer, so
  every saved thread can be sent.
- A send carries at most `ARTIFACT_CONTEXT_MAX_SELECTIONS` threads. The typed
  text plus every rendered thread must fit `STEERING_MESSAGE_MAX_CHARS`, the
  32,000-character cap every send path already has. Otherwise the send returns
  422 and changes nothing. The rail's Send N checks the same total.

## Anchors

`CommentAnchor` is today's `ArtifactSelection` union plus three new kinds. No
existing field is renamed, so stored requests parse unchanged.

| kind | model | fields | used on |
|---|---|---|---|
| `whole` | new | none | every target |
| `text` | today's | `text`, `surrounding_text` | HTML, Markdown, plain text, Paper source and preview, replies |
| `box` | today's | `rect`, `viewport`, `elements` | HTML, raster images, SVG |
| `element` | new | `path` (CSS path), `label`, `text` | HTML |
| `pdf` | new | `page`, `rect` (fraction of the page), `text` | PDF artifacts, Paper PDF preview |

- **Location:** every kind gains optional `where`, a location string of at most
  256 characters that the browser computes. Paper's `pdfWhere` and
  `markdownWhere` move into `web/src/comments/`. A Markdown block picked in the
  preview is a `text` anchor holding the block's text, with `where` set to
  `path:12-18`.
- **`comment` field:** every kind keeps its `comment` field. It is empty on a
  stored thread anchor. At send time it holds `render_thread(thread)`, so its
  bound rises to `COMMENT_THREAD_MAX_CHARS`. Raising a maximum only widens
  validation.
- **Send-only field:** each selection gains optional `drawn_version`, filled
  by the server from the thread row. `/tasks` rejects a client-built
  `artifact_context` that carries selections; only threads produce them.
- **Media types:** `COMMENT_MEDIA_TYPES` maps each kind to the MIME types that
  admit it. `whole` admits every type in `ARTIFACT_MEDIA_TYPES` except
  `application/octet-stream`. `pdf` admits `application/pdf`. `text` admits
  HTML, `text/markdown`, and `text/plain`. `box` and `element` keep today's
  sets. `supports_comments`, `_checked_artifact`, the Space thread check
  (`agent_threads.py`), and the viewers' `selectable` flags (`api/artifacts.py`,
  `api/tasks.py`, `api/episode_routes.py`) all read this map.
- **Consumers updated:**
  - `artifact_comment_message`, which numbers every kind.
  - `prompts.py`, which renders each selection through `render_anchor` and
    replaces its `text`/`_box_lines` branch.
  - `api/chats.py` `_artifact_comments` and `ChatArtifactQuote.kind`, which
    become the five kinds. The quote is `text` for text, element, and PDF
    anchors, the element names for a box, and empty for whole.
  - The web mirror in `types.ts`.
- **Crops:**
  - Staging reads each distinct `drawn_version` once and crops a raster box from
    its own version.
  - A version whose bytes are gone gets no crop. The prompt says so and gives
    the place.
  - The crops are written beside the staged context, so a recovery replays the
    same files.
  - PDF boxes send the page, place, and text-layer quote, without pixels.
- **Compatibility:** an old-writer replay test covers stored
  `ArtifactContextRequest` rows.

**PDF artifacts:** the artifact viewer shows a PDF with pdf.js pages and a text
layer, reusing `PaperPdf`'s rendering, inside the comments bundle. Download
stays. That replaces the "system viewer or Download" rule and the "no viewer
URL" rule for PDFs.

**Element picking:** in comment mode, hovering outlines an element and shows its
name chip. A click picks it.

## Placing anchors

The browser places a pin for each open thread when the target opens and when
its version or text changes. Nothing re-finds on the server.

**By kind:**
- **`text`:** search for the quote inside its `surrounding_text`. If that fails,
  search for the quote alone.
- **`pdf`:** use the page and fraction on the same version. On a newer version,
  re-find the quote on that page.
- **`element`:** look up the CSS path.
- **HTML `box`:** place it on its first element's path plus `region`, so
  scrolling and resizing do not move it.
- **Image `box`:** use the fractions of the image.
- **Not found:** the thread stays in the rail without a pin, and hovering it
  says the anchor is gone. The prompt names the version the thread was drawn
  on, so the agent knows when the page has since changed.

**Paper:** a thread drawn in the source editor pins there, and a thread drawn in
the preview pins there. The rail lists all of the file's threads in both views.
Clicking a thread from the other view switches to it.

`locateThread(thread)` does the scroll and flash. Clicking a card and opening a
link both call it.

## Thread states

```
open ──Resolve, or Send admitted──▶ resolved ──Reopen / reply──▶ open
```

- A sent thread is a resolved thread with `sent_operation_id`. The rail shows
  "Sent to agent" when that id is set.
- Only `open` threads show pins, can be ticked, and are listed or delivered as
  mentions.
- The rail folds resolved threads under "Resolved · N".
- Reopening clears `closed_*`, `sent_operation_id`, and `send_key`. A reply
  reopens a resolved thread, so a repeat sent after a reply is a new send; its
  text differs and the replay path answers 409, which the client shows as a
  failed send.

## Send to agent

Threads ride on the send path their target already uses. No new turn path is
added.

| target | entry point | carries threads as |
|---|---|---|
| `reply`, `paper` | the composer's new turn (`/tasks` for `node_chat`, `project_chat`, `paper_coach`) | message text |
| any, while a turn runs | the composer's steer (`POST /tasks/{op}/steer`) | message text |
| one `artifact` of this chat | the composer's new turn | `artifact_context` selections |
| `artifact` in the viewer | `POST …/artifacts/{id}/comments` with `{thread_ids, fresh_session}` | `artifact_context` selections, through `artifact_send_chat_turn` or the revoking path |

**Wiring:**
- `start_agent_task` pops `comment_thread_ids` from the body before
  `_validated_task_request`. It is never part of a stored request.
- `SteerAgentTaskRequest` gains `comment_thread_ids`. Its `message` may be empty
  when threads are present. The steer handler renders the threads into the
  steer text before `begin_chat_steer` builds the final text. A steer carries text only, so artifact
  threads steered into a running turn arrive as rendered text, without crops.
- The clients' "no steer while artifact context is staged" checks stay. Sending
  threads never stages context on the client.

**Repeats:** every thread send carries a client key:
- a new turn uses the composer's existing `Idempotency-Key`, which thread sends
  always include;
- a steer uses its `message_id`;
- the viewer route uses a new `Idempotency-Key`, read the same way.

`mark_threads_sent` stores the key in `send_key`. A thread counts as sendable
when it is `open`, or when it is resolved with `send_key` equal to this request's
key. When the key matches, `render_thread` renders only the posts created at or
before `closed_at`, so a repeat produces the same text and the existing replay
paths match it:
- the `existing is not None` branch of `client_request_admission` for turns;
- `begin_chat_steer` with `_require_same_steer` for steers.

**Steps:**
1. **Check.** Load the threads. Each must be sendable, pass
   `comment_target_readable` for the sender, and belong to this chat or Paper.
   - A composer send may include threads on at most one artifact, and that
     artifact must belong to this chat, as `admit_chat_artifact_comment`
     requires.
   - Otherwise return 409.
   - The total must fit the 32,000-character cap (422).
2. **Render.** Use `render_anchor` and `render_thread`.
   - Message text: the rendered threads follow the typed text.
   - Artifact selections: the server builds `ArtifactContextRequest` with
     `source="task"`, `operation_id` set to the artifact's origin operation,
     and no `base_version`, as the viewer route does now.
3. **Admit.** Refusals keep each route's existing status codes and change no
   thread. A steer receipt that `begin_chat_steer` returns refused also counts
   as a refusal.
4. **Mark sent.** After `background_tasks.start`, or after a steer receipt that
   was accepted, and on the replay branches, call
   `mark_threads_sent(ids, operation_id, key)`:
   `UPDATE … SET state='resolved', sent_operation_id=?, send_key=?, closed_by=?, closed_at=? WHERE thread_id IN (…) AND state='open'`.
   - The operation id is the new task, or for a steer, the running task.
   - A thread someone closed in the meantime stays as that person left it.
   - If the process stops between admission and this update, the threads stay
     open and can be sent again. A duplicate only repeats a prompt; nothing
     reaches the graph.

**Who can send:**
- Sending needs project write access and a Patch-capable identity, as today's
  route does.
- Only the owner of a private chat can send to it. Each thread in the list
  response carries `can_send`. When it is false, the picker hides the agent row
  and Send is absent, and members @mention the owner instead.
- If an admitted turn later fails, its threads stay resolved with the mark. The
  failure shows in the chat, and Reopen returns the threads.

## Routes

All routes live in `src/rcp/api/comments.py`.

| route | admission |
|---|---|
| `GET /api/projects/{p}/comment-threads?target_kind&target_id` | project read; target readable |
| `GET …/comment-threads/{t}` | project read; target readable (used by links) |
| `POST …/comment-threads {target, anchor, drawn_version, text}` | project write; target readable |
| `POST …/comment-threads/{t}/posts {text}` | project write; readable |
| `PATCH` and `DELETE …/comment-posts/{post}` | author only |
| `POST …/comment-threads/{t}/resolve` and `/reopen` | project write; readable |
| `GET /api/projects/{p}/comment-mentions` | the caller's mentions |

A read-only member can read threads. They cannot post, resolve, or send, and
the agent row is not offered to them.

## Mentions and Inbox

- **Parsing:**
  - Posts parse `<@member:ID>` with a member-only variant of the Bulletin
    `_parse`, which refuses agent tokens.
  - A mention of a member who cannot read the target is dropped and stays plain
    text.
  - The picker lists only members who can read the target.
- **Agent row:** the picker has one agent row, "the target's agent". Picking it
  saves the post without a token, then sends that thread.
- **Mention rows:**
  - Saving a post writes `comment_mentions` rows for its mentions.
  - An edit adds rows for new mentions and deletes rows for removed ones. It
    never re-notifies a row that already exists.
  - The Inbox reads the rows, not the text.
- **Notifier:**
  - A new `_recheck_comment_mentions` and `observe_comment_mention` enqueue
    kind `comment_mention`, with `item_id` = thread id and `occurrence` = user
    id. A second mention of the same member in one thread therefore does not
    notify again.
  - Its own branch in `_guard_notification_delivery`, which `_lease_due` relies
    on, keeps a notification only while all of these hold:
    - a `comment_mentions` row for that thread and member exists;
    - the thread is `open`;
    - `comment_target_readable` passes for the device's user.
  - The kind is added to `NOTIFICATION_DEFAULTS`, `NotificationPreferencesUpdate`,
    `DesktopNotification.reason`, and `ProjectNotifications.tsx`.
- **Links:**
  - The notifier uses the generic link `#/projects/<p>/targets/<graph_target>/comment/<thread_id>`.
    `NotificationItemKind` and `LINK_PATTERN` gain `comment`.
  - App resolves the link once. It reads `GET …/comment-threads/{t}` and routes
    by target:
    - **Artifact:** the artifacts view, then opens the artifact.
    - **Paper:** the paper view with `paper_file=<id>`.
    - **Reply:** the chats view, on that chat and reply.
  - App then calls `locateThread`.
  - The Inbox Mentions rows carry the same link.
- **Inbox:** a Mentions section above Proposals lists the caller's mention rows
  whose thread is open and readable.

## Live updates

- `ResourceName` gains `comments`.
- `install_comment_resource_versions` in `storage/resource_versions.py` bumps
  it on insert, update, and delete in all three tables, keyed by
  `ROW.project_id`. It is installed beside the Bulletin trigger.
- An open rail re-reads only its own target's threads. The Inbox re-reads
  mentions.

## Interface

Mockups show the artifact rail, the composer and picker, the Inbox, chat
replies, and the phone sheet.

- **Rail:**
  - Thread cards with a checkbox, avatar, author, time, and quote.
  - Replies in one flat column under thin rules.
  - A reply box with Reply and Send to agent, then Resolve. Send to agent on an
    empty reply sends the thread as it stands, so each card has one send.
  - A footer reading "Send N to agent" once a thread is ticked.
  - The same module serves the viewer, expanded inline artifacts, and Paper
    (beside the source and the preview).
- **Pins:** red numbered pins at the end of each anchor. A selected card
  highlights its pin.
- **Composer:** a popover at the selection with Comment and Send to agent. `@`
  opens the picker with the agent row first.
- **Chat replies:**
  - A pin at the quote opens its thread as a popover.
  - A comment count in the chat header lists the chat's open threads.
  - Selecting text in a reply opens the shared composer.
- **Phone (390px):** the rail becomes a bottom sheet. Tap picks an element, and
  a long-press drag draws a box.
- **Invariants and shared tools:**
  - The opaque HTML frame still reports selections through the shell
    (invariant 10e).
  - One shared box tool serves HTML, images, PDF, and Paper.

## Retired

**Code:**
- **localStorage tray:** unsent tray comments stored in a browser are dropped,
  and the release notes say so. They were never shared.
- **Old shapes:** `PreviewDirectComment` and `PaperBoxSelect`.
- **Composer filing:** filing a comment into the chat or Coach composer.

**Spec rules replaced in `paper-artifacts-and-result-views.md`:**
- **Selection and comments:**
  - Browser-profile storage of filed comments.
  - "PDFs … refuse comments".
  - The tray's Add, Remove, and Send.
- **Commenting on the preview:**
  - Comments wait in the Coach composer and "travel as text with the next Coach
    message".
  - "The Coach has no whole-file comment".
  - Touch readers cannot comment on images or HTML pages.
- **Artifacts inside a reply:** "Add stages one artifact … in the composer".
- **Editing and versions:** "Composer comments are ordinary Work turns" becomes
  "sent threads are ordinary Work turns".

Each rule moves into its owning spec in this PR.

## Out of scope

- **Paper Coach change list:** a folded per-turn list of changed files with
  commentable deltas. It is a follow-up that reuses this module with a `diff`
  anchor.
- **Open in RCP links:** Copy link, the team server's `/open` page, and the
  desktop app's `rcp:` URL type. This is its own PR right after this one, and
  it reuses the `comment` link kind and resolver.

## Checks

**Python:**
- **Store:**
  - Thread lifecycle.
  - `mark_threads_sent` leaves a closed thread alone.
  - Delete cascades.
  - Paper threads hidden and restored with their file.
  - The render-based thread budget.
  - The three tables in every project table list.
- **Sends:**
  - A 409 for a non-open thread, and a 404 for every route and target kind when
    the caller is not the private owner.
  - A 422 when the total exceeds 32,000 characters.
  - A replayed send returns the original turn and marks the threads.
  - Two artifacts in one composer send are refused.
  - A refused admission leaves threads open.
  - Each target kind's entry point carries the rendered threads.
  - Steering threads into a running chat turn and a running Coach turn.
  - Owner-only sends to a private chat.
- **Mentions:**
  - Parsing drops non-readers and refuses agent tokens.
  - An edit adds and removes rows.
  - A `comment_mention` is delivered and then withdrawn when its thread closes.
- **Anchors:**
  - Replay of old stored `ArtifactContextRequest` rows, with an old-writer
    regression.
  - Admission of each kind for each media type.
  - `render_anchor` for every kind.
  - Crops from mixed versions, and from a missing version.

**Migration:** an upgrade fixture.

**Web:**
- Anchor re-finding: same version, edited text, a missing anchor, and HTML boxes
  after a scroll.
- The anchor parity test.
- Resolving the notification link for each target kind.

**Served drive** on throwaway data, with two members and a real agent:
1. Comment, reply, and mention. The other member gets the notification and the
   Inbox entry, and opening it lands on the flashed anchor.
2. Resolve and reopen.
3. Send two threads, and check the turn the agent receives.
4. Click to locate on HTML, an image, a PDF, Paper, and a chat reply.
5. Repeat at 390px.
