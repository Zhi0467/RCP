---
id: S103-server-operations-are-console-operations
status: pending
tier: live
driver: pytest + api + ssh
covered_by:
  - tests/test_server_cli.py
  - tests/test_server_control.py
  - tests/test_server_doctor.py
  - tests/test_server_install.py
  - tests/test_server_provider_readiness.py
  - tests/test_server_update_prepare.py
  - tests/test_server_update_rehearsal.py
  - tests/test_server_update_checkpoint.py
  - tests/test_server_update_cutover.py
  - tests/test_server_install_live.py
  - tests/test_team_project_provisioning.py
  - tests/test_server_member_removal_storage.py
  - tests/test_server_member_removal.py
  - tests/test_server_restore_activation.py
invariants: [1, 8]
last_checked: >-
  2026-08-29 — install, update preparation, copied-state candidate rehearsal,
  coherent local rollback checkpoint/re-entry, cutover, loud old-service
  rollback, provider readiness, and project machine preparation have concrete
  OS-authority/private-control regressions. Live run 33278422722 reached the
  first injected rollback-crash boundary on both Ubuntu versions after the
  ordinary forced rollback, exposing a harness-only mixed-account process-group
  cleanup defect now fixed at 75fcafc. Exact-head run 33278678760 started no
  steps because GitHub rejected hosted runners for the account's payment or
  spending-limit state. Member removal now has hermetic exact-preview,
  access-fence, graceful-drain, crash-reentry, and installed-service startup
  reconciliation coverage. A passing 22.04/24.04 update rerun, the live restore
  and member-removal drives, the complete remote drive, and the full scenario
  remain pending. Restore activation is now hermetically implemented: exact
  authority/member confirmation, offline stale-member removal, closed startup,
  root-only private activation, and durable readback pass focused integration
  coverage. Its fresh-host live drive remains pending.
---

# Dangerous operations need the machine, not a login

