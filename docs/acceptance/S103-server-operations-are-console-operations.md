---
id: S103-server-operations-are-console-operations
status: pending
tier: live
driver: pytest + api + ssh
covered_by: none
invariants: [1, 8]
---

# Dangerous operations need the machine, not a login

This scenario is human-confirmed and pending implementation. Its boundary is in
[Server and machine operations](../specs/projects-spaces-and-operations.md#server-and-machine-operations).

Every member has equal space authority and there is no admin role. That only
works if the operations nobody should perform casually are kept off the product
surface entirely, rather than guarded by a rank the design refuses to introduce.

Installation, backup, restore, source update, provider credential setup, project
provisioning, and member removal therefore require server operating-system
authority. A member token — including a stolen one — cannot redirect backups,
pull code, restore over the space, configure a provider, provision a checkout,
or remove anyone.

Removal is the operation with product consequences, so it has to say what it
will end before it ends it.

## Setup

A team space with two members, a configured backup destination, a project both
belong to, and one member running a long task and an active campaign.

## Drive

1. As an authenticated member, attempt through the HTTP API to change the backup
   destination, trigger a backup, pull an update, restore an archive, configure
   a provider, provision a checkout, and remove the other member.
2. Read what Server Settings shows a member about backups, source version, and
   updates.
3. Run `rcp server doctor` as `rcp`, as an allowed named operator through
   `sudo -n -u rcp -H`, and as an ordinary unprivileged account.
4. At the console, run the member-removal command for the
   member with running work, and read what it reports before confirming.
5. Confirm. Read the state of that member's tasks and campaign, their project
   memberships, their token, and their sessions.
6. Read the repository that member's Work task had already written, and the
   canonical history that member authored.
7. With the server running, invoke a stateful command and verify it uses the
   private control socket rather than opening SQLite.
8. Dirty the managed source checkout and run `sudo rcp server update`; then clean
   it, fast-forward `origin/main`, prepare a separate release, restart, and read
   back the running commit. Fail npm once before the release switch and fail
   startup once after it. Inspect both release directories, the current pointer,
   every subprocess account, and the operator/service account's sudo and systemd
   permissions.
9. Run one command interactively and through its structured-output mode and
   compare the resulting durable state.

## Assert

- `no_api_route_exposes_backup_configuration_to_a_member`
- `no_api_route_exposes_update_installation_to_a_member`
- `no_api_route_exposes_restore_to_a_member`
- `no_api_route_exposes_provider_or_checkout_provisioning_to_a_member`
- `no_api_route_exposes_member_removal_to_a_member`
- `server_settings_shows_backup_and_update_state_as_read_only`
- `server_operations_require_machine_authority_not_an_rcp_member_role`
- `a_running_server_command_uses_the_private_control_socket_not_sqlite`
- `removal_reports_the_tasks_and_campaigns_it_will_stop_before_acting`
- `removal_stops_that_members_running_tasks_and_campaigns`
- `removal_drops_project_memberships_revokes_the_token_and_ends_sessions`
- `removal_leaves_completed_repository_writes_and_external_effects_intact`
- `removal_leaves_authored_canonical_history_and_its_attribution_intact`
- `a_member_leaving_voluntarily_remains_available_in_the_app`
- `console_operations_refuse_without_service_account_privilege`
- `update_refuses_a_dirty_diverged_or_non_main_source_checkout`
- `update_fast_forwards_origin_main_builds_syncs_restarts_and_reads_back_the_commit`
- `update_uses_only_the_dedicated_source_fetch_identity`
- `update_runs_source_and_build_steps_as_rcp_and_only_restart_coordination_as_root`
- `the_rcp_account_has_no_general_sudo_or_systemd_control_permission`
- `a_candidate_build_never_changes_the_current_or_running_release`
- `a_failed_candidate_leaves_the_old_release_serving_unchanged`
- `a_failed_post_switch_start_is_loud_and_never_silently_rolls_back`
- `update_never_resets_changes_force_pulls_or_substitutes_a_package`
- `interactive_and_structured_cli_modes_drive_the_same_command_implementation`

## UI path

Server Settings shows the last successful backup, latest failure, running and
upstream Git commits, and whether `origin/main` is ahead — as status, with no Web
control that changes any of them. Where a member might expect a button, the
interface names the corresponding `rcp server ...` command.

**Leave space** remains an ordinary member action.

The desktop provisioning bridge is the narrow exception in presentation, not
authority: after an explicit click it may invoke one fixed CLI command over a
separately proven operator SSH route. The browser and member API still perform no
machine operation. Server status does not name a human administrator; the
machine's SSH and `sudo` policy owns that identity.

Deliberately not possible: a member token alone configuring backups, pulling an
update, restoring, configuring credentials, provisioning a checkout, or removing
another person.

## Boundary

RCP does not define who may administer the machine. It borrows the machine's
privilege system, so the lab's existing `sudo` policy decides. The consequence
belongs in the docs rather than in a rule RCP enforces: machine privilege also
grants read access to every project's history and every member's token hash.

Stopping a removed member's work is deliberate. Because permission is rechecked
at Apply ([S100](S100-permission-is-checked-twice.md)), a campaign left running
after its authorizer was removed would spend hours of provider budget and then
have every patch rejected.

The fixed command family is `rcp server install`, `doctor`, `provider configure`,
`project provision`, `backup configure`, `backup run`, `restore`,
`member remove`, and `update`. Subcommand-specific flags may grow during
implementation, but ordinary product actions must not migrate into this
privileged namespace.
