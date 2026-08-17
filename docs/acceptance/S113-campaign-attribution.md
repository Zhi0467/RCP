---
id: S113-campaign-attribution
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_history_attribution.py
  - tests/test_episode_history_api.py
  - tests/test_dispatch_authority.py::test_agent_task_authority_carries_episode_id_from_each_exact_task_row
  - tests/test_identity_patch_contract.py::test_base_attribution_is_strict_additive_with_nullable_episode_lineage
  - web/tests/projectHistory.test.mjs
invariants: [1, 3, 4]
last_checked: 2026-08-15 — the envelope field, admission refusals, legacy
  `campaign_id` replay decode, and History grouping are covered by the backend
  and web suites. The History drawer half is undriven and stays that way by
  decision, because no hermetic fixture emits an episode-attributed graph Patch
  and a browser drive would need a new acceptance fixture first.
---

# Episode work retains its authorization lineage

Confirmed by the human 2026-08-12, once the orchestrator lifecycle in
[S77](S77-auto-research-stops-at-belief.md) and
[S78](S78-one-budget-one-stop.md) was itself confirmed, and was generalized to
both episode modes in [S120](S120-episodes-wrap-up-with-a-visual-report.md).
This scenario is not part of base attribution in
[S99](S99-attribution-travels-with-history.md); it adds exactly one field to
what S99 made canonical.

## The problem

A patch already says who authorized it, whether the agent was ordinary or
elevated, and which task produced it. RCP stamps all of that at admission.

What it cannot say is which episode the work belonged to. Run Auto-research that
seats six workers or a bounded Experiment loop that leaves several patches, and
every one reads
"authorized by you, profile orchestrator or ordinary, task `abc123`". Nothing in
history says which bounded episode those revisions belong to.

That question is answerable today only by joining the patch's task against the
operational store, which records the episode for every run. But history is
append-only and permanent, and the operational store is neither. Deleting the
project drops those rows. So the decision is which lineage facts must survive in
permanent history on their own, and which are fine to lose.

The cost of getting it wrong in the generous direction is that every field added
here is in every episode patch forever and can never be rewritten. Lifecycle
ids sitting in permanent history are also the raw material for someone later
mistaking one for a permission principal.

## Decided 2026-08-12

- **The envelope gains `episode_id` and nothing else.** It is the only lineage
  fact that groups patches into one authorized episode and cannot be
  reconstructed from the patch itself. Parent task and worker role stay in the
  operational store, which already records both and never prunes those rows.
- **Every patch produced inside either episode mode carries it**, seated workers
  included. A worker patch therefore reads `profile: "ordinary"` together with an
  `episode_id`, which is the precise truth: ordinary semantic authority,
  exercised inside that episode. Restricting the field to orchestrator patches
  would have grouped only coordination turns and excluded the research.
- **It is a bare nullable id.** Unlike a person's display name there is no
  episode name to snapshot, and the patch already carries its authorizer and
  timestamp. No sub-object, and no label derived from the human's starting
  instruction — that would copy uncorrectable prose into permanent history for
  cosmetics.
- **RCP stamps it from the task's own operational row**, in the same resolution
  that already supplies the authorizer and profile. Admission never reads the
  episode parent, so a deleted episode can never fail a patch.
- **Any episode-bound patch with no `episode_id` is refused at admission.** An
  orchestrator profile can only come from an Auto-research episode, and an
  Experiment-loop task carries the same immutable scope binding, so a null means
  the bookkeeping broke, and unattributed elevated work must not land quietly. An
  ordinary non-episode patch remains null.
- **History groups an episode's revisions**, and stops labelling every agent
  revision as ordinary.
- **The grouping comes from the envelope alone.** Live episode state decorates
  the header when the record exists and degrades honestly when it does not, so
  the grouping itself can never break.
- **A human approval carries no `episode_id`.** Approving a Proposal an
  orchestrator raised is your own act, and the existing rule that an approval
  patch carries nothing agent-derived stays intact. The consequence is accepted:
  an episode's group contains what its agents wrote, not the belief changes you
  then approved.
