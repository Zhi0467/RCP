---
id: S104-backups-never-pause-work
status: pending
tier: live
driver: pytest + ssh
covered_by:
  - tests/test_backup_manifest.py
  - tests/test_backup_sqlite_capture.py
  - tests/test_backup_capture.py
  - tests/test_backup_configuration.py
  - tests/test_backup_encryption.py
  - tests/test_backup_retention.py
  - tests/test_server_doctor.py
  - tests/test_server_restore_state.py
  - tests/test_server_restore_checkouts.py
  - tests/test_server_restore_projects.py
  - tests/test_server_restore_activation.py
  - tests/test_server_install_live.py
  - tests/test_server_restore_live.py
invariants: [1, 2, 7]
---

# A backup interrupts nothing and claims nothing it did not capture

This scenario is human-confirmed and pending its live drive. The strict archive
schema, closed app-data/research-root classifications, nonsecret checkout
recovery descriptor, and lock-free retained-history inventory are covered
hermetically. The online SQLite snapshot and copied-database-only typed project
inventory are also covered with concurrent writers. Optimistic local/SSH
project-file capture now has hermetic coverage for typed source selection,
main/branch heads, complete chat boundaries, stable mutable reads, filtered
remote export, and per-project failure isolation. Deterministic `age` 1.x
encryption, atomic publication, ciphertext readback, immutable receipts, durable
status, proven retention, doctor projection, and first-run timer activation are
also covered hermetically, including one real upstream `age` 1.3.1
encrypt/decrypt drive. Replacement restore is also complete hermetically through
archive verification, lifecycle detachment, fresh checkout reconstruction,
canonical publication/replay, exact authority/member review, offline stale-member
removal, and fenced durable activation. Exact-head workflow run
[33456906376](https://github.com/Zhi0467/RCP/actions/runs/33456906376) now proves
source-host protected backup followed by fresh-host Git reconstruction,
old-authority/member review, offline stale-member removal, activation, and
cleanup on both Ubuntu 22.04 and 24.04. The scenario remains pending because its
broader live concurrent/no-pause, unreachable-SSH partial-capture, active
lifecycle detachment, and full retained-history inspection have not yet been
driven together. Its boundary is in
[Server and machine operations](../specs/projects-spaces-and-operations.md#server-and-machine-operations).

An earlier design had the server delay dispatch and delay applying results for
the duration of each capture. With canonical history on remote machines that
window is minutes, every night. It is also unnecessary: both halves of the
archive are already consistent by construction.

The operational database is in WAL mode, so an online snapshot is consistent
while writers continue. Canonical history is append-only, so recording each
project's head revision and copying up to it cannot capture half a state.

The other half of the promise is honesty. A lab's projects live on machines that
reboot, and one unreachable host must not turn into no backup at all — but an
archive must never claim completeness it does not have.

## Setup

A team space with several registered projects, one of them on a host that can be
made unreachable. A deterministic agent so a task can be held mid-run, a Sync
that can be held mid-batch, an ordinary external watcher, one Experiment-loop
episode waiting on a watcher, one Auto-research episode with pending recovery and
child admission, and one queued episode-report attempt. Use check/provider
helpers that fail the test if any of those old effects executes on the restore
host. Include one provisioning request with a claimed machine step and one
in-progress server-operation receipt. An `age` recipient has its private
recovery identity held off-server, with
one operator-chosen writable backup directory and a fresh restore host. One
captured project has byte-identifiable provider transcripts
imported by personal-to-team transfer plus different live provider-home logs.
Keep one permanent member token, one HTTP session, and one unused team
invitation code alive at capture time.
The same project has one explicitly kept artifact and one unkept output artifact.
It also has one canonical RCP chat, a Paper draft that differs from its canonical
introduction, one safe `.research/facts/` file, and one referenced legacy kept
result view. Its chat, one terminal retryable task, and one Paper writing session
carry native provider-session ids, and `chat_session_contexts` contains the chat
prompt checkpoint. Add one unrelated human file beside each kept repository
directory; those checkout files are not referenced by RCP metadata.
Place byte-identifiable sentinels in every current excluded app-data root:
runtime metadata, a bootstrap manifest, project/Paper snapshots, state/source
caches, temporary attachments and run stages, a partial transfer inbox, and a
sealed personal transfer export. Give the imported `project-sources/` history a
different sentinel that must survive.
It also has one resolved Auto-research graph branch with immutable metadata,
Patches, and a merge receipt plus derived branch outputs.
The directory may be local or mounted; the scenario makes no claim about its
physical topology. Every protected project has a captured completed provisioning
record with repository sources, resolved local/SSH central paths, and deploy-key
fingerprints. The fresh host begins with empty checkout roots, a restored
server-to-remote SSH route, and no provider-native login.

## Drive

1. Start the long task and all active watcher/episode/recovery fixtures, then
   begin a backup. Attempt to dispatch another task and to apply a result while
   the backup runs.
2. Begin a backup during a Sync, with the batch staged but not yet renamed.
   Inspect what the archive captured for that project.
3. Append patches and an RCP chat message after their observed head/byte
   boundaries, and atomically replace the Paper introduction and kept artifact
   while their copies run. Inspect the archive. Force one facts/kept file to
   churn through the bounded stable-read window and inspect partial status.
4. Read the archive manifest.
5. Make one project's host unreachable and run a scheduled backup.
6. Read the archive manifest and Server Settings afterward.
7. Inspect the archive bytes without the recovery identity, decrypt them with the
   off-server identity, and validate the manifest and hashes.
8. Restore the archive into the fresh/empty configured `RCP_DATA_DIR` after
   inspecting that destination and explicitly confirming that the old copy
   cannot resume. Supply the off-server identity through its protected
   file/descriptor path, not raw argv or environment text. First present a copy
   with an unknown newer format/persistence boundary and confirm refusal before
   target mutation. Follow the old source/project deploy-key,
   SSH-route, and provider-native-auth revocation checklist. Let restore display
   fresh per-repository public keys, add them with write access, and reconstruct
   every captured local/SSH central checkout from its bound Git source before it
   publishes `.research`. Force one clone to contain conflicting later canonical
   history and confirm restore refuses to overwrite it. Complete a clean retry,
   serve without provider authentication, then open every captured project and
   read the restored RCP chat, Paper draft/introduction, facts, kept artifact,
   legacy kept result view, and graph-branch history. Inspect the unkept
   artifact's retained metadata, explicit unavailable state, and absent
   Open/Download/Keep/Revise/stage-URL actions.
9. Inspect the restored tasks that had been running at capture time.
   Attempt Pause, Resume, Retry, and graph repair on pre-restore tasks. Continue
   the restored RCP chat before provider login, authenticate directly with the
   provider as the configured execution account, recheck readiness, continue the
   chat, run Refresh over restored imported history, and inspect the provider
   argv/session events and Paper writing-session list. Inspect every restored
   episode, watcher, report attempt, recovery, and child admission, then run the
   real startup reconciliation path and verify none of the old effect helpers
   ran. Inspect the restored provisioning request and server-operation receipt.
   Attempt the old HTTP session and unused invitation code, then reconnect with
   the permanent member token after confirming the snapshot-time roster.
10. Repeat from a snapshot whose member token is known to have been revoked or
    rotated after capture. Confirm restore stays stopped. With another active
    enrolled member present, use restore's offline console member-removal step,
    prove it reuses the ordinary removal transaction after lifecycle detachment,
    and continue. Repeat with the stale token belonging to the only active
    member and confirm restore stays stopped without minting or impersonating a
    replacement credential.
11. Interrupt restore after every journaled candidate, checkout, publication,
    review, and activation boundary. Re-enter the same operation, supplying the
    same archive and protected identity again when its temporary candidate has
    been removed. Prove systemd stays stopped until final readback and that no
    database row, project publication, or review is duplicated.
12. Confirm the imported transcript bytes are present and byte-identical. Search
    the archive and server backup configuration for private recovery identities,
    native provider authentication/configuration directories, live provider
    logs, Git deploy keys, SSH keys, source repositories, unkept materialized
    outputs, raw SQLite WAL/shared-memory, lock/server metadata, bootstrap
    manifests, project/Paper snapshots, temporary input attachments,
    run/transfer staging, sealed personal transfer exports, and caches. Add one
    unknown direct app-data child and confirm the next capture is partial rather
    than silently omitting it.
13. Configure the proposed daily 02:00 schedule and 30-archive retention, change
    both through the CLI, and compare the versioned server config, systemd timer,
    and displayed status. Restore an older SQLite snapshot and inspect the
    machine configuration again.

## Assert

- `a_backup_does_not_delay_dispatch`
- `a_backup_does_not_delay_applying_a_result`
- `the_database_snapshot_is_consistent_while_writers_continue`
- `the_project_inventory_comes_from_the_database_snapshot_not_a_later_live_list`
- `the_manifest_records_each_projects_captured_head_revision`
- `each_protected_project_has_a_bound_nonsecret_checkout_recovery_descriptor`
- `a_project_without_a_reconstructible_checkout_descriptor_is_uncaptured`
- `patches_appended_after_the_recorded_head_are_simply_absent`
- `chat_records_appended_after_the_observed_complete_jsonl_boundary_are_absent`
- `a_chat_suffix_whose_operation_postdates_the_sqlite_snapshot_is_absent`
- `captured_chat_operation_bindings_never_dangle_from_the_captured_database`
- `backup_never_takes_canonical_append_chat_publication_or_refresh_locks`
- `paper_facts_and_kept_files_are_old_or_new_whole_bytes_never_a_mixed_claim`
- `continued_project_file_churn_marks_that_project_uncaptured`
- `a_batch_staged_but_not_renamed_is_captured_as_absent_not_as_half`
- `rebuildable_materialized_outputs_are_excluded_and_regenerated_by_replay_on_restore`
- `main_and_graph_branch_canonical_histories_are_captured_through_the_same_retained_inventory`
- `branch_metadata_patches_and_merge_receipts_restore_but_branch_materializations_do_not`
- `an_unreachable_project_does_not_fail_the_whole_backup`
- `the_manifest_names_each_uncaptured_project_with_a_reason_and_time`
- `server_settings_shows_which_projects_are_actually_protected`
- `the_server_stores_only_an_age_public_recipient`
- `age_1_x_native_x25519_archives_encrypt_and_restore_on_both_supported_ubuntu_releases`
- `plugin_ssh_passphrase_and_post_quantum_recipients_are_rejected_in_this_format`
- `backup_accepts_an_explicit_local_or_mounted_filesystem_directory`
- `backup_status_does_not_claim_to_know_the_destinations_physical_topology`
- `backup_schedule_and_retention_live_in_versioned_machine_config_not_sqlite`
- `the_cli_atomically_updates_the_same_schedule_rendered_into_systemd`
- `restoring_sqlite_does_not_reconfigure_the_backup_timer`
- `retention_keeps_thirty_readback_archives_and_the_newest_complete_archive`
- `every_archive_is_age_encrypted_and_integrity_checked_before_restore`
- `the_private_recovery_identity_remains_off_server`
- `restore_reads_the_private_identity_for_one_run_without_argv_environment_or_persistence`
- `imported_project_provider_histories_are_backed_up_and_restored_byte_identically`
- `explicitly_kept_artifacts_are_backed_up_and_restored_byte_identically`
- `unkept_artifact_metadata_survives_but_excluded_stage_bytes_are_unavailable`
- `restored_artifact_projection_publishes_availability_reason_and_action_decisions`
- `a_restored_kept_artifact_can_open_and_download_but_cannot_keep_or_revise`
- `a_restored_unkept_artifact_has_no_open_download_keep_revise_or_stage_url_action`
- `the_browser_never_constructs_or_probes_a_route_for_an_unavailable_restored_artifact`
- `canonical_rcp_chats_paper_introduction_and_facts_are_backed_up_and_restored`
- `legacy_kept_result_views_are_backed_up_and_restored_byte_identically`
- `only_metadata_referenced_kept_files_are_captured_from_repository_directories`
- `an_unclassified_durable_project_root_makes_capture_visibly_partial`
- `unkept_outputs_and_temporary_input_attachments_are_not_backed_up`
- `run_stages_and_partial_transfer_inboxes_are_not_backed_up`
- `runtime_metadata_bootstrap_locators_and_derived_snapshots_are_not_backed_up`
- `raw_sqlite_wal_and_shared_memory_files_are_not_archived`
- `sealed_personal_transfer_exports_are_not_team_backup_inputs`
- `an_unknown_direct_app_data_root_makes_capture_visibly_partial`
- `live_provider_homes_authentication_and_logs_are_not_backed_up`
- `refresh_can_read_restored_imported_provider_history`
- `a_restored_archive_replays_every_captured_project`
- `restore_uses_the_displayed_configured_fresh_data_root_not_an_ad_hoc_second_root`
- `an_unknown_newer_archive_is_refused_before_target_mutation`
- `restore_journals_every_mutating_phase_outside_data_and_checkout_roots`
- `a_restore_crash_keeps_the_service_stopped_and_resumes_the_same_operation`
- `restore_reentry_never_duplicates_publication_review_or_space_identity`
- `restore_reconstructs_each_captured_checkout_from_git_before_publishing_research`
- `restore_regenerates_bootstrap_locators_and_never_trusts_old_absolute_paths`
- `restore_generates_fresh_project_keys_and_never_extracts_a_source_checkout_from_backup`
- `archive_external_or_conflicting_canonical_history_is_not_overwritten_on_restore`
- `an_uncaptured_project_remains_visible_but_unavailable_after_restore`
- `tasks_running_at_capture_time_are_restored_as_interrupted`
- `every_pre_restore_task_is_history_only_with_status_answer_and_receipts_preserved`
- `history_only_tasks_reject_pause_resume_retry_and_graph_repair_at_the_backend`
- `history_only_task_and_chat_projections_expose_no_native_session_continuation`
- `writing_sessions_and_chat_session_contexts_are_cleared_on_restore`
- `old_native_session_ids_are_not_exported_as_executable_continuations`
- `continuing_a_restored_rcp_chat_starts_a_fresh_checked_provider_session`
- `provider_login_is_not_required_to_serve_and_read_restored_history`
- `missing_provider_login_blocks_new_execution_until_native_login_and_recheck`
- `every_nonterminal_pre_restore_episode_is_stopped_with_a_restore_diagnostic`
- `pre_restore_watchers_are_stopped_without_poll_or_delivery`
- `pre_restore_report_recovery_and_child_admission_cannot_restart`
- `normal_startup_schedules_no_external_effect_from_pre_restore_state`
- `restore_invalidates_old_http_sessions_and_unused_enrollment_codes`
- `restore_preserves_only_snapshot_time_permanent_member_reconnect_credentials`
- `restore_requires_review_of_the_snapshot_time_member_roster_before_serving`
- `restore_does_not_claim_to_include_post_snapshot_credential_revocations`
- `a_known_post_snapshot_credential_change_keeps_restore_stopped`
- `a_stale_member_can_be_removed_only_when_another_active_member_remains`
- `the_only_members_stale_token_cannot_be_replaced_by_machine_authority`
- `restored_in_progress_provisioning_requires_explicit_operator_reentry`
- `restored_machine_operation_leases_are_interrupted_not_auto_resumed`
- `provider_authentication_configuration_git_deploy_keys_ssh_keys_and_source_repositories_are_absent`
- `restore_requires_explicit_old_authority_exclusion_before_serving`
- `restore_names_old_git_ssh_and_provider_authority_that_must_be_revoked_or_destroyed`
- `rcp_never_collects_or_performs_provider_or_ssh_revocation_during_restore`

## Boundary

Excluding rebuildable materialized outputs is what makes the no-pause design safe:
`graph.json` captured beside an earlier head revision would be the one way to
produce an inconsistent archive. Restoration replays instead. This is enforcing
an existing rule, not adding one.

A backup does not preserve an in-memory execution and must not claim to. Tasks
that were running come back interrupted, with their receipts and committed
results intact. Because provider homes, task stages, and provider-native
conversation state are excluded, all pre-restore tasks are history-only and
native Resume/prompt indexes are detached. The human may continue the same RCP
chat, but that next turn deliberately starts a fresh session on the checked
replacement execution account.

RCP cannot itself prove that the old authority is gone. `space_id` surviving a
restore makes the replacement the same space, so this scenario requires the
operator's explicit old-authority confirmation and verifies the concrete
revocation checklist; the durable identity boundary remains in
[S95](S95-durable-team-space.md).

The first implementation accepts the upstream `age` 1.x CLI and one native
X25519 public recipient. Recipient rotation creates new archives for the new
recipient; it does not rewrite old archives or copy a private recovery identity
onto the server. A real restore drill, not successful encryption alone, proves
the backup usable.
