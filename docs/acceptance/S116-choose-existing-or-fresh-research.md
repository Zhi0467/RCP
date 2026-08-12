---
id: S116-choose-existing-or-fresh-research
status: implemented
tier: hermetic
driver: pytest + browser + ssh
covered_by:
  - tests/test_setup.py
  - tests/test_transport.py
  - tests/test_history.py
  - browser + live SSH 2026-08-12
invariants: [1, 2, 6, 7]
last_passed: 2026-08-12
---

# Choose existing research or start fresh before setup changes anything

**Confirmed by the human 2026-08-12:** when Add project finds retained RCP
research, the wizard must stop and make the choice explicit. Continuing old
state may expose a version incompatibility. Starting fresh archives the whole
old `.research/` directory; it never overwrites or edits append-only history.

Deleting a project removes its RCP registration, not its repository-owned
research history. Adding the same canonical repository later therefore finds
the old project again. A matching name does not make it new, and a different
name does not make the retained state disappear.

## Setup

Use local and SSH repositories in three states:

1. no `.research/` directory;
2. compatible retained RCP history; and
3. retained history whose replay stops at a known revision under the current
   RCP schema.

## Drive

1. Add the empty repository and confirm setup offers ordinary creation without
   an existing-research warning.
2. Add the compatible repository. After the read-only check, see an **Existing
   RCP research found** window naming its project, canonical location, retained
   revision count, and compatibility with this RCP version.
3. Cancel. Confirm that neither the repository nor the catalog changed.
4. Return and choose **Open existing research**. Confirm that the retained
   project opens and no setup value overwrote its manifest.
5. Add the incompatible repository. The same window names the exact revision,
   rule, and replay message, and says that ordinary resume is unavailable
   because this RCP version cannot fully replay the retained state.
6. Choose **Open last coherent state (read-only)** and inspect the last coherent
   graph without creating a writable home, appending a Patch, or repairing any
   file.
7. Return and choose **Archive existing research and start fresh**. Confirm the
   destructive choice once in that window.
8. RCP atomically renames the complete old `.research/` directory to a unique,
   timestamped sibling and then initializes a new `.research/`. Open the new
   project and inspect both directories.
9. Repeat the compatible, incompatible, cancel, and archive paths over SSH.
10. Force archive rename failure and fresh initialization failure separately.
11. Append a retained Patch after the modal opens but before confirming the
    archive.

## Assert

- `empty_repository_uses_ordinary_creation`
- `existing_research_is_detected_from_the_canonical_location_not_the_typed_name`
- `preflight_replays_without_writing_canonical_state`
- `compatible_history_names_its_project_location_and_revision_count`
- `compatible_history_can_open_without_manifest_overwrite`
- `incompatible_history_names_the_exact_replay_failure`
- `last_coherent_inspection_is_read_only_and_claims_no_writable_home`
- `cancel_writes_nothing`
- `fresh_start_archives_the_complete_research_directory_before_initializing`
- `no_patch_or_materialized_file_is_edited_or_individually_deleted`
- `archive_names_never_overwrite_an_existing_archive`
- `archive_rename_failure_leaves_the_original_research_directory_intact`
- `archive_confirmation_is_bound_to_the_reviewed_history`
- `changed_history_requires_a_fresh_review_under_the_archive_lock`
- `initialization_failure_leaves_the_archive_recoverable_and_registers_no_project`
- `the_same_contract_holds_over_ssh`
- `the_window_has_no_console_or_request_errors`

## UI path

The existing-research choice is a modal window inside the Review step, before
the final setup action can run. It is not a passive warning beneath a Create
button.

Compatible state offers **Cancel**, **Open existing research**, and **Archive
existing research and start fresh**. Incompatible state replaces ordinary open
with **Open last coherent state (read-only)** and shows the exact replay failure
beside it. The archive action states that it moves the complete existing
`.research/` directory to a recoverable sibling before creating anything new.

“Overwrite” is never used as the action label because RCP does not overwrite
history. If the archive cannot be proven complete, fresh initialization does
not begin.
