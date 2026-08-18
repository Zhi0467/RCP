---
id: S97-a-project-carries-its-identity
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_history.py::test_project_identity_claim_is_visible_idempotent_and_semantically_empty
  - tests/test_history.py::test_adopting_identity_never_mutates_prior_patches_or_research_semantics
  - tests/test_project_identity_catalog.py
  - tests/test_project_identity_chat_continuity.py
  - tests/test_attachments.py
  - tests/test_setup.py::test_connect_requires_confirmation_and_names_the_sole_writable_home
  - tests/test_identity_api.py::test_old_project_url_alias_is_canonicalized_for_tasks_chat_and_paper
  - web/tests/projectHistory.test.mjs
  - browser 2026-08-11 — isolated adopted project, identity-only Overview, Project revisions
  - live SSH 2026-08-11 — exact pre-adoption chat stage reused on tianhaowang-gpu0.ucsd.edu
last_passed: 2026-08-11 — the served legacy-project flow passed with clean
  browser and server logs; a live SSH probe on tianhaowang-gpu0.ucsd.edu then
  rekeyed its local task ledger, attached the exact pre-adoption remote chat
  stage, recovered its marker, and removed the isolated probe stage cleanly
invariants: [1, 2, 6]
---

# A project says who it is and where it belongs

This scenario was confirmed by the human on 2026-08-11. On 2026-08-11 the human
also approved `system` as the reserved producer for RCP-owned identity and
migration revisions; an automatic adoption never pretends to be a human
research edit or an agent result.

Today a `project_id` is derived from the project's name, state host, and
repository path, and lives only in the catalog that minted it. That is enough
while there is one catalog. It is not enough once a project can move between
spaces, or once two catalogs can independently derive the same id for two
separately writable copies.

The project therefore carries a nameplate in canonical Patch history: one
durable `project_id` and its current `home_space_id`. Project creation and
legacy adoption are ordinary visible canonical revisions. A later transfer
changes the home through another visible revision; it never changes the project
id.

This is a nameplate, not version control. There is no fork, branch, or merge.

## Setup

Use four throwaway cases:

1. a new project in one space;
2. a legacy project already registered in that space before the identity
   migration;
3. an untagged legacy repository discovered later through **Add project**; and
4. a second space attempting to add a repository already claimed by the first.

The legacy projects have existing Patch history but no canonical project id or
home. One has operational rows keyed by the old derived catalog id.

## Drive

1. Create the new project and read its first History entry and materialized
   identity.
2. Restart the upgraded app containing the already-registered legacy project.
   Open it, inspect the automatic adoption entry, then open it again.
3. Compare every pre-adoption Patch byte-for-byte and compare the research graph
   before and after adoption.
4. Add the separately discovered legacy repository. Read the confirmation that
   the current space will become its sole writable home, then accept it.
5. Rename a project, change its state host, move its repository path, remove it
   from the catalog, and add it again. Re-read its identity.
6. In the second space, attempt to add the already claimed repository.
7. Materialize that repository with the low-level replay path outside the
   ordinary project catalog.
8. Open an old URL using the derived catalog id and read a task, chat context,
   and paper draft created under that id.
9. Across adoption, continue and recover an existing local and remote native
   chat, and send or remove an attachment uploaded before the id changed.

## Assert

- `project_creation_is_the_first_visible_canonical_revision`
- `project_identity_revisions_are_produced_by_reserved_system_authority`
- `identity_patch_summaries_do_not_bake_the_home_space_id_into_portable_history`
- `project_id_is_minted_once_and_is_not_derived_from_name_host_or_path`
- `home_space_id_is_recorded_in_the_same_identity_revision`
- `an_already_registered_legacy_project_is_adopted_automatically`
- `legacy_adoption_is_one_visible_revision_and_is_idempotent`
- `adoption_leaves_every_older_patch_byte_identical`
- `adoption_changes_no_research_graph_semantics`
- `adding_a_newly_discovered_legacy_project_names_the_home_claim_before_confirmation`
- `the_first_successful_canonical_claim_wins`
- `identity_survives_rename_host_change_path_change_and_reregistration`
- `a_wrong_space_refuses_registration_and_creates_no_catalog_row`
- `a_wrong_space_refuses_every_project_write`
- `wrong_space_refusal_names_the_owning_space`
- `low_level_replay_materializes_regardless_of_home_space`
- `replay_never_loads_space_identity_or_permission_data`
- `the_legacy_derived_id_remains_resolvable_for_one_release`
- `old_urls_tasks_chats_and_drafts_survive_the_identity_migration`
- `native_chat_workspaces_and_temporary_attachments_survive_the_identity_migration`
- `an_interrupted_display_cache_migration_converges_on_restart`

## UI path

**History** shows project creation and adoption in the same revision list as
other canonical changes. The entries say **Project created in _space_** and
**Project identity adopted in _space_**; they are not hidden setup metadata and
do not claim that the research graph changed.

When **Add project** finds an untagged legacy repository, its final confirmation
states that the active space will become the project's sole writable home.
Cancelling writes nothing. An already-registered project needs no new prompt
during upgrade because its existing registration is the prior human choice.

When another space already owns the repository, Add project refuses, names the
known owning space or its durable id, and creates no project card. There is no
ordinary read-only catalog mode.

## Boundary

There is **no fork action**. A wrong-space backend does not mint a second
identity, bless a divergent copy, or offer a choice. Separating a copy
deliberately remains an unbuilt console recovery operation.

Low-level replay remains home-agnostic for recovery and forensics. That does not
create a product-level read-only project: the ordinary app refuses registration
and all writes in the wrong space.
