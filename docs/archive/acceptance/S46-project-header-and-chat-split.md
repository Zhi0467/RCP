---
id: S46-project-header-and-chat-split
status: implemented
tier: hermetic
driver: browser
covered_by: [web/tests/chatLayout.test.mjs]
invariants: []
last_passed: 2026-08-02 — browser-driven against the remote demo project;
  verified the line-free 24px strip, full-width collapsed chat, two-way Command+B
  toggle, optional usage 404 isolation, and the three-second error notice timeout
---

# Fold the project utilities and resize the Chats split

## UI path (confirmed 2026-08-02)

Open a project and enter **Chats**. The project header does not repeat the
project name. The project-panel navigation places an icon-only fold control
immediately to the left of Overview: it shows an upward arrow while the
utility header is expanded and a downward arrow while the header is folded.
Folding removes the utility header completely while leaving navigation
available; expanding it brings back the Sync, Ask, history, and refresh
controls. The folded state is remembered for that project.

The Chats conversation list and selected conversation are separated by a
visible, keyboard-accessible resize handle. Dragging the handle widens or
narrows the conversation list without changing the selected chat. The width is
bounded, and the chosen width is remembered for that project. A compact arrow
sits inside a line-free separator strip. The whole strip highlights on hover.
The left arrow folds the conversation list away while the strip remains against
the left edge as a wide click target; the right arrow restores the list and its
previously chosen width. Command+B performs the same toggle.
The selected chat surface does not repeat the conversation title above the
transcript; the left conversation list remains the title-bearing navigation.

Bottom operation notices, including errors, disappear after three seconds and
remain manually dismissible while visible. An unavailable optional usage
endpoint does not turn an otherwise successful project refresh into a Not Found
notice.

## Drive

1. Open a project and confirm its name is absent from the project header and the
   upward fold arrow sits immediately before Overview.
2. Fold the project utility header from the navigation and confirm the banner
   disappears, the arrow turns downward, and the current view remains usable;
   expand it and confirm the project actions return.
3. Open **Chats**, drag the divider between the conversation list and chat
   surface in both directions, and confirm the selected conversation remains
   selected while the chat surface gains or loses width.
4. Hover the divider and confirm the full strip highlights without a vertical
   line crossing the arrow. Click the left arrow and confirm the conversation
   list folds away, the strip remains fully clickable against the left edge,
   the chat surface expands, and the arrow becomes a right arrow. Click it and
   confirm the list returns at its prior width.
5. Reload or leave and re-enter the project. Confirm the header fold state,
   Chats list width, and conversation-list fold state are retained.
6. Press Command+B twice and confirm it folds and restores the list without
   changing the selected chat. Use the resize handle with keyboard arrows and
   confirm it remains usable without a pointer.
7. Refresh the project while the usage endpoint is unavailable, then trigger an
   ordinary success and error notice. Confirm project refresh remains usable and
   each bottom notice disappears after three seconds without a click.

## Assert

- `project_name_is_not_repeated_in_the_project_header`
- `project_header_can_fold_without_hiding_project_navigation`
- `project_header_toggle_sits_before_overview_and_changes_direction`
- `project_header_actions_return_when_expanded`
- `project_header_fold_state_is_project_scoped_and_persistent`
- `chat_list_divider_is_draggable`
- `chat_list_width_is_bounded`
- `resizing_preserves_selected_conversation`
- `chat_list_width_is_project_scoped_and_persistent`
- `chat_list_can_fold_and_expand_from_the_divider`
- `chat_list_divider_has_no_line_through_its_arrow`
- `chat_list_divider_strip_highlights_on_hover`
- `collapsed_chat_list_leaves_a_clickable_separator_strip`
- `chat_list_fold_state_is_project_scoped_and_persistent`
- `command_b_toggles_the_chat_list`
- `chat_list_divider_is_keyboard_accessible`
- `workspace_chat_does_not_repeat_the_conversation_title`
- `optional_usage_404_does_not_fail_project_refresh`
- `bottom_notices_auto_dismiss_after_three_seconds`
- `no_console_or_application_request_errors`

## Failure means

The utility header continues to consume chat space, the project name adds
repeated text, resizing selects or loses a conversation, or the layout cannot
be adjusted with a keyboard.
