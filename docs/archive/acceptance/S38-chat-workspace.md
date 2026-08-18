---
id: S38-chat-workspace
status: implemented
tier: hermetic
driver: browser
covered_by:
  - tests/test_api.py::test_node_chat_returns_as_task_then_persists_result_and_transcript
  - tests/test_api.py::test_project_chat_persists_project_scoped_transcript
  - tests/test_api.py::test_chat_history_is_paginated_from_full_canonical_transcripts
  - tests/test_api.py::test_chat_history_reports_remote_refresh_failure_as_unavailable
  - tests/test_api.py::test_new_chat_turn_refuses_resumable_paused_attempt
  - tests/test_storage.py::test_resumable_paused_chat_query_is_exact_and_child_attempt_resolves_it
  - web/tests/chatWorkspace.test.mjs
  - web/tests/chatApi.test.mjs
  - web/tests/floatingWindow.test.mjs
  - browser 2026-07-31 — draggable detail/chat, docking, draft preservation,
    project Ask routing, canonical transcript rendering, and isolated synthetic
    active-to-unread banner routing
  - browser 2026-08-07 — project Ask started a selected empty conversation while
    preserving the existing project conversation; Chats navigation created no
    additional conversation
  - browser 2026-08-07 — New session in both presentations: the Chats list went
    from seven conversations to eight with the new empty one selected, and the
    floating chat swapped to a fresh conversation while its window and the node
    detail stayed open
last_passed: 2026-08-07 — drove the changed project Ask path against an existing
  project conversation. The new empty conversation was selected, the existing
  one remained available, returning through Chats kept the selection without
  creating another conversation, and browser/server logs had no application
  errors. The New session control was driven on the same run in both the Chats
  workspace and the floating window, with no console or non-200 API traffic.
invariants: [8, 10, 10b, 10c, 11]
---

# Keep the node in view while its conversation continues

Opening a node is inspection, not navigation away from the research surface.
The node detail and its chat are independent draggable windows, so a human can
keep both visible and arrange them around the underlying project view.

Closing the floating chat minimizes its presentation; it never closes the
conversation or cancels agent work. Every conversation for the current project
is available in a first-class **Chats** panel with a compact conversation list
on the left and the selected transcript and composer on the right.

## UI path

Confirmed on 2026-08-02; project Ask behavior reconfirmed on 2026-08-07.

- **Chats** is a project navigation panel immediately to the right of
  **Settings**, equal in hierarchy to the other project panels.
- The panel follows the simple two-column shape of a normal chat window: current
  project's conversations on the left, selected conversation on the right. It
  does not reproduce Codex-specific global sidebar, account, repository, model,
  or window controls.
- The list and selected transcript come from canonical `.research/chat/`
  history, not the recent bounded task feed; long answers and conversations
  older than the latest task page remain available.
- The conversation list reads one canonical summary page at a time. Opening
  Chats never downloads every page preemptively; the explicit **Load more**
  control requests the next page and appends it without changing the selection.
  A single request to open Chats refreshes remote canonical state at most once,
  and opening one conversation does not parse every unrelated transcript.
- Opening or reopening a conversation positions the transcript at its latest
  turn. If the human scrolls up to read older turns, later transcript updates
  respect that reading position until the human returns to the bottom.
- Clicking a graph node opens its detail as a modeless draggable window. The
  project beneath remains visible and interactive.
- **Ask** from that node opens its conversation in a second draggable window at
  a non-overlapping initial position. The node detail stays open. Either window
  can be moved independently by its header and remains reachable inside the
  viewport.
- Closing the floating chat minimizes it into **Chats**. Its transcript,
  composer state, task status, and result remain available there.
- Discuss/Work mode is the same conversation state in the floating chat and the
  Chats workspace. Minimizing or docking never resets the next-turn mode, and
  historical turn badges render identically in both presentations.
- The chat's context row carries the provider name, a **New session** control,
  and the repository scope picker, all one height on one baseline. The picker
  names its own current state — **All repositories**, or *n* of *m* — with no
  label above it and no eyebrow inside it.
- **New session** starts a fresh conversation of the same kind and node and moves
  the current surface to it: in the floating chat it replaces that window's
  conversation while the node detail stays open, and in **Chats** it becomes the
  selected conversation. The previous conversation remains in the list,
  unchanged. It is the node-chat counterpart to what project-header **Ask**
  already does, so both surfaces can reach a fresh conversation.