This scenario is human-confirmed and partially implemented. Its boundary is in
[Server and machine operations](../specs/projects-spaces-and-operations.md#server-and-machine-operations).

Every member has equal space authority and there is no admin role. That only
works if the operations nobody should perform casually are kept off the product
surface entirely, rather than guarded by a rank the design refuses to introduce.

Installation, backup, restore, source update, project provisioning, and member
removal therefore require server operating-system authority. Provider login is
not an RCP operation at all: the operator performs it with the provider's own
command as the execution account, and RCP only checks readiness. A member token —
including a stolen one — cannot redirect backups, pull code, restore over the
space, provision a checkout, or remove anyone.

Removal is the operation with product consequences, so it has to say what it
will end before it ends it.

## Setup

A team space with two enrolled members, one preprovisioned but unenrolled name,
one pending space invitation and one pending project invitation created by the
member who will be removed, a configured backup destination, a project both
belong to, a second project whose only member is the removal target, and that
member running a long task plus active Auto-research and Experiment episodes.

## Drive

1. As an authenticated member, attempt through the HTTP API to change the backup
   destination, trigger a backup, pull an update, restore an archive, provision a
   checkout, and remove the other member.
2. Read what Server Settings shows a member about backups, source version, and
   updates.
3. Run `rcp server doctor` as `rcp`, as an allowed named operator through
   the documented root-owned, `visudo`-validated `sudo -n -u rcp -H` rule, and
   as an ordinary unprivileged account. Attempt one command outside the narrow
   rule.
4. At the console, run the member-removal command for the
   member with running work, and read what it reports before confirming. Confirm
   refusal while they are the second project's only member. Have that member
   attempt self-service permanent-token revocation and confirm the same project
   guard. Add the remaining
   enrolled person to that project through the ordinary member UI, then rerun
   the same console preview; prove the console did not assign membership itself.
5. Confirm. Read the state of that member's tasks and Auto-research/Experiment
   episodes, their project
   memberships, tokens, sessions, invitations, and durable identity tombstone.
   Inject a process crash after the access fence and again during graceful stop,
   then restart and read removal completion. Have the remaining member create a
   fresh pending invitation, then attempt to remove that one remaining enrolled
   member while the preprovisioned name and pending invitation exist. Attempt to
   revoke that member's sole permanent token, then rotate it atomically.
6. Read the repository that member's Work task had already written, and the
   canonical history that member authored.
7. With the server running, invoke a stateful command and verify it uses the
   private control socket rather than opening SQLite.
8. Dirty the managed source checkout and run `sudo rcp server update`; then clean
   it, fast-forward `origin/main`, prepare and rehearse a separate release, enter
   the final maintenance barrier, restart, and read back the running commit.
   Fail npm once before the release switch and fail startup once after it.
   Inspect the rollback checkpoint, both release directories, the current
   pointer, deferred startup-effect owners, every subprocess account, and the
   operator/service account's sudo and systemd permissions. Seed one retained
   local and remote run stage plus partial and complete transfer-inbox sentinels
   before rehearsal; prove the candidate touches none of those live paths, then
   inspect both stages after forced rollback. Kill the root coordinator after
   every rollback journal phase, inspect the durable failure through doctor,
   re-enter through update, and inspect the service and exact old data
   before continuing. Keep a separate configured SSH project unreachable throughout one
   successful rehearsal and cutover, then inspect its update receipt and
   unavailable project projection.
9. Run one command interactively and through its structured-output mode and
   compare the resulting durable state.

## Assert

- `no_api_route_exposes_backup_configuration_to_a_member`
- `no_api_route_exposes_update_installation_to_a_member`
- `no_api_route_exposes_restore_to_a_member`
- `no_member_api_route_executes_checkout_or_git_credential_preparation`
- `no_api_route_exposes_member_removal_to_a_member`
- `server_settings_shows_backup_and_update_state_as_read_only`
- `server_operations_require_machine_authority_not_an_rcp_member_role`
- `each_server_command_rejects_the_wrong_root_or_service_account_entry_identity`
- `root_coordinators_drop_to_rcp_before_source_git_provider_or_data_work`
- `a_running_server_command_uses_the_private_control_socket_not_sqlite`
- `removal_reports_the_tasks_and_episodes_it_will_stop_before_acting`
- `removal_stops_that_members_running_tasks_and_auto_research_and_experiment_episodes`
- `removal_fences_new_turns_but_does_not_kill_one_already_in_flight`
- `an_in_flight_turn_settles_and_apply_rechecks_the_removed_members_access`
- `removal_drops_project_memberships_revokes_the_token_and_ends_sessions`
- `removal_invalidates_pending_invitations_owned_by_the_removed_member`
- `removal_preserves_an_inactive_identity_tombstone_for_historical_attribution`
- `member_removal_refuses_to_strand_the_space_without_an_enrolled_member`
- `a_preprovisioned_name_or_pending_invitation_does_not_satisfy_the_last_member_guard`
- `member_removal_refuses_to_orphan_a_project_with_no_remaining_member`
- `self_service_token_revocation_cannot_make_a_project_unreachable`
- `only_the_ordinary_product_flow_can_add_the_replacement_project_member`
- `the_last_members_sole_token_cannot_be_revoked_but_can_be_rotated_atomically`
- `crash_interrupted_removal_resumes_without_restoring_access_or_forgetting_live_work`
- `removal_leaves_completed_repository_writes_and_external_effects_intact`
- `removal_leaves_authored_canonical_history_and_its_attribution_intact`
- `self_service_credential_rotation_and_guarded_revocation_remain_product_actions`
- `console_operations_refuse_without_their_required_machine_entry_privilege`
- `update_refuses_a_dirty_diverged_or_non_main_source_checkout`
- `update_fast_forwards_origin_main_builds_syncs_restarts_and_reads_back_the_commit`
- `update_uses_only_the_dedicated_source_fetch_identity`
- `update_runs_source_and_build_steps_as_rcp_and_only_restart_coordination_as_root`
- `the_rcp_account_has_no_general_sudo_or_systemd_control_permission`
- `the_named_operator_rule_allows_only_the_documented_service_account_commands`
- `a_candidate_build_never_changes_the_current_or_running_release`
- `candidate_rehearsal_cannot_dispatch_provider_watcher_git_or_external_work`
- `candidate_rehearsal_rebinds_partial_and_complete_transfer_inboxes_to_absent_overlays`
- `candidate_rehearsal_never_reads_imports_or_cleans_a_live_transfer_inbox`
- `an_already_unreachable_ssh_project_is_named_unverified_without_blocking_otherwise_safe_update`
- `unavailable_project_identity_and_projection_survive_update_without_a_false_replay_claim`
- `a_reachable_or_newly_broken_project_still_blocks_candidate_rehearsal`
- `the_switched_candidate_remains_behind_the_same_external_effect_fence_until_verified`
- `a_rollback_restores_local_stages_and_attachments_without_touching_remote_stages`
- `a_failed_candidate_leaves_the_old_release_serving_unchanged`
- `cutover_waits_for_mutations_and_machine_operations_before_its_final_checkpoint`
- `durable_watchers_survive_the_short_update_maintenance_window`
- `a_failed_post_switch_start_restores_and_verifies_the_previous_release_loudly`
- `a_coordinator_crash_cannot_start_a_mixed_or_unrestored_data_tree`
- `rollback_reentry_idempotently_restores_the_exact_pre_cutover_bytes`
- `update_never_resets_changes_force_pulls_or_substitutes_a_package`
- `interactive_and_structured_cli_modes_drive_the_same_command_implementation`

## UI path

Server Settings shows the last successful backup, latest failure, running and
upstream Git commits, and whether `origin/main` is ahead — as status, with no Web
control that changes any of them. Where a member might expect a button, the
interface names the corresponding `rcp server ...` command.

Rotating or safely revoking your own member credential remains an ordinary
member action. Removing somebody's durable identity and stopping all of their
work remains a console operation.

The desktop provisioning bridge is the narrow exception in presentation, not
authority: after an explicit click it may invoke one fixed CLI command over a
separately proven operator SSH route. The browser and member API still perform no
machine operation. Server status does not name a human administrator; the
machine's SSH and `sudo` policy owns that identity.

Deliberately not possible: a member token alone configuring backups, pulling an
update, restoring, provisioning a checkout, or removing another person. Provider
authentication is neither a member action nor an RCP server mutation.

## Boundary

RCP does not define who may administer the machine. It borrows the machine's
privilege system, so the lab's existing `sudo` policy decides. The consequence
belongs in the docs rather than in a rule RCP enforces: machine privilege also
grants read access to every project's history and every member's token hash.

Stopping a removed member's work is deliberate. Because permission is rechecked
at Apply ([S100](S100-permission-is-checked-twice.md)), an episode left running
after its authorizer was removed would spend hours of provider budget and then
have every patch rejected.

The fixed command family is `rcp server install`, `doctor`, `provider check`,
`project provision`, `project transfer-import`, `backup configure`,
`backup run`, `restore`, `member remove`, and `update`. Subcommand-specific
flags may grow during implementation, but ordinary product actions must not
migrate into this privileged namespace.
