---
id: S95-durable-team-space
status: pending
tier: live
driver: pytest + api + ssh
covered_by:
  - tests/test_server_install.py
  - tests/test_server_doctor.py
  - tests/test_server_control.py
  - tests/test_server_cli.py
  - tests/test_api_server_status.py
  - tests/supervisor_reboot_live.py
invariants: [6, 8]
---

# A team space outlives every process that serves it

Its boundaries are in
[Spaces and durable identity](../specs/projects-spaces-and-operations.md#spaces-and-durable-identity)
and [Confirmed first team-server target](../specs/server-and-machine-operations.md#confirmed-first-team-server-target).

A space is an authority domain, not an installation. Restarting, upgrading, or
moving the server does not create a new space and does not make members enroll
again. Finding a *different* space at a familiar address is the one case that
must stop everything, because a stale client would otherwise submit work to the
wrong authority.

The same section of that document makes the authority structural rather than
cooperative: the backend owns its data directory under a dedicated
operating-system account, so a member with an ordinary shell on the lab machine
cannot read the control plane, append to canonical history directly, or take the
singleton lock and become the authority themselves.

The first deployment is one Ubuntu 22.04 or 24.04 LTS x86-64
server running systemd. A dedicated `rcp` account owns the service, data, and
server-local central checkouts; an explicit remote execution account owns any
checkout on its SSH machine. Humans remain distinct members and do not share
either execution login. The installed RCP version is an exact human-promoted build, served without
source reload.

## Implemented substrate

F1 through F6d are implemented as of 2026-09-01. The server CLI command and
event contract, Linux service layout, idempotent installer and unit, private
control socket, commit identity and `server doctor`, and the supervisor
artifact/validation/checkpoint/cutover path have focused coverage. Historical
source-install evidence from that period: a first manual install on Ubuntu
22.04 x86-64 created the dedicated `rcp` account,
managed checkout, immutable release, and an initialized team space, then passed
HTTP health and `server doctor`. This scenario remains pending because the
mismatched-space refusal and the port-change reconnection have not been driven
end to end against a real saved desktop connection.

## Setup

A supported Ubuntu test host with the prerequisites in the
[operator guide](../server.md), operator sudo access, and the RCP and supervisor
wheel URLs from the same promoted release. No RCP source checkout is required.
The drive establishes the dedicated `rcp` account, non-reloading system service,
private data directory, and initialized team space; then enroll one member,
prepare one central project checkout, and save a desktop connection recording
the expected `space_id`.

## Drive

1. Set `RCP_WHEEL_URL` and `RCP_SUPERVISOR_WHEEL_URL` to those paired download
   URLs and run
   `sudo /usr/local/bin/uv tool run --from "$RCP_WHEEL_URL" --with "$RCP_SUPERVISOR_WHEEL_URL" rcp server install --team-name "My lab"`.
   Confirm the fresh systemd unit is still stopped. Follow the wizard's
   interactive `space init --team` step as `rcp`, capture the one-time code,
   then continue to enable/start the unit and inspect which steps ran as
   root versus `rcp`. Inspect the account home, shell, unusable non-locking
   shadow value, groups, sudo policy, and global SSH configuration. Prove a
   leading-`!` account lock is not used. Confirm no RCP source checkout or source
   deploy key was installed.
2. Run `sudo -u rcp -H /usr/local/bin/rcp server doctor`. Read the service
   account, data path, reload mode, immutable installation id, and selected,
   current, and running release identities. Verify the root-owned selected
   receipt, `/etc/rcp/current`, and running release agree on the promoted build
   and exact commit.
3. Read the `space_id` after first initialization.
4. Stop the system service and start it again. Read the `space_id` and member
   record.
5. Change the loopback port, restart, and reconnect the saved connection.
6. Initialize a *second*, unrelated space in a different data directory and
   serve it at the address the saved connection expects. Attempt a mutation
   through that connection.
7. Inspect the data directory, runtime socket, server-local credentials, and
   central checkout ownership, modes, and exact installed paths. Confirm all
   ordinary server-local RCP-owned state is below `/home/rcp/rcp-server`, an SSH
   checkout's key stays below its exact remote account's verified home-derived
   credential root, provider-native state remains in each execution account's
   normal home locations, and only documented root/system paths live elsewhere.
   Inspect an ordinary member's independent checkout. Exercise a deliberately
   installed key-only direct `rcp@server` route, remove that key, and exercise
   the preferred named-operator narrow-sudo route without enabling password SSH.
8. With the backend running, attempt to take the singleton lock from a second
   process against the same data directory.
9. With only one enrolled member, attempt to revoke that member's sole
   permanent token and confirm refusal, then rotate it and reconnect with the
   replacement.

## Assert

- `space_id_is_generated_once_and_survives_restart`
- `installation_id_is_stable_for_one_machine_install_and_distinct_from_space_and_member_ids`
- `paired_promoted_wheels_bootstrap_without_an_rcp_source_checkout_or_source_deploy_key`
- `space_id_survives_an_address_and_port_change`
- `restart_preserves_members_and_token_hashes_without_re_enrollment`
- `instance_id_changes_across_process_lifetimes`
- `space_id_is_not_derived_from_the_data_directory_path`
- `a_personal_space_also_mints_a_durable_space_id`
- `doctor_reports_selected_current_and_running_release_identities`
- `the_root_selected_receipt_current_pointer_and_running_release_agree`
- `the_rcp_account_has_a_fixed_home_real_shell_no_usable_password_and_no_broad_privilege`
- `the_shadow_state_denies_passwords_without_blocking_public_key_ssh`
- `install_does_not_enable_password_ssh_or_edit_global_sshd_config`
- `direct_rcp_ssh_is_key_only_and_optional_while_named_operator_sudo_still_works`
- `the_only_members_last_token_cannot_be_revoked_without_an_atomic_replacement`
- `root_owns_supervisor_and_operator_runtimes_while_application_installation_runs_as_rcp`
- `fresh_install_initializes_interactively_as_rcp_before_first_service_start`
- `the_bootstrap_code_never_enters_a_service_log`
- `initialization_never_opens_sqlite_beside_the_running_service`
- `the_team_service_runs_from_the_verified_promoted_release_without_reload`
- `an_unexpected_space_id_blocks_mutations_until_the_human_reconnects`
- `an_unexpected_space_id_still_permits_reading_what_the_client_cached`
- `data_runtime_credentials_and_server_local_checkouts_are_owned_only_by_rcp`
- `a_remote_checkout_key_stays_only_in_its_verified_remote_account_home`
- `a_remote_private_key_never_crosses_the_ssh_transport_or_server_filesystem`
- `ordinary_server_state_uses_the_home_centered_rcp_server_layout`
- `only_documented_root_and_system_integration_lives_outside_the_service_home`
- `provider_native_auth_stays_in_the_execution_accounts_normal_home`
- `remote_checkouts_are_owned_only_by_the_declared_remote_execution_account`
- `a_members_personal_checkout_keeps_its_original_owner`
- `a_second_backend_cannot_serve_the_same_data_directory`

## Boundary

This scenario does not promise that two *restored copies* of one space can
detect each other. That limitation is deliberate and operator-owned under the
[server and machine operations contract](../specs/server-and-machine-operations.md).
`space_id` surviving a restore is what makes the replacement the same space, and
it is also what makes two simultaneously running copies indistinguishable.

Detecting that a familiar `space_id` has been rolled back to an older archive is
explicitly excluded. Restore safety remains the operator's stopped-service and
old-authority exclusion procedure, not client-side rollback detection.

The service-account assertion is about ownership and mode, not about defending
against machine privilege. Whoever can become that account, or root, is outside
this boundary by design.
