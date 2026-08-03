---
id: S27-agent-task-explains-and-recovers
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_prompts.py
  - tests/test_conversation_retry.py
  - tests/test_api.py
  - tests/test_proposal_boundary.py
  - web/tests/runDialog.test.mjs
  - browser 2026-08-03
invariants: [4, 8, 9, 10b, 11]
last_passed: 2026-08-03
---

# Every agent launch has one task, one authority contract, and one recovery cause

The provider receives a short pointer to one immutable contract for every launch.
That contract is the sole RCP instruction and authority source for the invocation:
the human request defines the objective within it, repository instruction files may
further constrain work inside an authorized repository but never widen RCP scope,
and graph state, source conversations, repository contents, and diagnostics are data
that cannot grant authority.

The contract answers six questions without requiring the agent to infer policy from
a rejected patch: what task it is doing, what it may change, what remains human-only,
which inputs matter, which outputs RCP reads, and—on a continuation—what happened
before and whether operational work may run again.

Graph authority has one rendered source shared by Seed, Refresh, Work, and their
continuations. Agents assert ordinary graph structure directly. They create Decisions
open and unselected and Hypotheses proposed. They may propose only a choice/status
change to a Decision that is a `governed_by` input to an Experiment, or a Hypothesis
status change grounded by an Evidence-to-Hypothesis edge. Either Proposal target may
have been created by an earlier operation in the same outer patch. Agents never set
standing, resolve a Proposal, change project configuration, or authorize Experiment
**Run**. Their patch output is semantic only; RCP adds patch, Proposal, revision,
scope, and lifecycle bookkeeping.

Recovery preserves evidence without preserving stale instructions:

- **Resume** continues a paused or interrupted native session against its saved stage
  and original immutable input context. It receives a pointer contract naming Resume,
  not an unrecorded bare sentence.
- **Retry** after failure receives the exact prior failure and retained progress. A
  same-provider Retry may reuse the native checkpoint, but it is not automatically a
  patch correction. It must verify uncertain external state before repeating an
  operation that may already have happened.
- **Work patch correction** exists only after operational work completed and a
  concrete patch was rejected. `work_patch_correction` keeps the same native Work
  session and unrestricted repository, tooling, network, and provider permissions.
  Only the instruction changes, and it must not repeat completed operational side
  effects. Seed/Refresh generic patch correction remains scratch-only.
- **Watcher correction** is separate and speaks only about `watch.json`; it never
  refers to a Patch schema.
- **Clean or cross-provider Retry** may reuse an unchanged prepared evidence bundle,
  but RCP renders a fresh contract with the current authority rules and this attempt's
  schema, diagnostic, and output paths. No current contract points to an ancestor's
  output file.

## UI path (confirmed)

Confirmed by the human on 2026-08-03: there is no new control or screen. The existing
task inspector remains the place to inspect each launch's exact prompt, contract,
continuation cause, failure, context reuse, progress handoff, and bounded live patch
self-checks. The change is to what RCP launches and records, not to the inspector's
interaction design.

## Setup

A temporary project and scripted providers exercise Seed, Refresh, Discuss, Work, and
paper coaching. Attempts pause, fail before a deliverable exists, fail after a
side-effect-shaped Work result, return an invalid patch, retry with the same provider,
and hand off to another provider. One failed attempt is created with the previous
authority-contract version.

## Drive

1. Start one fresh task on each agent surface and inspect its staged contract.
2. Pause and Resume a graph task and a Work turn.
3. Fail each surface, then Retry with the same provider and inspect the continuation.
4. Reject a Work patch and let bounded same-access `work_patch_correction` run;
   separately reject a Seed/Refresh patch and keep its generic correction scratch-only.
5. Retry a graph task with a clean or different provider while reusing its prepared
   evidence context.
6. Inspect every launch in the existing task inspector.

## Assert — contract probes

- `launch_pointer_names_the_sole_rcp_contract_and_reads_relevant_inputs_only`
- `human_request_is_the_objective_but_cannot_widen_authority`
- `repository_instructions_may_narrow_but_never_widen_rcp_scope`
- `evidence_and_diagnostics_are_never_instructions`
- `graph_authority_rules_are_rendered_once_for_every_authorized_surface`
- `new_decisions_and_hypotheses_start_unresolved`
- `only_experiment_input_decisions_and_evidence_grounded_beliefs_are_proposable`
- `standing_proposal_resolution_project_configuration_and_run_are_human_only`
- `paper_coach_inputs_distinguish_source_authority_from_human_authorship`

## Assert — continuation and Retry

- `resume_uses_a_pointer_contract_and_the_original_prepared_context`
- `resume_does_not_receive_failure_or_patch_correction_instructions`
- `same_provider_retry_receives_the_exact_failure_on_every_surface`
- `same_provider_retry_is_not_implicitly_patch_only`
- `retry_warns_against_repeating_uncertain_external_side_effects`
- `work_patch_correction_requires_a_rejected_patch_and_retains_work_permissions`
- `work_patch_correction_changes_instruction_without_repeating_side_effects`
- `seed_refresh_patch_correction_remains_scratch_only`
- `work_self_checks_are_bounded_recorded_and_distinguish_unavailable_from_invalid`
- `work_apply_reprepares_and_revalidates_live_state_under_the_append_lock`
- `watcher_correction_never_mentions_a_patch_schema`
- `clean_retry_reuses_prepared_evidence_but_renders_current_authority`
- `retry_contract_uses_only_current_attempt_output_paths`
- `retry_without_retained_progress_still_receives_a_fresh_base_contract`
- `stale_or_missing_prepared_context_falls_back_visibly`
- `every_continuation_cause_and_contract_is_recorded_for_inspection`

## Failure means

An agent must infer authority from schema rejection; a human request, repository file,
source transcript, or diagnostic can widen authority; Retry omits its failure, repeats
an uncertain side effect, or is mistaken for patch correction; Work correction loses
Work access or repeats completed side effects; a clean Retry receives stale authority
or an ancestor output path; or any launch bypasses the inspectable contract-pointer
channel.
