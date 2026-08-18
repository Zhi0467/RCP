---
id: S49-chat-node-reference-links
status: implemented
tier: hermetic
driver: browser
covered_by:
  - web/tests/chatMarkdown.test.mjs
  - browser 2026-08-02 — isolated fixture verified workspace and floating links,
    inline-code link styling, fenced/unresolved preservation, click activation,
    Enter activation, and clean browser diagnostics
last_passed: 2026-08-02
invariants: [10, 10d, 11]
---

# Open an existing node from a chat answer

An agent answer may mention a graph node using its canonical id without
following a special response schema. RCP makes an existing node id in ordinary
Markdown prose or inline Markdown code clickable when it can resolve that id in
the current project graph. An unresolved lookalike remains ordinary text, and
node-like text in fenced code remains code.

## UI path

- Open a project with nodes such as `exp/two-update-matched-trajectory` and
  `hyp/search-restores-future-learning`.
- Open **Chats**, select a conversation, and show an assistant answer that
  mentions both existing ids, a nonexistent id such as `exp/not-in-this-graph`,
  and one of the ids inside inline or fenced code.
- The existing ids in ordinary prose and inline code render as links. The
  nonexistent id and the fenced-code occurrence remain visible but are not
  links.
- Click an existing node link. Its modeless node-detail window opens for that
  exact graph node while the chat remains open and its transcript stays in
  place. Press Tab until the link is focused, then press Enter; the same node
  detail opens without navigating away from chat.
- Repeat from a floating node chat. The same link opens the node detail without
  closing or replacing the chat window.

## Assert

- `existing_node_ids_are_best_effort_links`
- `unresolved_node_lookalikes_are_plain_text`
- `fenced_code_occurrences_are_not_rewritten`
- `existing_markdown_links_and_formatting_remain_intact`
- `clicking_a_node_reference_opens_the_exact_detail_window`
- `floating_chat_remains_open_after_node_reference_activation`
- `tab_then_enter_opens_the_node_detail`
- `no_console_or_server_errors`

## Failure means

The agent must emit a new rigid link schema, a false-positive node link opens
the wrong node, an unresolved reference becomes a dead link, a fenced-code
occurrence is rewritten, or activating a reference navigates away from the chat
instead of opening the existing node detail.
