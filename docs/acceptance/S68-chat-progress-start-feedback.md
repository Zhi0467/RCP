---
id: S68-chat-progress-start-feedback
status: implemented
tier: hermetic
driver: browser
covered_by: none
invariants: []
---

# Chat progress appears immediately under a sent message

When a human sends a chat message, the message and its task-start feedback
appear together. The feedback does not wait for the server's task-start request
to return; once the task record arrives, the temporary feedback becomes the
normal live progress display.

## Drive

1. Open a node or project chat with a ready provider.
2. Send a message while observing the conversation area.

## Assert

- The sent message appears immediately.
- A running progress bar appears directly below that message in the same UI
  update, even while task creation is still pending.
- The temporary bar is replaced by the server-backed task progress without a
  second message or a visible gap.
