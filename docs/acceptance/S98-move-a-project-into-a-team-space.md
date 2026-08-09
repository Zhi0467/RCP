---
id: S98-move-a-project-into-a-team-space
status: pending — not human-confirmed
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [1, 6]
---

# Hand a personal project over to the lab, once

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Spaces and project homes](../design/spaces-and-project-homes.md#project-transfer-is-one-way).

Transfer is personal space → team space, one way. Files do not move: the
canonical state repository keeps its path. What changes is who may write it, and
who owns the directories on disk.

That ownership handover is the part a person is most likely to be surprised by,
because a directory that was theirs stops being theirs while staying exactly
where it is. So it has to be shown before it happens, not discovered afterward.

## Setup

A personal space with a project whose canonical state repository and one
truth-scope repository are owned by the person, plus an enrolled team space. A
task that has already run in the personal space, so there is task history, chat
history, and attribution to follow.

## Drive — proposal

1. Open the project in the personal space and choose to move it to the team
   space.
2. Read the confirmation screen before confirming.
3. Confirm. Watch active work settle.
4. In the team space, open the project, read its graph and revision history, and
   start a task.
5. In the personal space, attempt to write the project.
6. Read the project's canonical history around the transfer point.
7. Inspect ownership of the canonical state repository and the truth-scope
   repository.
8. Attempt to move a team project back to the personal space, and attempt to
   move it to a second team space.

## Assert

- `transfer_appends_a_home_change_to_canonical_history`
- `project_id_is_unchanged_by_transfer`
- `canonical_state_repository_keeps_its_path`
- `state_and_truth_scope_repository_ownership_passes_to_the_service_account`
- `the_confirmation_names_every_directory_that_changes_hands`
- `the_confirmation_names_the_active_work_that_will_be_settled`
- `the_source_space_can_no_longer_write_the_project`
- `history_authored_before_the_transfer_remains_readable_and_attributed`
- `execution_configuration_must_be_re_established_in_the_target_space`
- `a_personal_provider_login_or_local_machine_definition_does_not_carry_over`
- `team_to_personal_transfer_is_not_offered`
- `team_to_team_transfer_is_not_offered`

## UI path (proposal)

**Move to team space** lives in Project Settings in the personal space, next to
the project's home information. Choosing it opens a confirmation that states, in
plain language:

- which team space will own the project;
- which directories change ownership, by absolute path;
- what active work will be settled first; and
- that execution settings must be chosen again in the team space.

After confirmation the project disappears from the personal index and appears in
the team space's list. The old entry is not left behind as a broken row.

Deliberately not possible: moving a project out of a team space, moving one
between team spaces, and confirming without having been shown the directory
list.

Open for a human answer: what happens to the personal space's task history,
chat history, and paper drafts for that project — carried across in the transfer
envelope, or left behind with a note that they stayed.

## Boundary

Releasing a project *from* a team space is a console operation for whoever
administers the server, and it is unbuilt. It is kept off the product surface
because with equal members any single person could otherwise pull a shared
project private unilaterally, and the fix for that would be a rank the design
refuses.

The browser half of this scenario exists only for the confirmation screen —
whether the human is truthfully told what they are about to give away. Every
mechanical assertion is backend truth and is checked with pytest.
