---
id: S99-attribution-travels-with-history
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_identity_patch_contract.py
  - tests/test_history_attribution.py
  - tests/test_identity_api.py::test_personal_sync_and_task_records_keep_immutable_identity_snapshots
  - tests/test_identity_api.py::test_team_sync_and_tasks_use_only_current_trusted_member_snapshot
  - tests/test_identity_api.py::test_resume_retry_and_repair_capture_the_current_actor_instead_of_parent
  - tests/test_identity_api.py::test_reopened_poller_terminalizes_legacy_watcher_once_without_wake
  - web/tests/projectHistory.test.mjs
  - browser 2026-08-11 — isolated identity prompt, exact task retry, Project revisions
last_passed: 2026-08-11 — strict admission, replay, API, task, and UI checks
  preserved immutable authorization snapshots; the served flow retried the exact
  named task and rendered system history without browser or server errors
invariants: [1, 2, 3]
---

# History says who authorized a change

This base-attribution scenario was confirmed by the human on 2026-08-11.
Episode and orchestrator lineage moved to S113 and is deliberately not implied
here.

Canonical history currently records only `"human"` or `"agent"`. A team needs
to know which person authorized a human or ordinary-agent change even after the
project leaves that space. Each new Patch therefore carries an additive,
snapshotted attribution block owned by RCP rather than supplied by the agent or
request body.

A human Patch records the authorizing `space_id`, durable `user_id`, and display
name as it existed when the Patch was appended. An ordinary-agent Patch records
that same `authorized_by` block, `profile="ordinary"`, and the direct task id.
It carries no episode id unless that task is actually bound to an episode.

## Setup

A team space with two durable human identities who share one project; a personal
space whose local owner has chosen an explicit RCP display name; existing
canonical history written before attribution fields existed; and an ordinary
agent task that produces a Patch.

## Drive

1. As the first team member, choose a Decision option and Sync. As the second,
   judge a Proposal. Read both Patch envelopes and History entries.
2. Run the ordinary agent task and read its Patch envelope.
3. Read the materialized nodes produced by those Patches.
4. Replay the project after deleting every user record from the operational
   database.
5. Read pre-attribution history in History.
6. Rename the second member, make one new change, and compare both old and new
   entries.
7. Make one human and one ordinary-agent change in the personal space.
8. Reopen an upgraded space with a ready watcher whose legacy origin task has
   no durable human-attribution snapshot, then poll twice.

## Assert

- `a_human_patch_snapshots_space_user_and_display_name_at_append`
- `an_ordinary_agent_patch_records_its_authorizer_profile_and_direct_task_id`
- `an_agent_cannot_supply_or_replace_canonical_attribution`
- `a_request_body_cannot_choose_another_users_identity`
- `renaming_a_member_changes_only_future_patch_snapshots`
- `execution_details_other_than_direct_task_identity_stay_in_receipts`
- `legacy_author_field_is_retained_and_unchanged`
- `materialized_created_by_keeps_its_human_or_agent_values`
- `a_patch_without_attribution_materializes_exactly_as_before`
- `legacy_history_is_rendered_as_unattributed_not_as_a_guess`
- `replay_succeeds_with_no_user_records_present`
- `a_personal_space_patch_uses_its_durable_local_owner_identity`
- `an_unattributable_legacy_watcher_stops_once_with_a_durable_diagnostic`
- `no_base_attribution_claims_an_orchestrator_or_campaign`

## UI path

History shows the stored display-name snapshot beside each new human or ordinary
agent revision. Ordinary-agent entries also show that an ordinary task made the
change without turning its task id into the primary label.

Changing a member's current name does not repaint earlier entries. A legacy
entry keeps its existing Human or Agent role and adds **Unattributed**; RCP does
not invent a person.

Before the first newly attributed write in a personal space, RCP asks once for
an explicit local display name. Reading, Discuss, and the paper coach remain
available before it is supplied. RCP never guesses from the operating-system
account name.

## Boundary

The change is purely additive. No historical Patch is rewritten and
`created_by` keeps its existing `"human" | "agent"` meaning. Display-name
snapshots are immutable even if the current member record is renamed or removed.

Episode id, parent/worker lineage, and the final immutable receipt schema belong
to S113 after S77 and S78. Current Experiment-loop tasks remain ordinary-agent
attribution.

**The orchestrator profile value landed here, additively, on 2026-08-12** — it is
no longer deferred. `profile` is an already-canonical S99 field, and a campaign
turn has to be able to name itself truthfully rather than sign its work
`"ordinary"`. RCP sets the field, never the agent, so the extra value opens no
forgery surface. What stayed deferred is what matters under invariant 1: campaign,
parent, and worker lineage are absent from the Patch envelope entirely and live in
operational storage, so nothing S113 has yet to decide is being written
irreversibly into history.
