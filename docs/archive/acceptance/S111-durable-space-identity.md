---
id: S111-durable-space-identity
status: implemented
tier: hermetic
driver: pytest + api
covered_by:
  - tests/test_storage.py::test_space_identity_is_canonical_durable_and_distinct_per_store
  - tests/test_storage.py::test_existing_database_receives_one_durable_space_identity
  - tests/test_storage.py::test_concurrent_initialization_converges_on_one_space_identity
  - tests/test_storage.py::test_space_identity_survives_complete_database_relocation
  - tests/test_storage.py::test_existing_space_identity_is_never_silently_replaced
  - tests/test_api.py::test_health_separates_durable_space_process_and_data_directory_identity
last_passed: 2026-08-11
invariants: [6, 8]
---

# A space keeps one identity across process and path changes

This scenario was confirmed by the human on 2026-08-11.

Every RCP space has a durable, randomly generated `space_id`. It identifies the
authority domain stored in the control-plane database, not the process currently
serving it and not the path where that database happens to live. Personal spaces
and team spaces use the same identity rule.

An existing installation that predates this field receives its identity once as
an additive database migration. Restarting, upgrading, changing the listening
address, or relocating the complete data directory preserves it. Creating a new
space creates a different identity.

## Setup

Two throwaway RCP data directories: one empty, and one containing a pre-change
database with no `space_id`. A third path is available for relocating the first
directory after its backend stops.

## Drive

1. Initialize the empty data directory and read its `space_id` from the store
   and the health endpoint.
2. Stop that backend, start it again on a different address and port, and read
   the identity again.
3. Stop it, relocate the complete data directory, start from the new path, and
   read the identity together with the process `instance_id` and path-derived
   `data_dir_id`.
4. Open the pre-change database twice and inspect its migration.
5. Initialize the other empty data directory and compare its identity.

## Assert

- `space_id_is_generated_once_and_stored_in_the_control_plane`
- `an_existing_data_directory_receives_exactly_one_space_id`
- `space_id_is_a_canonical_random_uuid`
- `a_personal_space_and_a_team_space_use_the_same_identity_rule`
- `space_id_survives_backend_restart`
- `space_id_survives_address_and_port_changes`
- `space_id_survives_complete_data_directory_relocation`
- `space_id_is_not_derived_from_the_data_directory_path`
- `space_id_is_distinct_from_the_process_instance_id`
- `a_new_space_receives_a_different_space_id`
- `a_missing_or_malformed_stored_identity_is_not_silently_replaced`
- `health_reports_space_process_and_data_directory_identity_separately`

## Boundary

Relocating or restoring a complete data directory preserves its `space_id`; it
does not prove that an older copy is offline. Detecting two restored copies or a
rollback to an older archive remains outside this scenario and under the manual
exclusive-recovery boundary in
[S95](S95-durable-team-space.md).

This scenario does not yet add team enrollment, member persistence, saved
connections, service-account ownership, or cross-space navigation. Those remain
in S95, S96, and S105 rather than being implied by the existence of an id.
