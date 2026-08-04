---
id: S70-uniform-patch-validation-contract
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_graph_patch_validator.py::test_seed_attempt_stages_and_serves_live_validator_before_final_append
  - tests/test_prompts.py::test_graph_contract_keeps_fanout_and_points_to_payload_files
  - tests/test_prompts.py::test_work_patch_correction_keeps_work_access_and_live_validator_contract
  - tests/test_prompts.py::test_discuss_contract_has_no_patch_path_or_schema_and_no_project_authority
last_passed: 2026-08-04
invariants: [4b, 9, 10b]
---

# Patch-producing tasks self-check through the same validator

Every task contract states the patch rule explicitly. Seed and Refresh, Work,
and Work corrections receive the RCP-staged validator client and must check
their current `patch.json` before reporting completion. Discuss and Paper Coach
receive an explicit no-patch rule and do not receive graph authority.

## Assert

- Seed and Refresh contracts stage and expose the validator client.
- Work and Work correction contracts keep the same validator behavior.
- Discuss and Paper Coach contracts explicitly say that they must not produce
  or validate a graph patch.
- The validator remains read-only and RCP's final append validation remains the
  authoritative gate.
