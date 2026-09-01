---
id: S98-move-a-project-into-a-team-space
status: pending
tier: live
driver: pytest + browser + desktop + ssh
covered_by:
  - tests/test_project_home_transfer.py
  - tests/test_project_transfer_request_storage.py
  - tests/test_project_transfer_request_api.py
  - tests/test_project_transfer_request_restore.py
  - tests/test_transfer_archive_manifest.py
  - tests/test_transfer_record_models.py
  - tests/test_transfer_records.py
  - tests/test_transfer_project_files.py
  - tests/test_transfer_provider_history_selection.py
  - tests/test_imported_provider_sources.py
  - tests/test_imported_provider_source_lifecycle.py
  - tests/test_imported_provider_source_remote_staging.py
  - tests/test_server_restore_projects.py
  - tests/test_server_cli.py
  - tests/test_server_control.py
  - tests/test_server_update_checkpoint.py
  - tests/test_server_update_rehearsal.py
  - tests/test_transfer_project_configuration.py
  - tests/test_transfer_import.py
  - tests/test_transfer_import_storage.py
  - tests/test_transfer_source.py
  - tests/test_transfer_source_archive.py
  - tests/test_transfer_catalog_finalizer.py
  - tests/test_transfer_target.py
  - tests/test_transfer_target_upload.py
  - web/tests/projectSetup.test.mjs
  - web/src-tauri/src/project_transfer.rs::tests
  - web/src-tauri/src/server_commands.rs::tests
invariants: [1, 3, 6, 11]
---

# Hand a personal project over to the lab, once

This live scenario remains pending because the complete source-built desktop
interruption drive against two real spaces and a real SSH operator route has not
yet passed. The backend transfer, native desktop relay, proof return/source
cleanup orchestration, unified wizard, and crash-recovery coordinator are
implemented and hermetically verified.

The implemented path now covers the canonical home-transfer Patch, linked
cross-space requests, independent human receipts, strict repository/configuration
binding, one-time protected proofs, snapshot-consistent terminal operational
export, canonical/project-file capture, best-effort exact provider-history
selection, the deterministic private source archive, and the source admission
fence. The target running service owns one request/digest upload lease; the
stdin-only CLI owns only the derived private partial/final paths and never opens
SQLite. The same CLI then asks the running service to decode the exact sealed
archive, validate retained local or SSH history, durably bind that exact
pre-publication configuration and retained-history evidence, invoke the atomic importer,
replay the imported canonical state, prepare the reviewed central registration,
and compound-commit the project row, first member seat, provisioning completion,
consumed upload, activation receipt, and `target_activated` request phase.

