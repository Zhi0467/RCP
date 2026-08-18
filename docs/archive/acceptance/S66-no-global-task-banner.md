---
id: S66-no-global-task-banner
status: implemented
tier: hermetic
driver: browser
covered_by: none
invariants: [10]
reported_by: human, 2026-08-04
---

# Agent tasks do not appear as a global banner

The project surface does not show a persistent task activity banner after an
agent task starts, pauses, or completes. Task state remains available in the
conversation, Runs, project History, and the task inspector.

## Drive

1. Start or select an agent task.
2. Navigate between Overview, Chats, Runs, and History.
3. Inspect the task from its task list entry.

## Assert

- No global task activity banner is rendered above the project content.
- Task status and actions remain available through the task-specific surfaces.
- No console, network, or server error occurs.
