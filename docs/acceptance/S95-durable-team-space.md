---
id: S95-durable-team-space
status: pending — not human-confirmed
tier: hermetic
driver: pytest + api
covered_by: none
invariants: [6, 8]
---

# A team space outlives every process that serves it

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in [Spaces and project homes](../design/spaces-and-project-homes.md).

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

## Setup

A team space initialized in a throwaway data directory, with one enrolled
member, one registered project, and a saved client connection recording the
expected `space_id`.

## Drive — proposal

1. Read the `space_id` after first initialization.
2. Stop the backend and start it again. Read the `space_id` and the member
   record.
3. Start it on a different port and address. Reconnect the saved connection.
4. Initialize a *second*, unrelated space in a different data directory and
   serve it at the address the saved connection expects. Attempt a mutation
   through that connection.
5. Inspect the data directory's ownership and mode, and the mode of a locally
   homed canonical state repository.
6. With the backend running, attempt to take the singleton lock from a second
   process against the same data directory.

## Assert

- `space_id_is_generated_once_and_survives_restart`
- `space_id_survives_an_address_and_port_change`
- `restart_preserves_members_and_token_hashes_without_re_enrollment`
- `instance_id_changes_across_process_lifetimes`
- `space_id_is_not_derived_from_the_data_directory_path`
- `a_personal_space_also_mints_a_durable_space_id`
- `an_unexpected_space_id_blocks_mutations_until_the_human_reconnects`
- `an_unexpected_space_id_still_permits_reading_what_the_client_cached`
- `data_directory_and_local_state_repositories_are_owned_only_by_the_service_account`
- `a_second_backend_cannot_serve_the_same_data_directory`

## Boundary

This scenario does not promise that two *restored copies* of one space can
detect each other. That limitation is deliberate and operator-owned; see
[Team server operations](../design/team-server-operations.md#restore-and-the-authority-boundary).
`space_id` surviving a restore is what makes the replacement the same space, and
it is also what makes two simultaneously running copies indistinguishable.

Detecting that a familiar `space_id` has been rolled back to an older archive is
an open question, not a promise here.

The service-account assertion is about ownership and mode, not about defending
against machine privilege. Whoever can become that account, or root, is outside
this boundary by design.
