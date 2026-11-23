# Design: a chat's artifacts, and sending comments from the viewer

Extend the chat header's chips to a list of the chat's artifacts. Extend the
viewer so it sends its own comments through the code that sends composer turns.
This file is deleted before merge. Its rules move to
`docs/specs/paper-artifacts-and-result-views.md` (viewer, pane) and
`docs/specs/api-web-and-desktop-projections.md` (routes, state).

## Problem

1. A chat shows its artifacts only as cards scattered through the transcript.
2. A saved viewer comment silently docks at the source chat's composer, and the
   viewer navigates there. Readers do not know how a comment reaches the agent.
   The viewer has two client paths for one job. It docks when the artifact's chat
   takes turns, and otherwise posts `/comments`.

## Decisions (answered by the user)

- The list is a side pane in the chat, opened from a header button with a count,
  as in the Claude Code desktop artifacts pane.
- The viewer sends its own comments; it no longer docks them or navigates.

## One resolver

An artifact's chat is `artifact_chat_id(artifact_reply_origin(store, artifact))`
(`src/rcp/storage/artifact_replies.py`). Admission already uses it to send an
artifact's comments. The pane uses the same function, so a row and its Send
agree on the conversation. Where the turn runs still depends on state. A chat
that takes turns gets an ordinary Work turn. A running orchestrator's artifacts
get the existing origin-bound edit.

| Origin | Chat |
| --- | --- |
| node or project chat turn | its `chat_id` |
| Auto-research reply chain | the origin's `graph_target.branch_id` |
| Experiment report whose parent has no chat | the latest `experiment_loop` task's chat |
| task outside any chat (ingestion) | none: the resolver raises, and the pane skips it |

`_artifact_source_hrefs` keeps building links. Unifying it with this resolver is
out of scope.

## A. The chat's artifacts pane

**Route.** `GET /api/projects/{p}/chats/{chat_id}/artifacts` lives in
`src/rcp/api/artifacts.py`. It returns `list[RunArtifactEntry]`, the episode
route's model: id, name, media type, `created_at`. It lists the project's
unexpired artifacts whose resolved chat is `chat_id`, newest first.

- **Scan.** The route loads `store.artifacts(project_id)` and filters in memory,
  as `run_artifacts` does.
  - Resolving one artifact reads its origin task, plus the episode and parent
    for reports.
  - Within one request, a memoizing `ReplyOriginReader` wrapper caches task,
    episode and `episode_tasks` reads, so the resolver itself stays unchanged.
    Artifacts whose origin cannot match the chat are skipped before resolving.
  - The cost is measured on a production copy before merge.
  - A project with a few hundred artifacts costs a few hundred indexed reads per
    refetch.
  - An artifact-to-chat column is the fallback if a measurement shows a cost.
- **Access.** The route filters like `saved_artifacts`:
  - a private chat of another member is refused with 404 (`private_chat_ids_except`);
  - each artifact passes `private_artifact_owner`.

**Web.**

- `chatArtifactsProfile` sits in `web/src/artifacts/artifactResources.ts` beside
  `runArtifactsProfile`. The project artifacts watermark invalidates it.
- `AgentChatView` adds a header chip, shown only when the count is above zero:
  - the Files icon and the count;
  - pressed while the pane is open;
  - "Artifacts" as its hover name.
- The pane is `web/src/chat/ChatArtifactsPane.tsx`:
  - a right column inside the conversation surface, 300px wide; at phone width
    it covers the chat;
  - each row shows the type icon and the name, with the time on hover;
  - a click calls `openArtifact`, and at phone width it also closes the pane;
  - the chip and Close close the pane, and the open state is not persisted.
- The chip never makes the header chips wrap.
- Scope: workspace project chats. The floating chat and Space threads keep
  their inline cards.

## B. Sending comments from the viewer

**Server: unchanged send path.** `comment_artifact` (`POST /artifacts/{a}/comments`)
stays the viewer's one endpoint, because only the server knows the artifact's
chat, branch, node and latest profile. It already does the following:

- calls `artifact_thread_context`, which checks that every thread targets this
  artifact;
- admits under one idempotency scope, `artifacts/{a}/comments`;
- inside that admission, starts either an ordinary Work turn in the chat
  (`artifact_send_chat_turn`, then `start_ordinary_task`, the same task start the
  composer route ends in) or, when the chat refuses human turns now, the
  origin-bound `admit_artifact_edit`.

