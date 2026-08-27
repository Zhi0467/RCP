---
id: S98-move-a-project-into-a-team-space
status: pending
tier: live
driver: pytest + browser + desktop + ssh
covered_by: none
invariants: [1, 6]
---

# Hand a personal project over to the lab, once

This scenario is human-confirmed and pending implementation. Its boundaries are
in [Project identity and home](../specs/projects-spaces-and-operations.md#project-identity-and-home)
and [Personal-to-team transfer archive](../specs/projects-spaces-and-operations.md#personal-to-team-transfer-archive).

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
execution account. The personal project has terminal tasks, chats, durable
attachments, Paper history, and stopped episodes/watchers/reports to transfer.

## Drive

1. Open the project in the personal space, choose **Move to team space**, and
   select a saved team connection.
2. Inspect the durable target provisioning request and its intended central
   paths before any project authority changes.
3. Run `rcp server project provision <request-id>`. Complete the deploy-key
   write check, central checkout preparation, and provider/execution readiness.
4. At **ready for review**, read the final confirmation, then confirm and watch
   source work settle.
5. Interrupt once after the source is fenced but before target registration;
   resume the same request and finish it.
6. In the team space, open the project, read its graph and revision history, and
   start a task from the central checkout.
7. In the personal space, attempt to register or write the old checkout.
8. Inspect the canonical home-change history, both catalog records, central
   checkout ownership, and unchanged ownership of the person's checkout.
9. Inspect the versioned archive and imported finished operational history;
   attempt to Resume/Retry it or reach its former provider sessions and stages.
10. Attempt to move a team project back to the personal space and to a second
    team space.

## Assert

- `transfer_appends_a_home_change_to_canonical_history`
- `project_id_is_unchanged_by_transfer`
- `the_team_uses_a_separate_central_checkout_set_on_the_declared_accounts`
- `the_persons_checkout_keeps_its_path_and_owner`
- `the_confirmation_names_the_source_and_target_directories`
- `the_confirmation_names_the_active_work_that_will_be_settled`
- `the_source_is_fenced_before_the_target_becomes_writable`
- `interruption_can_leave_zero_writers_but_never_two_writers`
- `an_interrupted_transfer_resumes_from_its_durable_request`
- `the_source_space_can_no_longer_write_the_project`
- `history_authored_before_the_transfer_remains_readable_and_attributed`
- `execution_configuration_must_be_re_established_in_the_target_space`
- `personal_git_provider_ssh_and_machine_credentials_do_not_carry_over`
- `one_versioned_checksummed_archive_is_the_only_transfer_format`
- `all_finished_human_visible_history_and_kept_artifacts_transfer`
- `source_session_stage_machine_scratch_cache_and_credentials_do_not_transfer`
- `imported_history_cannot_resume_or_retry_through_source_execution_bindings`
- `the_target_activates_only_after_database_and_file_readback`
- `team_to_personal_transfer_is_not_offered`
- `team_to_team_transfer_is_not_offered`

## UI path

**Move to team space** lives in Project Settings in the personal space, next to
the project's home information. Choosing it first creates the target's durable
provisioning request. Its preparation screen states, in plain language:

- which team space will own the project;
- the personal source paths that remain owned by the person;
- the new central checkout paths and their owning execution accounts;
- what active work will be settled first; and
- that execution settings must be chosen again in the team space.

The human cannot confirm until server preparation is **ready for review**. After
confirmation the project disappears from the personal index and appears in the
team space's list. The old entry is not left behind as a broken row, but the old
checkout remains an ordinary personal working copy.

Deliberately not possible: moving a project out of a team space, moving one
between team spaces, and confirming without having been shown the directory
list.

The final review names that all finished task, chat/artifact, Paper, and stopped
episode/watcher/report history will transfer as non-resumable history. It also
states that provider sessions, active work, scratch, caches, credentials, and
machine configuration will not transfer.

## Boundary

Releasing a project *from* a team space remains outside this first lab-server
slice. It is kept off the product surface because with equal members any single
person could otherwise pull a shared project private unilaterally, and the fix
for that would be a rank the design refuses.

Transfer is a durable cross-space sequence rather than one impossible distributed
transaction. Its recovery rule is fail closed: after the source home changes,
the target request must finish or stay visibly repairable; the source never
reopens admission as a fallback. The desktop coordinates the two spaces and may
invoke the target CLI over SSH, but both backends and the CLI publish the durable
truth the UI renders.