An import retry after partial publication reuses the bound pre-publication
evidence instead of inspecting already-changed target files. An activation retry
returns the same receipt and removes only a verified leftover inbox file. Before
that compound commit, a failed decode/import remains
unregistered and invisible. Update checkpoints preserve only receipt-backed
`complete` uploads, ignore `consumed` uploads, and reject any untyped or leftover
inbox bytes. Restore invalidates machine-bound nonterminal upload authority; an
`archive_bound` request has a reviewed storage re-entry that issues a fresh lease,
and the implemented desktop coordinator can resume that exact re-entry. Its
behavior still needs the pending source-built two-space/SSH interruption drive.
Imported provider histories remain project-owned, separate from live provider
homes, protected through backup/restore/update, and validated for local and SSH
Seed/Refresh. The reachable-SSH interruption/removal drive in step 11 remains
pending. Current boundaries are in
[Project identity and home](../specs/projects-spaces-and-operations.md#project-identity-and-home)
and [Personal-to-team transfer archive](../specs/server-and-machine-operations.md#personal-to-team-transfer-archive).

Transfer is personal space → team space, one way. The team server prepares a
separate central checkout set, with each checkout owned by `rcp` locally or by
the declared execution account on its SSH machine; the person's checkout stays
in place and keeps its owner. The durable `project_id` and canonical Patch
history move to the new home. The source is fenced before the target can write,
so interruption may make the project temporarily unavailable but can never make
both copies writable.

## Setup

A personal space with a project whose canonical state repository and two
truth-scope repositories are in the person's checkouts, plus a connected team
space with one server-local checkout root owned by `rcp` and one reachable SSH
execution account. The personal project has terminal tasks; canonical chats
whose durable history retains attachment display metadata after the temporary
input bytes have expired; a Paper draft that intentionally differs from its
canonical introduction; one safe `.research/facts/` file; one referenced kept
artifact; one unkept output artifact whose disposable stage is excluded; one
referenced legacy kept result view; stopped
episodes/watchers/reports; and complete Codex, Claude, and provider-fixture
conversations matched to its repositories. At least one chat and one Paper
writing-session row carry a source native-session binding.
At least one selected conversation begins before `last_refresh_at` and continues
after it.
The source manifest uses distinct personal repository paths, machine hosts, and
provider roots while retaining machine/repository aliases referenced by
historical `SourceRef`s. One target clone is empty; a second contains an
identical retained-history prefix; conflict fixtures contain a different
identity, a later canonical head, a renamed historical alias, and one changed
Patch byte.

## Drive

1. Open the project in the personal space, choose **Move to team space**, and
   confirm the shared project wizard opens with **Move an existing personal
   project to a team** preselected and the source project pinned. Select a saved
   team connection.
2. Inspect the durable target provisioning request and its intended central
   paths before any project authority changes. Inspect both linked requests'
   independent source-release and target-activation proof commitments, bound
   source-configuration digest, and negotiated schema/archive-codec version;
   prove neither raw proof is exposed. Attempt one link with no common version.
3. Run `rcp server project provision <request-id>`. Complete the deploy-key
   write check, central checkout preparation, and provider/execution readiness.
4. At **ready for review**, read the final review and confirm once. Inspect the
   team backend's target-admission receipt and the personal backend's
   source-release receipt, each attributed to its own authenticated actor.
   Change the source manifest after target preparation in a separate drive and
   prove source release returns to preparation before fencing or changing home.
5. Repeat once while interrupting after target admission but before source
   release. Reload both spaces, verify the source is still writable and the
   target is not active, then resume the same request without repeating the
   target confirmation.
6. Interrupt once after the source is fenced but before target registration;
   inspect the sealed source export, digest, and post-fence source-release proof
   envelope, restart the personal backend, attempt ordinary project Delete, then
   resume the same request and exact bytes and finish it. Verify the source
   accepts the target cleanup receipt only after checking the activation proof.
   Attempt to fetch that proof with the browser session, another member token,
   and before activation; then fetch it through the native shell with the exact
   target confirmer's stored token and complete the public cleanup acknowledgment.
7. In the team space, open the project, read its graph and revision history, and
   start a task from the central checkout.
8. In the personal space, attempt to register or write the old checkout.
9. Inspect the canonical home-change history, both catalog records, central
   checkout ownership, and unchanged ownership of the person's checkout.
10. Inspect the versioned archive, automatic provider-selection summary, imported
    finished operational history, transformed RCP chat, both Paper
    draft/canonical sides, facts, referenced kept files, and complete read-only
    selected provider histories under the target's project-owned app-data source
    root. Compare the archived source-manifest provenance with the rebuilt live
    target manifest and exercise every retained-history conflict fixture before
    import. Open the kept artifact and inspect the unkept artifact's historical
    metadata, unavailable reason, and absent Open/Download/Keep/Revise actions
    and URL. Confirm that the
    crossing provider conversation was not sliced at
    `last_refresh_at`; attempt to Resume/Retry it, the transferred RCP chat, and
    the former Paper session or reach their former provider sessions and stages.
11. Run Refresh once through a local target provider account and once through a
    reachable SSH target provider account. Verify both contracts can read
    imported project sources and new target-account native logs while preserving
    the transferred watermark. For SSH, inspect the immutable task inputs and
    prove only imported project-owned bytes were staged; interrupt and Resume
    once, then remove the stage and prove the existing clean-retry path is loud.
12. Attempt to move a team project back to the personal space and to a second
    team space.
13. Repeat once with the desktop's operator route unavailable, inspect the exact
    Terminal relay/import commands, then interrupt an automated archive upload
    and resume the same request and digest.
14. Attempt target activation with only the target confirmation and then with
    only the source receipt present in a crash fixture. Attempt activation with
    a forged serialized source receipt, a missing release proof, and a wrong
    proof. Attempt source cleanup with a forged target receipt and a wrong
    activation proof.

## Assert

- `transfer_appends_a_home_change_to_canonical_history`
- `project_settings_opens_move_in_the_same_unified_project_wizard`
- `the_home_change_attributes_both_space_scoped_human_actors`
- `project_id_is_unchanged_by_transfer`
- `the_team_uses_a_separate_central_checkout_set_on_the_declared_accounts`
- `the_persons_checkout_keeps_its_path_and_owner`
- `the_confirmation_names_the_source_and_target_directories`
- `the_confirmation_names_the_active_work_that_will_be_settled`
- `one_desktop_review_records_independent_target_and_source_human_confirmations`
- `each_space_attributes_its_confirmation_to_its_own_authenticated_actor`
- `cross_space_user_ids_are_never_assumed_to_match`
- `linked_requests_bind_independent_source_release_and_target_activation_commitments`
- `linked_requests_negotiate_schema_and_archive_codec_before_preparation`
- `no_common_transfer_version_fails_while_the_source_remains_writable`
- `source_configuration_drift_is_rechecked_before_release_or_fence`
- `raw_transition_proofs_are_absent_before_their_committed_boundaries`
- `a_target_only_confirmation_leaves_the_source_writable_and_resumes_idempotently`
- `the_machine_import_command_cannot_replace_either_human_confirmation`
- `the_target_cannot_activate_with_only_one_human_confirmation`
- `the_source_is_fenced_before_the_target_becomes_writable`
- `interruption_can_leave_zero_writers_but_never_two_writers`
- `an_interrupted_transfer_resumes_from_its_durable_request`
- `the_source_atomically_retains_one_mode_0600_archive_after_home_change`
- `every_relay_retry_rehashes_and_streams_the_same_digest_bound_source_bytes`
- `a_missing_or_corrupt_sealed_source_archive_is_repair_not_regeneration`
- `ordinary_source_project_delete_is_unavailable_until_target_activation`
- `target_activation_receipt_allows_only_exact_source_export_cleanup`
- `the_target_verifies_the_post_fence_source_proof_before_import`
- `the_source_verifies_the_post_activation_target_proof_before_cleanup`
- `only_the_exact_target_confirmer_native_token_can_retrieve_the_committed_activation_proof`
- `cookie_authenticated_web_and_cli_progress_never_expose_a_raw_transition_proof`
- `forged_serialized_cross_space_receipts_grant_no_transition_authority`
- `raw_transition_proofs_never_enter_web_state_argv_logs_or_imported_history`
- `the_source_space_can_no_longer_write_the_project`
- `history_authored_before_the_transfer_remains_readable_and_attributed`
- `execution_configuration_must_be_re_established_in_the_target_space`
- `the_source_manifest_is_provenance_and_never_the_live_target_manifest`
- `historical_machine_and_repository_aliases_remain_replayable_after_rebinding`
- `target_paths_hosts_and_profiles_come_from_reviewed_configuration_while_provider_roots_follow_target_account_conventions`
- `main_and_retained_branches_replay_against_the_rebuilt_target_manifest`
- `an_identical_retained_history_prefix_may_be_reused_without_overwrite`
- `different_identity_later_head_renamed_alias_or_changed_patch_blocks_import`
- `personal_git_provider_ssh_and_machine_credentials_do_not_carry_over`
- `one_versioned_checksummed_archive_is_the_only_transfer_format`
- `the_native_relay_uses_one_request_derived_protected_target_inbox`
- `the_import_cli_accepts_a_request_id_and_never_an_arbitrary_archive_path`
- `the_native_relay_streams_stdin_without_scp_mv_or_a_remote_shell_pipeline`
- `the_native_shell_pins_the_personal_backend_and_archive_bytes_never_cross_tauri_ipc`
- `the_import_cli_uses_the_control_socket_and_never_opens_sqlite`
- `browser_state_shell_arguments_logs_and_credentials_never_contain_archive_bytes`
- `an_interrupted_upload_resumes_the_same_request_and_digest`
- `all_finished_human_visible_history_and_kept_artifacts_transfer`
- `imported_terminal_tasks_keep_their_status_and_answer_but_are_history_only`
- `history_only_tasks_cannot_pause_resume_or_retry_at_projection_or_admission`
- `history_only_task_projection_exposes_no_source_native_session_continuation`
- `new_target_work_creates_a_new_ordinary_task_under_target_configuration`
- `canonical_chat_jsonl_transfers_as_typed_history_not_a_raw_resume_binding`
- `chat_text_graph_receipts_and_display_only_attachment_metadata_survive`
- `chat_native_session_execution_machine_and_cwd_do_not_transfer`
- `paper_draft_base_ancestor_and_canonical_introduction_all_transfer`
- `completed_paper_coach_answers_transfer_but_writing_session_resume_rows_do_not`
- `safe_facts_files_transfer_byte_identically`
- `referenced_kept_artifacts_and_legacy_result_views_transfer_byte_identically`
- `unkept_artifact_metadata_transfers_but_excluded_stage_bytes_are_unavailable`
- `artifact_projection_publishes_availability_reason_and_all_four_action_decisions`
- `a_kept_imported_artifact_can_open_and_download_but_cannot_keep_or_revise`
- `an_unkept_imported_artifact_has_no_open_download_keep_revise_or_stage_url_action`
- `the_browser_never_constructs_or_probes_a_route_for_an_unavailable_artifact_action`
- `unreferenced_repository_artifact_and_view_files_do_not_transfer`
- `retained_graph_branch_metadata_patches_and_merge_receipts_transfer_without_materializations`
- `chat_attachment_metadata_transfers_but_temporary_input_bytes_do_not`
- `provider_conversations_are_selected_automatically_by_best_effort_path_match`
- `provider_history_capture_uses_each_saved_source_execution_account_locally_or_over_ssh`
- `provider_history_capture_never_falls_back_to_another_home_or_member_laptop`
- `unmatched_or_unreadable_provider_conversations_warn_without_blocking_transfer`
- `every_selected_provider_conversation_transfers_in_full`
- `each_selected_native_transcript_is_byte_identical_after_import`
- `imported_transcripts_live_under_project_app_data_not_research_or_provider_home`
- `the_ingestion_watermark_never_truncates_a_transferred_conversation`
- `target_refresh_reads_imported_history_and_new_target_account_logs`
- `remote_refresh_stages_only_imported_project_sources_and_never_a_native_provider_home`
- `remote_refresh_resume_reuses_the_same_imported_source_fingerprint`
- `missing_remote_imported_source_stage_requires_a_visible_clean_retry`
- `missing_or_corrupt_durable_imported_history_blocks_refresh_instead_of_being_skipped`
- `source_resume_stage_machine_scratch_cache_and_credentials_do_not_transfer`
- `imported_history_cannot_resume_or_retry_through_source_execution_bindings`
- `the_target_activates_only_after_database_and_file_readback`
- `team_to_personal_transfer_is_not_offered`
- `team_to_team_transfer_is_not_offered`

## UI path

**Move to team space** lives in Project Settings in the personal space, next to
the project's home information. It deep-links into the same project wizard used
for personal and new-team setup; it does not mount a separate transfer wizard.
The intent is offered only when the personal backend permits export, the
selected team backend permits import, and the desktop-native bridge reports an
authenticated relay route between them. Choosing it first creates the target's
durable provisioning request. Its
preparation screen states, in plain language:

- which team space will own the project;
- the personal source paths that remain owned by the person;
- the new central checkout paths and their owning execution accounts;
- what active work will be settled first; and
- that execution settings must be chosen again in the team space.

The human cannot confirm until server preparation is **ready for review**. One
desktop review action first records target admission under the authenticated
team member, then source release under the authenticated personal owner. The
backends keep their own actor identities and credentials. If the app stops
between them, the UI says **Target accepted; source release still required** and
resumes the same request. Only after both confirmations and successful import
does the project disappear from the personal index and appear in the team
space's list. The old entry is not left behind as a broken row, but the old
checkout remains an ordinary personal working copy.

Deliberately not possible: moving a project out of a team space, moving one
between team spaces, and confirming without having been shown the directory
list.

The final review names that all finished task, chat/artifact, Paper, and stopped
episode/watcher/report history will transfer as non-resumable history. Chat
attachment display metadata remains in that history, while expired/temporary
input bytes do not become durable. Provider selection is automatic and does not
add a classification screen or confirmation gate; the transfer receipt reports
selected and skipped counts. Complete selected histories transfer as read-only
Seed/Refresh sources. Provider credentials, native-home installation, resumption
authority, active work, scratch, caches, and machine configuration do not
transfer.

## Implemented substrate

T1 through T5b are implemented as of 2026-08-31. The source and target state
machines, exact archive/export/import boundary, central-checkout rebuild,
retained-history transformation, activation proofs, native relay, unified move
wizard, independent actor confirmations, cold-restart session recovery, and
explicit protected manual relay all have focused coverage. The Web receives
only safe projections and native decisions; cross-space receipts, proofs,
configuration commitments, and archive bytes remain behind the native boundary.
Automatic relay failures are visible and retryable rather than being treated as
success. This scenario remains pending because the complete source-built
desktop interruption drive against two real spaces and a real SSH operator
route has not yet passed.

## Boundary

Releasing a project *from* a team space remains outside this first lab-server
slice. It is kept off the product surface because with equal members any single
person could otherwise pull a shared project private unilaterally, and the fix
for that would be a rank the design refuses.

Transfer is a durable cross-space sequence rather than one impossible distributed
transaction. Its recovery rule is fail closed: after the source home changes,
the target request must finish or stay visibly repairable; the source never
reopens admission as a fallback. The sealed source archive is recovery-critical
until target activation and cannot be regenerated under an already bound digest.
The desktop coordinates the two spaces and may
invoke the target CLI over SSH, but both backends and the CLI publish the durable
truth the UI renders.
