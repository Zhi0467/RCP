---
id: S65-concurrent-agent-tasks
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_storage.py::test_multiple_active_agent_tasks_can_share_a_project
last_passed: 2026-08-04 — backend and web checks plus browser chat verification
invariants: [8, 10, 10b]
reported_by: human, 2026-08-04
---

# Multiple agent tasks can run at once

RCP does not serialize unrelated agent tasks at the project boundary. A user
can start work in one conversation while another conversation, Paper, Seed,
Refresh, or a watcher task is still running. Each conversation still refuses
to start a new turn while its own resumable pause or active turn needs action.
Canonical graph publication remains protected by its existing append lock.

## Drive

1. Start an agent task in one conversation.
2. Open a different conversation and send another turn while the first task is
   still active.
3. Confirm both tasks remain visible and continue independently in Chats and
   Runs.
4. Return to the first conversation. Only its own active or paused turn affects
   its composer.

## Assert

- `multiple_active_agent_tasks_can_share_a_project`
- `unrelated_chat_composers_do_not_show_a_global_active_task_block`
- `canonical_append_lock_still_serializes_graph_publication`

## Boundary

The project may still refuse deletion while any task is active. A paused turn
continues to require Resume or Retry before another turn in that same
conversation, and experiment control loops retain their node-specific guards.
