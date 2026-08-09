---
id: S99-attribution-travels-with-history
status: pending — not human-confirmed
tier: hermetic
driver: pytest
covered_by: none
invariants: [1, 2, 3]
---

# History says who did it, and still says so somewhere else

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Identity, permissions, and agent profiles](../design/identity-permissions-and-agent-profiles.md#provenance).

Canonical history can currently record one thing about a human change:
`"human"`. In a five-person lab that is the entire attribution — nobody can tell
who chose a Decision, approved a Proposal, or pressed Sync. That breaks on day
one of a team space, before any project moves anywhere.

Attribution goes into the patch envelope rather than only into task receipts
because receipts live in SQLite, and SQLite does not travel with a project.
Opaque ids alone would not survive either: after a transfer, a `user_id` from
another space is a meaningless string that *looks* like information. So the
envelope carries a display name snapshotted at append time.

## Setup

A team space with two members, a project both belong to, and existing canonical
history written before this change. A personal space with one project, for the
transfer half.

## Drive — proposal

1. As the first member, choose a Decision option and Sync. As the second, judge
   a Proposal. Read both patch envelopes.
2. Run an ordinary agent task that produces a patch, and an orchestrator
   campaign turn. Read those envelopes.
3. Read the materialized node fields those patches produced.
4. Replay the whole project from history with the member records deleted from
   SQLite.
5. Read the pre-change patches and the history view that renders them.
6. Change the second member's display name, then re-read the earlier patches.
7. Author history in a personal space, transfer that project into the team
   space, and read the pre-transfer patches from the team space.

## Assert

- `a_human_authorized_patch_records_space_id_user_id_and_display_name`
- `an_agent_patch_records_its_profile_and_task_and_campaign_ids`
- `display_name_is_a_snapshot_and_is_never_re_resolved`
- `renaming_a_member_does_not_change_earlier_patches`
- `execution_details_stay_in_receipts_and_not_in_the_envelope`
- `legacy_author_field_is_retained_and_unchanged`
- `materialized_created_by_keeps_its_human_or_agent_values`
- `a_patch_without_attribution_materializes_exactly_as_before`
- `pre_change_history_renders_as_unattributed_not_as_a_guess`
- `replay_succeeds_with_no_user_records_present`
- `a_personal_space_patch_carries_its_space_id_and_owner_display_name`
- `attribution_authored_before_a_transfer_stays_legible_afterward`

## Boundary

The change is **purely additive**. No historical patch is rewritten, no existing
field changes meaning, and `created_by` keeps its `"human" | "agent"` values so
an older client cannot render a person's name where it expects a role word.

A name written into append-only history can never be removed or corrected, by
anyone, including that person. This is the same property a commit authorship
line has, and it is accepted deliberately rather than inherited by accident. The
alternative — ids only, resolved live — would keep names correctable at the cost
of the record going blank the moment the project leaves the space.

This is a stored-graph schema change, not a team API change.

Which lineage the *receipts* must carry, and their final immutable schema,
remain to be settled; this scenario fixes only the envelope.