- The project-header **Ask** control always opens **Chats** with a fresh, empty
  project conversation. Existing project conversations remain unchanged in the
  list. Entering **Chats** through project navigation still returns to the
  selected or otherwise relevant existing conversation. **Ask** does not create
  a separate chat surface.
- Chat task banners are scoped to **Chats**. They are never rendered over
  Overview, Inbox, Research, Runs, Paper, or Settings. Outside Chats,
  the **Chats** navigation item carries the activity or unread-result indicator.
  Entering Chats makes the chat task banner visible and selects the exact
  conversation it describes instead of opening only the generic task
  inspector. The indicator clears only after that conversation is viewed.
- Non-chat task banners keep their existing project-wide behavior.
- Historical completed or failed chats remain in **Chats** and the **Agent
  tasks** drawer, never in **Runs**, and do not become unsolicited project-entry
  notifications.

Deliberately not possible: opening chat by destroying the node detail, closing
chat by cancelling its run, mixing conversations from another project into the
list, or routing a notification only to a generic task inspector.

## Drive

1. Open a project's Research view and click a node.
2. Drag the node detail away from its starting position and continue using the
   project beneath it.
3. Press **Ask** in the node detail. Move the new chat independently and verify
   that both windows remain visible.
4. Submit a slow chat turn, then close the floating chat and navigate elsewhere.
5. Confirm that no chat task banner is visible there, then use the activity
   indicator on **Chats** to return and inspect live progress on the right.
6. Leave Chats again, let the turn complete, and confirm that the unread result
   changes the Chats indicator without rendering a chat banner on that panel.
   Enter Chats and open the completed conversation.
7. Press **New session** in the floating chat, then again in **Chats**. Confirm
   each starts an empty conversation of the same kind and node, that the floating
   window and node detail stay open, and that the previous conversation is still
   in the list.
8. Select an existing project conversation, leave **Chats**, then use the
   project-header **Ask** control. Confirm it opens a fresh, empty project
   conversation while the existing one remains in the list. Send a
   project-level message, leave, and return through **Chats**; confirm navigation
   returns to the selected conversation rather than creating another one. Then
   switch between the project and node conversations.
9. Reopen the project and inspect an older completed conversation. Confirm the
   transcript starts at its latest turn rather than at the beginning. Scroll up
   and confirm the view stays there while the chat surface otherwise updates.
10. Use a fixture with more than one page of conversations. Confirm the first
   page is usable before requesting the next, then load the next page without
   losing the selected conversation.

## Assert

- `node_detail_is_modeless_and_draggable`
- `node_detail_and_chat_are_visible_together`
- `detail_and_chat_move_independently`
- `floating_windows_remain_reachable`
- `closing_chat_does_not_cancel_the_run`
- `chats_panel_is_immediately_after_settings`
- `conversation_list_is_current_project_only`
- `conversation_list_loads_one_page_at_a_time`
- `loading_another_page_preserves_selection`
- `one_chat_request_performs_at_most_one_remote_refresh`
- `opening_one_chat_does_not_parse_unrelated_transcripts`
- `reopening_conversation_starts_at_latest_turn`
- `manual_transcript_scroll_is_respected`
- `minimized_chat_keeps_transcript_composer_and_status`
- `new_session_starts_fresh_conversation_on_both_surfaces`
- `new_session_preserves_the_previous_conversation`
- `new_session_keeps_the_floating_window_and_node_detail_open`
- `chat_context_row_has_no_label_above_the_scope_picker`
- `chat_context_row_controls_share_one_height_and_baseline`
- `project_ask_starts_fresh_project_conversation`
- `project_ask_preserves_existing_project_conversations`
- `chats_navigation_does_not_create_conversation`
- `chat_banner_is_visible_only_inside_chats`
- `chats_indicator_reports_activity_and_unread_result`
- `opening_chats_routes_to_exact_conversation`
- `viewing_conversation_clears_its_indicator`
- `non_chat_banner_behavior_is_unchanged`
- `historical_chat_does_not_interrupt_project_entry`
- `chat_tasks_do_not_appear_in_runs`
- `no_console_or_server_errors`

## Failure means

Asking about a node hides the evidence being discussed, a long-running answer
becomes hard to find after its window closes, or a completion notification
returns the human to the wrong project or conversation.
