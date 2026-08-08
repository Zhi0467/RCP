---
id: S51-live-agent-patch-validation
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_agent_schema.py::test_agent_output_schema_omits_nested_rcp_bookkeeping
  - tests/test_agent_schema.py::test_rcp_prepares_canonical_metadata_and_proposal_bookkeeping
  - tests/test_staged_graph_validation.py
  - tests/test_patch_validator.py
  - tests/test_graph_patch_validator.py::test_seed_attempt_stages_and_serves_live_validator_before_final_append
  - tests/test_prompts.py::test_graph_contract_keeps_fanout_and_points_to_payload_files
  - tests/test_prompts.py::test_work_patch_correction_keeps_work_access_and_live_validator_contract
  - tests/test_prompts.py::test_discuss_contract_has_no_patch_path_or_schema_and_no_project_authority
  - tests/test_prompts.py::test_paper_and_continuation_contracts_only_point_to_dynamic_content
  - tests/test_transport.py::test_remote_stage_workspace_mailbox_round_trip_is_atomic
  - tests/test_transport.py::test_remote_stage_workspace_operations_fail_closed
  - tests/test_launcher.py::test_codex_work_bypasses_approvals_and_sandbox
  - tests/test_launcher.py::test_claude_work_bypasses_permissions
  - tests/test_api.py::test_invalid_work_patch_is_corrected_without_repeating_operational_work
  - tests/test_api.py::test_watch_handoff_correction_arms_once_and_wake_is_not_a_user_turn
  - tests/test_api.py::test_work_patch_is_applied_to_live_state_without_correction
invariants: [1, 3, 4, 4b, 9, 10b]
last_passed: 2026-08-04
---

# A patch-producing agent checks the exact semantic patch RCP will apply

An agent describes only the semantic graph change. RCP owns revision, authority,
dependency, and lifecycle bookkeeping, and gives every running Seed, Refresh,
or Work patch-producing session a
bounded way to ask the canonical validator whether its current `patch.json`
would apply to the live graph.

This adds no new control. Each check appears in the existing task event stream,
so a person can see how many checks occurred and whether RCP was reachable.

## Scenario

- The agent-facing schema contains semantic operations only. RCP supplies patch
  kind, author, revision, run scope, Proposal dependencies and base revision,
  object lifecycle revisions, and admission metadata before canonical apply.
- Validation walks operations in their written order against a temporary graph
  containing every earlier valid operation. Whole-patch lookup remains available
  for a node or edge created later, so relations and causes may forward-reference
  same-patch objects without reordering the patch.
- Creating then updating one node, creating then resolving one ambiguity, and
  creating a Decision then proposing its governed choice all validate and
  materialize. Creating the same node id twice is rejected.
- A Work session can run the staged validator client from its writable workspace.
  The client submits `patch.json`; RCP reads the live canonical graph, prepares
  bookkeeping, runs the same semantic validator used by Apply, and returns the
  full result through request and response files in that workspace. The same
  exchange works through the existing SSH transport for a remote stage.
- Each turn has a fixed maximum number of self-checks. Each request adds a task
  event with its count. Patch invalidity and validator unavailability have
  distinct client exit codes, so transport failure cannot become an invalid-patch
  correction loop.
- A human Sync after context assembly is visible to the next self-check. Apply
  does not compare against the turn's original graph revision; it prepares and
  revalidates against current state while holding the canonical append lock.
- An invalid Work patch or watcher handoff is corrected in the same native session
  with the same Work repository, tooling, network, and permission access. Only the
  instruction changes; completed operational work is not rerun.
- Claude Work and correction turns use `bypassPermissions`. The only graph-state
  prohibition is the Work contract against canonical `.research`; repositories
  and ordinary tools are otherwise unrestricted.
- Every patch-producing contract names the rule rather than relying on shared
  session context: Seed, Refresh, Work, and Work correction receive the staged
  validator and must check their current `patch.json` before completion.
- Discuss and Paper Coach explicitly cannot produce or validate a graph patch.
  They receive neither a patch schema nor a validator client.
- The self-check is read-only. RCP's validation immediately before the canonical
  append remains authoritative.

## Assertions

- `agent_schema_contains_no_rcp_bookkeeping_fields`
- `validation_uses_a_staged_graph_without_reordering_operations`
- `same_patch_objects_can_be_created_then_changed`
- `duplicate_same_patch_node_ids_are_rejected`
- `proposal_bookkeeping_is_derived_from_the_live_graph`
- `work_self_check_uses_the_same_live_semantic_validator_as_apply`
- `self_checks_are_bounded_counted_and_transport_failure_is_distinct`
- `apply_revalidates_current_state_under_the_append_lock`
- `correction_reuses_the_original_work_session_and_permissions`
- `claude_work_is_unrestricted_except_for_the_research_contract`
- `seed_refresh_work_and_work_correction_name_the_live_validator_contract`
- `discuss_and_paper_coach_have_an_explicit_no_patch_contract`
- `self_check_never_replaces_final_append_validation`

## Failure means

An agent must guess RCP bookkeeping; validation judges an operation against a
graph that omits earlier operations; a self-check uses frozen context or a
different validator from Apply; a dropped validator is reported as semantic
invalidity; correction loses Work access; or Apply rejects only because the
turn's original graph revision is stale even though the patch is valid now; or
a no-patch surface receives graph authority.
