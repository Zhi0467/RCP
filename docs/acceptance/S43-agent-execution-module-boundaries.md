---
id: S43-agent-execution-module-boundaries
status: pending
tier: hermetic
driver: pytest
covered_by:
  - tests/test_api.py::test_background_seed_can_pause_inspect_and_resume
  - tests/test_api.py::test_invalid_patch_is_corrected_in_the_same_native_session
  - tests/test_api.py::test_node_chat_answers_without_writing_a_patch
  - tests/test_api.py::test_unauthorized_chat_patch_is_discarded_not_applied
  - tests/test_api.py::test_work_without_patch_succeeds_without_spending_a_revision
  - tests/test_api.py::test_invalid_work_patch_is_corrected_without_repeating_operational_work
  - tests/test_api.py::test_paper_coach_uses_agent_task_manager_and_result_shape
invariants: [4, 4b, 8, 9, 10, 10b, 10c, 10d, 10e, 11]
---

# Keep every agent surface intact while its execution code moves

RCP may move agent-run orchestration out of the HTTP application module without
changing what any agent surface can do, how durable work resumes, or what the API
and stored task record mean. The module boundary changes; the product contract
does not.

Seed and Refresh, Discuss, Work, graph repair, and paper coaching remain separate
policy entry points. Shared code may pump provider events, stage files, and record
receipts, but it never chooses authority from a generic mode, kind, or surface
switch.

## UI path (confirmed)

Confirmed by the human on 2026-08-01: there is no new UI path, control, label, or
response shape. Existing Seed, Refresh, Discuss, Work, Repair graph update,
Pause, Resume, Retry, artifact, and paper-coach paths behave exactly as their
existing scenarios say. This scenario is hermetic because the behavior that can
regress is backend dispatch and lifecycle policy, not browser state.

## Assertions

- `rcp.api.app.create_app` remains the public application entry point, and every
  existing route keeps its request, response, status, and event-stream contract.
- App construction, lifespan, middleware, and route declarations stay in
  `rcp.api.app`; agent-run orchestration lives in policy-specific modules.
- Seed and Refresh keep their mandatory graph-output, bounded correction,
  scratch retention, context-reuse, and Pause/Resume/Retry behavior.
- Discuss keeps writable conversation scratch but receives no graph contract,
  repository write authority, or canonical append path. A stray patch remains a
  discarded diagnostic receipt.
- Work keeps exact run-scope repository writes, optional graph reflection,
  independent operational and graph outcomes, and bounded patch-only correction
  without repeating operational work.
- Paper coaching remains read-only and retains its current session and task
  lifecycle.
- Local and remote stages, conversation projections, artifact discovery,
  native-session checkpoints, receipts, transcript writes, and cleanup rules are
  unchanged.
- Shared execution plumbing contains no `kind`, `is_chat`, `mode`, `surface`, or
  equivalent discriminator that decides policy.
- Private helpers moved out of `rcp.api.app` are imported from their owning
  modules by internal tests; `app.py` carries no compatibility re-exports.

## Deliberately unchanged

Provider commands and permissions, persistence schemas, patch schemas,
correction limits, stage names and retention, API payloads, event ordering,
frontend behavior, and canonical graph semantics.

## Failure means

The refactor changes an observable response or stored record, loses or repeats
work, alters recovery behavior, weakens an authority boundary, leaves execution
policy in the route module, or replaces explicit policy entry points with one
generic discriminator-driven runner.
