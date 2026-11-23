# Design: a chat's artifacts, and sending comments from the viewer

Extend the chat header's chips to a list of the chat's artifacts. Extend the
viewer so it sends its own comments through the code that sends composer turns.
This file is deleted before merge. Its rules move to
`docs/specs/paper-artifacts-and-result-views.md` (viewer, pane) and
`docs/specs/api-web-and-desktop-projections.md` (routes, state).

## Problem

1. A chat shows its artifacts only as cards scattered through the transcript.
2. A saved viewer comment silently docks at the source chat's composer and the
   viewer navigates there. Readers do not know how a comment reaches the agent.
   Behind that are two client paths (dock, or post `/comments`) and two server
   copies of "send threads as a chat turn": `start_agent_task` and
   `comment_artifact`'s `artifact_send_chat_turn` branch.

## Decisions (answered by the user)

- The list is a side pane in the chat, opened from a header button with a count,
  as in the Claude Code desktop artifacts pane.
- The viewer sends its own comments; it no longer docks them or navigates.

## One resolver

An artifact's chat is `artifact_chat_id(artifact_reply_origin(store, artifact))`.
This is the rule that comment threads, `admit_chat_artifact_comment` and
`_require_chat` already use. The pane, the viewer's Send and the send's chat
check all use it, so they cannot disagree. Its outcomes:

| Origin | Chat |
| --- | --- |
| node or project chat turn | its `chat_id` |
| Auto-research orchestrator or reply chain | the chain root's branch id |
| Experiment report | the latest `experiment_loop` task's chat |
| task outside any chat (ingestion) | none |

`_artifact_source_hrefs` keeps building links. Unifying it with this resolver is
out of scope.

## A. The chat's artifacts pane

**Route.** `GET /api/projects/{p}/chats/{chat_id}/artifacts` lives in
`src/rcp/api/artifacts.py` and returns `list[RunArtifactEntry]`, the episode
route's model. It contains the project's unexpired artifacts whose resolved chat
is `chat_id`, newest first.

- **Scan.** The route loads `store.artifacts(project_id)` and filters in memory,
  as `run_artifacts` does. Resolving the chat costs one task read per artifact.
  Projects hold hundreds of artifacts, not tens of thousands. Add an index only if
  a measurement shows a cost.
- **Access.** A private chat of another member is refused with 404, using the
  `private_chat_ids_except` check that `saved_artifacts` already makes.

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
  - each row shows the type icon and the name, with the version and time on hover;
  - the artifact open in the viewer is highlighted;
  - a click calls `openArtifact`;
  - the chip and Close close the pane, and the open state is not persisted.
- Scope: project chats and the floating chat. Space threads store their
  artifacts elsewhere and keep their inline cards.

## B. Sending comments from the viewer

**Server.** `comment_artifact` (`POST /artifacts/{a}/comments`) stays the
viewer's one endpoint, because only the server knows the artifact's chat, branch,
node and latest profile. Its chat branch stops building its own turn. Extract
`send_chat_turn(...)` from `start_agent_task` into `src/rcp/api/tasks.py`. It
covers four steps: `chat_turn_send`, client-request admission, starting the
ordinary task, and marking threads sent. `start_agent_task` (the composer) and
`comment_artifact` both call it. `artifact_send_chat_turn` shrinks to deriving
the kind, chat, node, branch and profile; it no longer builds an
`artifact_context`. When the artifact has no chat, or its chat refuses human turns
now, the route keeps the existing origin-bound `admit_artifact_edit`. The
`fresh_session` flag and its `fresh_session_required` error stay on this route
only, since the edit path is the only one that can require a fresh session.
Invariant 10g holds: the retry is the existing explicit retry, never a silent
fallback.

**Viewer.**

- The comment rail gains a footer with one button, "Send N to agent", for this
  artifact's open, unsent, sendable threads. It hides when N is 0.
- The send posts `thread_ids` with one idempotency key per attempt, as today.
- Saving a comment only saves it.
- After a send, the threads show as sent. The viewer keeps posting
  `rcp-artifact-edit-started` with the operation id. The parent marks the
  artifact as editing, and polling ends that mark when the turn finishes or a new
  version lands.
- A refusal shows in the rail notice.

**Deleted.**

- `origin_chat_id`: `ArtifactViewerState`, `web/src/core/types.ts`, the
  `ViewerState` in `viewer.tsx`, and `tests/test_artifact_viewer_state.py`;
- the viewer's `dock`, which posts `rcp-artifact-comments-docked`;
- `artifactDockMessage` and its `ArtifactViewer` handler and navigation;
- `artifact_send_chat_turn`'s body building.

Inline artifact comments inside a chat keep docking at that chat's composer.

## Phases

1. Server:
   - extract `send_chat_turn`;
   - add the chat artifacts route with its private-chat check;
   - simplify `comment_artifact` and drop `origin_chat_id`.
   - Tests: `tests/test_viewer_comment_sends.py`,
     `tests/test_artifact_edit_admission.py`, `tests/test_artifact_viewer_state.py`,
     and a new route test.
2. Viewer:
   - the Send footer;
   - remove the dock and navigation.
   - Tests: `web/tests/commentsViewer.test.mjs`, the artifact viewer browser test.
3. Pane:
   - the resource, the chip and `ChatArtifactsPane`;
   - a browser test at 1040 and 390px.
4. Specs, then delete this file.

## Alternatives considered

- **Client posts to the composer route.** The viewer would need the branch, node,
  kind and the chat's latest profile, and would duplicate server derivation.
  Rejected.
- **Index on a chat column.** Rejected until measured. The one resolver also
  covers reports and orchestrator chains, which a stored `chat_id` does not.
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
    orchestrator chain's artifacts under the root branch;
  - it excludes expired artifacts and other chats, and refuses another member's
    private chat;
  - a viewer send on a chat that takes turns starts a Work turn through
    `send_chat_turn` and marks the threads sent;
  - a read-only or chat-less artifact starts an `artifact_edit`;
  - an old artifact with no `chat_id` resolves through its origin task.
- **Browser:**
  - the chip count matches the rows, and a row opens the viewer, at 1040 and
    390px;
  - Send posts once with the thread ids, and the threads then show as sent;
  - a save does not navigate.
- **Served journey on a data copy:** send from an episode report viewer and see
  the turn in its chat.