The route keeps the `fresh_session` flag and its `fresh_session_required` error.
Only the edit branch reads the flag; the chat branch's context never sets it.
Invariant 10g holds: the retry is the existing explicit retry.

**Viewer.**

- The comment rail gains a footer with one button. It sends this artifact's
  threads that have `can_send`: open, in a chat the sender may use. That
  includes teammates' open threads, since members are one lab. Sending resolves
  them under the sender's name. The footer hides when none are sendable. A
  chat-less artifact's threads are never sendable today, and that stays.
- **Limits.** One send takes the oldest sendable threads that fit: at most
  `maxSelections` (50) threads, with rendered text within `maxChars`. Both
  values are already in the viewer config.
  - The button names the batch, for example "Send 12 of 30 to agent".
  - The rest stay open, and the footer offers them next.
  - The server's all-or-nothing refusal stays the backstop. On
    `comment_send_too_long`, the viewer drops the newest thread from the batch
    and shows the refusal, so the next click fits.
  - One click is one turn, never a split.
- Send posts the batch's thread ids to `/comments`, with one idempotency key per
  attempt and the existing `fresh_session` retry.
- A Send while the chat's agent is mid-turn is admitted, as a composer send is
  (admission refuses only archived or closed chats). A check covers it.
- Saving a comment only saves it.
- After a send, the sent threads resolve and leave the open list. The rail
  notice says "Sent" and links "Open chat", built from the thread's `chat_id`,
  so the reader knows where the turn runs. The viewer keeps posting
  `rcp-artifact-edit-started`. The parent marks the artifact as editing, and
  polling ends that mark.
- A refusal shows in the rail notice.

**Deleted.**

- `origin_chat_id`: `ArtifactViewerState`, `web/src/core/types.ts`, the
  `ViewerState` in `viewer.tsx`, and `tests/test_artifact_viewer_state.py`;
- the viewer's `dock`, which posts `rcp-artifact-comments-docked`;
- `artifactDockMessage` and its `ArtifactViewer` handler and navigation.

Inline artifact comments inside a chat keep docking at that chat's composer.

## Phases

These ship in one PR. The viewer's dock and `origin_chat_id` go in the same
commit as Send, so comments never lose their way out.

1. Pane server:
   - add the route with its access filters;
   - tests: chat, report, orchestrator, expired, private, chat-less.
2. Viewer Send:
   - add the footer;
   - remove the dock, navigation and `origin_chat_id`;
   - tests: `web/tests/commentsViewer.test.mjs`, `tests/test_artifact_viewer_state.py`.
3. Pane web:
   - add the resource, the chip and `ChatArtifactsPane`;
   - a browser test at 1040 and 390px.
4. Move the rules into the specs, then delete this file.

## Alternatives considered

- **Client posts to the composer route.** The viewer would need the branch, node,
  kind and latest profile, and two idempotency scopes would split one send.
  Rejected. The server route already ends in the composer's task start.
- **Extract a shared `send_chat_turn`.** Rejected. The two callers differ in
  admission scope, thread checks and the edit fallback, so the extraction would
  need flags.
- **Index on a chat column.** Deferred until measured.
- **Note field on Send.** Cut: the comments already carry the text.

## Risks

- **Scan cost on large projects.** It is bounded by project size, and the
  watermark cache keeps refetches rare.
- **The chat changes between the state read and Send.** For example, an
  orchestrator ends. The server resolves the route at POST time, so the turn
  always goes the current way.
- **Private chats.** The route refuses another member's private chat, and a test
  covers it.

## Checks

- **API:**
  - the route lists a chat's turn artifacts, its experiment report, and an
    orchestrator chain's artifacts under the branch id;
  - it skips chat-less and expired artifacts and other chats;
  - it refuses another member's private chat;
  - an old artifact with no `chat_id` resolves through its origin task.
- **Browser:**
  - the chip count matches the rows, and a row opens the viewer, at 1040 and
    390px;
  - Send posts once with the batch's thread ids, the threads resolve, and the
    notice links the chat;
  - with more than 50 threads, or text over the limit, the button names a
    batch that fits and the rest stay;
  - a Send while the chat is mid-turn is admitted;
  - a save does not navigate;
  - the footer hides with no sendable threads.
- **Served journey on a data copy:** send from an episode report viewer and see
  the turn in its chat.