- **Legacy append-only Patches are decoded one way.** A stored `campaign_id` is
  read in memory as `episode_id`, but files are never rewritten and live input
  supplying the retired key is rejected. Every new Patch writes only
  `episode_id`.

## Setup

A project with one completed Auto-research episode that produced patches from
its orchestrator and a seated worker, one bounded Experiment episode with
several patches, one ordinary non-episode agent patch, one human approval
resolving a Proposal an episode raised, and one episode whose operational record
has since been removed while its patches remain in history.

## Drive

1. Read the stored patches for each episode revision and for the ordinary one.
2. Attempt to admit an episode-bound patch whose episode id is missing.
3. Attempt to admit a patch supplying an episode that disagrees with its task.
4. Replay the whole project from its patch log with the operational store
   unavailable.
5. Open History and read both modes' episode revisions.
6. Open a report from that group, then from the episode's row in Runs.
7. Read the group whose episode record is gone.
8. Read the approval revision and confirm where it sits.

## Assert

- `every_patch_produced_inside_an_episode_carries_its_episode_id`
- `auto_research_and_experiment_patches_share_the_same_episode_lineage_field`
- `a_worker_patch_carries_the_episode_id_while_its_profile_stays_ordinary`
- `an_ordinary_non_episode_patch_carries_no_episode_id`
- `episode_id_is_stamped_by_rcp_from_the_task_row_and_never_by_the_agent`
- `an_episode_bound_patch_without_an_episode_id_is_refused_at_admission`
- `a_supplied_episode_id_disagreeing_with_the_task_row_is_refused`
- `admission_never_reads_the_episode_parent`
- `replay_never_loads_live_episode_task_membership_or_permission_records`
- `episode_id_is_never_read_by_validation_or_by_any_permission_check`
- `a_human_approval_patch_carries_no_episode_id`
- `legacy_campaign_id_decodes_only_inside_append_only_history_replay`
- `new_patch_bytes_never_emit_campaign_id`
- `base_and_legacy_patches_are_never_rewritten`
- `history_names_the_real_profile_rather_than_labelling_every_agent_patch_ordinary`
- `history_groups_an_episodes_revisions_under_one_header`
- `the_group_is_built_from_the_envelope_alone`
- `the_group_header_shows_live_episode_state_when_the_record_exists`
- `a_group_whose_episode_record_is_gone_still_groups_and_says_so_plainly`
- `the_report_opens_from_the_history_group_and_from_the_runs_row`

## UI path — revised 2026-08-14

**Where.** The existing project History drawer. No new destination.

**The group.** An episode's revisions collapse under one header carrying the
episode's mode and state, its authorizer, its date, and how many revisions it
holds.
Individual revisions read as they do now, except that an elevated one says so
instead of claiming to be ordinary — today the drawer prints the literal string
"Ordinary Agent task" on every agent revision regardless of profile, which is
wrong the moment an episode runs.

**The report.** When one exists, a control on the group header opens the
episode's wrap-up report, the same document the Runs row opens, rendered through the same
sandboxed frame. The report is not copied into the state repository; it stays an
operational record, and deleting the project deletes it while the patches it
explains live on.

**When the record is gone.** The group still forms, because the envelope is what
groups it. The header says plainly that the episode is no longer recorded
rather than rendering a bare id or silently ungrouping.

Deliberately not possible: filtering the whole of History down to one episode,
and any control that acts on an episode from inside History.

## Boundary

The parent task, the worker role, and the receipt schema stay out of the
envelope. The operational store already holds all three and does not prune those
rows, so they survive for the life of the project — and losing them when a
project is deleted is accepted.

`episode_id` is inert. Nothing in validation, admission authority, or any
permission decision may read it; it exists so history can say what happened.
This is asserted behaviorally: varying the field must leave every verdict
identical.

Authority — what an orchestrator may change — is
[S77](S77-auto-research-stops-at-belief.md), and the Auto-research lifecycle is
[S78](S78-one-budget-one-stop.md). Neither is restated here.
