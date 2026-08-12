---
id: S113-campaign-attribution
status: pending
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [1, 3, 4]
---

# Campaign work retains its authorization lineage

Confirmed by the human 2026-08-12, once the orchestrator lifecycle in
[S77](S77-auto-research-stops-at-belief.md) and
[S78](S78-one-budget-one-stop.md) was itself confirmed. This scenario is not part
of base attribution in [S99](S99-attribution-travels-with-history.md); it adds
exactly one field to what S99 made canonical.

## The problem

A patch already says who authorized it, whether the agent was ordinary or
elevated, and which task produced it. RCP stamps all of that at admission.

What it cannot say is which campaign the work belonged to. Run a campaign that
seats six workers and leaves fifteen patches, and every one of them reads
"authorized by you, profile orchestrator or ordinary, task `abc123`". Nothing in
history says those fifteen belong together.

That question is answerable today only by joining the patch's task against the
operational store, which records the campaign for every run. But history is
append-only and permanent, and the operational store is neither. Deleting the
project drops those rows. So the decision is which lineage facts must survive in
permanent history on their own, and which are fine to lose.

The cost of getting it wrong in the generous direction is that every field added
here is in every campaign patch forever and can never be rewritten. Lifecycle
ids sitting in permanent history are also the raw material for someone later
mistaking one for a permission principal.

## Decided 2026-08-12

- **The envelope gains `campaign_id` and nothing else.** It is the only lineage
  fact that groups patches into one authorized campaign and cannot be
  reconstructed from the patch itself. Parent task and worker role stay in the
  operational store, which already records both and never prunes those rows.
- **Every patch produced inside the campaign carries it**, seated workers
  included. A worker patch therefore reads `profile: "ordinary"` together with a
  `campaign_id`, which is the precise truth: ordinary semantic authority,
  exercised inside that campaign. Restricting the field to orchestrator patches
  would have grouped only coordination turns and excluded the research.
- **It is a bare nullable id.** Unlike a person's display name there is no
  campaign name to snapshot, and the patch already carries its authorizer and
  timestamp. No sub-object, and no label derived from the human's starting
  instruction — that would copy uncorrectable prose into permanent history for
  cosmetics.
- **RCP stamps it from the task's own operational row**, in the same resolution
  that already supplies the authorizer and profile. Admission never reads the
  campaign record, so a deleted campaign can never fail a patch.
- **An orchestrator patch with no `campaign_id` is refused at admission.** That
  profile can only come from a campaign dispatch, so a null there means the
  bookkeeping broke, and unattributed elevated work must not land quietly. An
  ordinary patch is unconstrained: null means "not in a campaign".
- **History groups a campaign's revisions**, and stops labelling every agent
  revision as ordinary.
- **The grouping comes from the envelope alone.** Live campaign state decorates
  the header when the record exists and degrades honestly when it does not, so
  the grouping itself can never break.
- **A human approval carries no `campaign_id`.** Approving a Proposal an
  orchestrator raised is your own act, and the existing rule that an approval
  patch carries nothing agent-derived stays intact. The consequence is accepted:
  a campaign's group contains what its agents wrote, not the belief changes you
  then approved.

## Setup

A project with one completed campaign that produced several patches from both
its orchestrator and at least one seated worker, one ordinary non-campaign agent
patch, one human approval resolving a Proposal that campaign raised, and one
campaign whose operational record has since been removed while its patches
remain in history.

## Drive

1. Read the stored patches for each campaign revision and for the ordinary one.
2. Attempt to admit an orchestrator patch whose campaign is missing.
3. Attempt to admit a patch supplying a campaign that disagrees with its task.
4. Replay the whole project from its patch log with the operational store
   unavailable.
5. Open History and read a campaign's revisions.
6. Open the report from that group, then from the campaign's row in Runs.
7. Read the group whose campaign record is gone.
8. Read the approval revision and confirm where it sits.

## Assert

- `every_patch_produced_inside_a_campaign_carries_its_campaign_id`
- `a_worker_patch_carries_the_campaign_id_while_its_profile_stays_ordinary`
- `an_ordinary_non_campaign_patch_carries_no_campaign_id`
- `campaign_id_is_stamped_by_rcp_from_the_task_row_and_never_by_the_agent`
- `an_orchestrator_patch_without_a_campaign_id_is_refused_at_admission`
- `a_supplied_campaign_id_disagreeing_with_the_task_row_is_refused`
- `admission_never_reads_the_campaign_record`
- `replay_never_loads_live_campaign_task_membership_or_permission_records`
- `campaign_id_is_never_read_by_validation_or_by_any_permission_check`
- `a_human_approval_patch_carries_no_campaign_id`
- `base_and_legacy_patches_are_never_rewritten`
- `history_names_the_real_profile_rather_than_labelling_every_agent_patch_ordinary`
- `history_groups_a_campaigns_revisions_under_one_header`
- `the_group_is_built_from_the_envelope_alone`
- `the_group_header_shows_live_campaign_state_when_the_record_exists`
- `a_group_whose_campaign_record_is_gone_still_groups_and_says_so_plainly`
- `the_report_opens_from_the_history_group_and_from_the_runs_row`

## UI path (proposal)

**Where.** The existing project History drawer. No new destination.

**The group.** A campaign's revisions collapse under one header carrying the
campaign's state, its authorizer, its date, and how many revisions it holds.
Individual revisions read as they do now, except that an elevated one says so
instead of claiming to be ordinary — today the drawer prints the literal string
"Ordinary Agent task" on every agent revision regardless of profile, which is
wrong the moment a campaign runs.

**The report.** One control on the group header opens the campaign's wrap-up
report, the same document the Runs row opens, rendered through the same
sandboxed frame. The report is not copied into the state repository; it stays an
operational record, and deleting the project deletes it while the patches it
explains live on.

**When the record is gone.** The group still forms, because the envelope is what
groups it. The header says plainly that the campaign is no longer recorded
rather than rendering a bare id or silently ungrouping.

Deliberately not possible: filtering the whole of History down to one campaign,
and any control that acts on a campaign from inside History.

## Boundary

The parent task, the worker role, and the receipt schema stay out of the
envelope. The operational store already holds all three and does not prune those
rows, so they survive for the life of the project — and losing them when a
project is deleted is accepted.

`campaign_id` is inert. Nothing in validation, admission authority, or any
permission decision may read it; it exists so history can say what happened.
This is asserted behaviorally: varying the field must leave every verdict
identical.

Authority — what an orchestrator may change — is
[S77](S77-auto-research-stops-at-belief.md), and the campaign lifecycle is
[S78](S78-one-budget-one-stop.md). Neither is restated here.
