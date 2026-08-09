---
id: S97-a-project-carries-its-identity
status: pending — not human-confirmed
tier: hermetic
driver: pytest
covered_by: none
invariants: [1, 2, 6]
---

# A project says who it is and where it belongs

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Spaces and project homes](../design/spaces-and-project-homes.md#a-project-carries-its-own-identity).

Today a `project_id` is derived from the project's name, state host, and
repository path, and lives only in the catalog that minted it. That is enough
while there is one catalog. It is not enough once a project can move between
spaces, or once two catalogs can independently derive the same id for two
separately writable copies.

So the project carries a nameplate in its own canonical history: a durable
identity and the one space allowed to write it. Any RCP that opens the
repository can read both.

This is a nameplate, not version control. There is no fork, no branch, and no
merge.

## Setup

Two spaces in throwaway data directories, and a project created in the first
one. Separately, a project whose canonical history predates this change and
carries only the derived id.

## Drive — proposal

1. Create a project and read its canonical history's genesis record.
2. Rename the project, change its state host, and move its repository path.
   Re-read the identity.
3. Remove the project from the catalog and register it again.
4. Register the same repository in the second space and attempt a write.
5. Materialize the graph in the second space without writing.
6. Open the pre-change project and read its graph, revision history, and
   catalog row.
7. Read an operational row keyed on the old derived id — a task, a chat session
   context, a paper draft — after migration.

## Assert

- `project_id_is_minted_once_into_canonical_history_at_creation`
- `project_id_is_random_and_not_derived_from_name_host_or_path`
- `identity_survives_rename_host_change_and_path_change`
- `identity_survives_deregistration_and_re_registration`
- `home_space_id_is_recorded_in_canonical_history`
- `a_backend_refuses_to_write_a_project_whose_home_is_another_space`
- `a_backend_refuses_to_register_a_duplicate_and_names_the_owning_space`
- `replay_materializes_the_graph_regardless_of_home_space`
- `replay_never_loads_identity_or_permission_data`
- `pre_change_history_replays_byte_for_byte_identically`
- `the_legacy_derived_id_remains_resolvable_for_one_release`
- `operational_rows_keyed_on_the_legacy_id_survive_migration`

## Boundary

There is **no fork action**. When a backend meets a repository whose history
names another home space, it refuses and explains; it does not mint a second
identity, bless a divergent copy, or offer a choice. Separating a copy
deliberately is a rare console operation and is unbuilt.

Refusal to write is cooperative where the other backend is a personal space on
someone's own computer that can still reach the files. That is the correct level
given the trust model in
[S95](S95-durable-team-space.md), and this scenario asserts the refusal, not
physical prevention.

Replay reading `home_space_id` must never become replay *enforcing* it. A graph
found on a disk must still materialize.
