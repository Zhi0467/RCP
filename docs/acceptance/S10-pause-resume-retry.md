---
id: S10-pause-resume-retry
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_background_seed_can_pause_inspect_and_resume
  - tests/test_api.py::test_failed_background_seed_can_retry_without_native_session
  - tests/test_api.py::test_resumed_chat_rejects_a_mismatched_saved_stage
  - tests/test_conversation_retry.py
  - tests/test_api.py::test_server_shutdown_pauses_live_background_seed
  - web/tests/agentTasks.test.mjs
invariants: [8, 10b]
last_checked: 2026-09-01 — automated lifecycle coverage passed; the full browser journey was not redriven.
---

# Agent work is durable

Every agent run is background work. Closing the surface you launched it from
must not cancel it.

The lifecycle is heavily tested. What is not tested is the thing you would
actually notice: whether the run stays _visible_ once you navigate away.

## Setup

A temporary copy of the demo project. Fake agent: emits progress long enough to
be paused mid-run, then writes a valid patch.

## Drive

1. Start a **Work** turn.
2. While it is running, close the chat surface. Go to **Runs**.
3. Find the run there. Pause it.
4. Resume it. Let it finish.
5. Start a second run. Pause it, then **Retry** instead of resuming.
6. Reopen the project after the child attempt has finished.

## Assert — browser, partially covered

- `run_survived_surface_close` — closing the launch surface did not cancel it,
  and the UI says so rather than leaving you guessing
- `progress_visible_in_runs` — Runs remains the project-wide activity surface,
  while chat banners themselves remain scoped to Chats
- `paused_state_reported`
- `only_unresolved_paused_leaf_interrupts_project_entry` — once Resume or Retry
  creates a child attempt, its paused ancestor remains in Runs but is no longer
  presented as resumable when the project opens; selector regression and the
  live CRLP project entry passed on 2026-08-01
- `chats_indicator_on_completion`
- `app_stayed_usable_throughout`

## Assert — pytest, covered

- `resume_created_a_child_task` — parent → child, not one task changing state
- `parent_chain_intact` — `parent_operation_id` links back
- `mismatched_saved_stage_rejected`
- `reusable_native_session_stage_preserved_across_resume`
- `turn_mode_preserved_across_resume` — changing the composer after pause never
  changes the resumed task's captured mode or permission envelope
- `work_apply_uses_live_state_not_resume_ancestry` — if resumed Work produces a
  patch, RCP re-prepares bookkeeping and revalidates current graph state under
  the append lock; neither its original context revision nor an ancestor walk is
  an Apply gate
- `patches_appended == 1` across the whole chain, not one per attempt
- `retry_starts_clean`
- `shutdown_pauses_live_work`
- `refused_lifecycle_is_truthful` — when a single-task lifecycle status guard
  refuses an operation, the task history keeps its status, receipts, retained
  Patch output, and routed lifecycle notices unchanged, while appending exactly
  one truthful warning refusal event. The human `request_pause` contract still
  raises without an event, and bulk restart interruption stays quiet for
  terminal rows; only an applied completion may remove retained Patch output.
  The focused `tests/test_agent_task_lifecycle.py` matrix covers all 49
  source-status and lifecycle-operation pairs, plus missing-id and terminal
  progress-update behavior.

## Failure means

A run disappears, loses its saved native stage or captured mode, or appends the
same patch twice; `work_patch_correction` also fails if it repeats a completed
side effect. A Work patch fails if Resume ancestry or its original context
revision is treated as an Apply gate instead of validating live current state.
On the browser half, failure also means you close a panel and cannot tell whether
an expensive run is still alive.
