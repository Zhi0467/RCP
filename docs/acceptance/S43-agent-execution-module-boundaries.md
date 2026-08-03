---
id: S43-agent-execution-module-boundaries
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_api.py::test_background_seed_can_pause_inspect_and_resume
  - tests/test_api.py::test_invalid_patch_is_corrected_in_the_same_native_session
  - tests/test_api.py::test_node_chat_answers_without_writing_a_patch
  - tests/test_api.py::test_unauthorized_chat_patch_is_discarded_not_applied
  - tests/test_api.py::test_work_without_patch_succeeds_without_spending_a_revision
  - tests/test_api.py::test_invalid_work_patch_is_corrected_without_repeating_operational_work
  - tests/test_api.py::test_public_task_request_cannot_select_watcher_or_control_authority
  - tests/test_api.py::test_experiment_work_stamps_and_applies_the_bound_control_patch
  - tests/test_api.py::test_watch_handoff_correction_arms_once_and_wake_is_not_a_user_turn
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
  scratch-only generic patch correction, scratch retention, context-reuse, and
  Pause/Resume/Retry behavior.
- Discuss keeps writable conversation scratch but receives no graph contract,
  repository write authority, or canonical append path. A stray patch remains a
  discarded diagnostic receipt.
- Work keeps unrestricted tooling and repository access, optional semantic graph
  reflection, independent operational and graph outcomes, and bounded
  `work_patch_correction` in the same native session with the same Work
  permissions. Only the correction instruction changes; completed operational
  side effects are not repeated.
- Work stages the validator client and serves its bounded workspace
  request/response exchange locally or through the existing SSH transport.
  Self-check and Apply share live in-process semantic validation; Apply
  re-prepares bookkeeping under the append lock without a context-revision pin
  or Resume-ancestor walk.
- Experiment-loop and Watcher continuations retain the Work authority captured
  by RCP: public task payloads cannot forge it, a bound control patch is stamped
  and validated as such, and a watcher wake is not persisted as a user turn.
- Paper coaching remains read-only and retains its current session and task
  lifecycle.
- Local and remote stages, artifact discovery, native-session checkpoints,
  receipts, chat-history writes, and cleanup rules remain policy-owned. Chat
  stages do not project or validate prior transcripts; Seed/Refresh stages keep
  their independent source-slice handling.
- Shared execution plumbing contains no `kind`, `is_chat`, `mode`, `surface`, or
  equivalent discriminator that decides policy.
- Private helpers moved out of `rcp.api.app` are imported from their owning
  modules by internal tests; `app.py` carries no compatibility re-exports.

## Deliberately unchanged

Discuss permissions, Seed/Refresh and their generic scratch-only correction,
paper-coach permissions, preview sandboxing, persistence schemas, correction
limits, API payloads, frontend behavior, and canonical graph semantics outside
the D29 Work-validator amendment.

## Failure means

The refactor changes an observable response or stored record, loses or repeats
work, alters recovery behavior, weakens an authority boundary, leaves execution
policy in the route module, or replaces explicit policy entry points with one
generic discriminator-driven runner.
