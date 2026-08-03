---
id: S54-paper-preview-and-resizable-node-detail
status: implemented
tier: hermetic
driver: browser
covered_by: web/tests/paperAndChatProfile.test.mjs, web/tests/detailResize.test.mjs, web/tests/floatingWindow.test.mjs
invariants: []
reported_by: human, 2026-08-03
last_passed: 2026-08-03
---

# Read authored Markdown and resize the node being inspected

Confirmed by the human through
[`docs/handoff-ui-fixes-and-graph-skills.md`](../handoff-ui-fixes-and-graph-skills.md)
on 2026-08-03.

The paper editor has one pane with Write and Preview modes. Preview renders the
current unsaved Markdown through the same renderer as chat; switching modes does
not alter saving, conflict resolution, word count, or canonical state.

Node details use the existing S45 floating-window model: **Dock** minimizes the
window into the project-level node strip and restoring it returns the same
floating detail. The floating detail's width and height are resizable. Its saved
size survives minimizing and restoring, as well as closing and reopening the
node, and its size and position remain reachable when the viewport changes.
Node detail is scoped to research inspection and closes when the human enters
Chats instead of covering the conversation. Chat windows are not resizable.

## UI path

1. Open Paper, type `## Methods`, switch to Preview, return to Write, and reload
   the project.
2. Open a node in Research. Resize its floating window, minimize it into the
   node strip, restore it, close it, and reopen it.
3. Resize the browser around the floating node, then enter Chats.

## Assert

- Preview renders `## Methods` as a heading from the unsaved editor content.
- Write/Preview state is remembered per project.
- The word count, save path, sync state, and conflict controls are unchanged.
- The floating node detail has a keyboard-accessible resize handle and respects
  minimum and viewport-constrained maximum sizes.
- The stored size survives minimize/restore and close/reopen for that project.
- The floating window's size and position remain inside the viewport after the
  window or viewport is resized.
- The floating chat has no resize handle.
- Entering Chats dismisses the node detail without changing or cancelling its
  conversation.
- No console, network, or server error occurs.

## Failure means

Markdown remains source-only, preview becomes a second document, resizing loses
the window, or node inspection obscures the first-class chat workspace.
