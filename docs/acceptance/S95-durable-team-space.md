---
id: S95-durable-team-space
status: pending
tier: live
driver: pytest + api + ssh
covered_by: none
invariants: [6, 8]
---

# A team space outlives every process that serves it

This scenario is human-confirmed and pending implementation. Its boundaries are
in [Spaces and durable identity](../specs/projects-spaces-and-operations.md#spaces-and-durable-identity)
and [Confirmed first team-server target](../specs/projects-spaces-and-operations.md#confirmed-first-team-server-target).

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

The first deployment is one source-built Linux server. A dedicated `rcp` account
owns the service, data, and server-local central checkouts; an explicit remote
execution account owns any checkout on its SSH machine. Humans remain distinct
members and do not share either execution login. The installed RCP version is
the server checkout's exact GitHub `main` commit, served without source reload.

## Setup

A Linux test host on which a normal operator has a disposable bootstrap checkout,
plus the separately installed managed source checkout, dedicated `rcp` account,
non-reloading system service, private data directory, one initialized team
space, one enrolled member, one central project checkout, and a saved desktop
connection recording the expected `space_id`.

## Drive

1. Run the first install through the bootstrap checkout's absolute CLI path with
   operator `sudo`. Inspect which steps run as root and which run as `rcp`, then
   remove the bootstrap checkout.
2. Run `rcp server doctor`. Read the service account, data path, source checkout
   commit, running commit, upstream, and reload mode.
3. Read the `space_id` after first initialization.
4. Stop the system service and start it again. Read the `space_id` and member
   record.
5. Change the loopback port, restart, and reconnect the saved connection.
6. Initialize a *second*, unrelated space in a different data directory and
   serve it at the address the saved connection expects. Attempt a mutation
   through that connection.
7. Inspect the data directory, runtime socket, credentials, and central checkout
   ownership and modes. Inspect an ordinary member's independent checkout.
8. With the backend running, attempt to take the singleton lock from a second
   process against the same data directory.

## Assert

- `space_id_is_generated_once_and_survives_restart`
- `space_id_survives_an_address_and_port_change`
- `restart_preserves_members_and_token_hashes_without_re_enrollment`
- `instance_id_changes_across_process_lifetimes`
- `space_id_is_not_derived_from_the_data_directory_path`
- `a_personal_space_also_mints_a_durable_space_id`
- `doctor_reports_the_exact_checkout_and_running_commits`
- `the_operator_bootstrap_checkout_never_becomes_the_managed_checkout`
- `root_performs_only_os_installation_and_managed_source_steps_run_as_rcp`
- `the_team_service_runs_from_source_without_reload`
- `an_unexpected_space_id_blocks_mutations_until_the_human_reconnects`
- `an_unexpected_space_id_still_permits_reading_what_the_client_cached`
- `data_runtime_credentials_and_server_local_checkouts_are_owned_only_by_rcp`
- `remote_checkouts_are_owned_only_by_the_declared_remote_execution_account`
- `a_members_personal_checkout_keeps_its_original_owner`
- `a_second_backend_cannot_serve_the_same_data_directory`

## Boundary

This scenario does not promise that two *restored copies* of one space can
detect each other. That limitation is deliberate and operator-owned under the
[server and machine operations contract](../specs/projects-spaces-and-operations.md#server-and-machine-operations).
`space_id` surviving a restore is what makes the replacement the same space, and
it is also what makes two simultaneously running copies indistinguishable.

Detecting that a familiar `space_id` has been rolled back to an older archive is
an open question, not a promise here.

The service-account assertion is about ownership and mode, not about defending
against machine privilege. Whoever can become that account, or root, is outside
this boundary by design.
