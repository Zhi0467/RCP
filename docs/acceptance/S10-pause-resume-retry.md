---
id: S10-pause-resume-retry
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_background_seed_can_pause_inspect_and_resume
  - tests/test_api.py::test_failed_background_seed_can_retry_without_native_session
  - tests/test_api.py::test_resumed_chat_is_judged_against_the_revision_it_started_from
  - tests/test_api.py::test_resumed_chat_fails_closed_when_lineage_proof_is_missing
  - tests/test_api.py::test_resumed_chat_fails_closed_on_lineage_cycle
  - tests/test_api.py::test_resumed_chat_rejects_a_mismatched_saved_stage
  - tests/test_api.py::test_server_shutdown_pauses_live_background_seed
  - web/tests/agentTasks.test.mjs
invariants: [8, 10b]
---

# Agent work is durable

Every agent run is background work. Closing the surface you launched it from
must not cancel it.

The lifecycle is heavily tested. What is not tested is the thing you would
actually notice: whether the run stays *visible* once you navigate away.

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
- `freshness_used_first_attempt_revision` — the resumed turn is judged against
  the revision its *original* context was assembled from, recovered by walking
  the chain, never the revision it just re-assembled
- `fails_closed_on_missing_lineage`
- `fails_closed_on_lineage_cycle`
- `mismatched_saved_stage_rejected`
- `conversation_projection_preserved_across_resume`
- `turn_mode_preserved_across_resume` — changing the composer after pause never
  changes the resumed task's captured mode or permission envelope
- `patches_appended == 1` across the whole chain, not one per attempt
- `retry_starts_clean`
- `shutdown_pauses_live_work`

## Failure means

A resumed turn applies a patch built against a graph that has since moved — the
thing the freshness check exists to prevent. Or, on the untested half, you close
a panel and cannot tell whether an expensive run is still alive.
