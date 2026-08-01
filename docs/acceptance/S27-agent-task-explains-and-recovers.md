---
id: S27-agent-task-explains-and-recovers
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_launcher.py, tests/test_prompts.py, tests/test_api.py, web/tests/runDialog.test.mjs, browser 2026-07-31
last_passed: 2026-07-31
invariants: [8, 9, 11]
---

# A failed seed tells the truth and another provider continues it

When a provider stops because its session limit was reached, that provider
error is the task error. RCP does not replace it with "patch not written" or
launch patch-correction rounds against an exhausted provider.

Retry follows the failure boundary. With the same provider, a failed attempt
that has a native checkpoint and did not exhaust its session continues that
native session in the same saved stage; a transient network failure does not
discard provider-owned context. Reusing that session does not mean repeating the
previous instruction: a retry after a failure tells the session what the failure
was, so it corrects rather than resubmits. A session-limit failure is never
resumed or sent into patch correction. With another provider, Retry starts a clean native
session but continues the same work. RCP walks the complete attempt lineage: an
intermediate attempt with no native session or useful work cannot hide an older
useful dispatch. Prepared corpus context and provider progress are selected
independently, so the newest valid input bundle may be paired with an older
provider transcript or retained patch before the replacement provider inspects
the original corpus again.

## UI path (proposal)

The existing task inspector shows the exact terminal provider error at the top.
For the observed failure it says that Claude reached its session limit, together
with the reset time Claude reported. The absence of `patch.json` remains a
diagnostic fact, not the cause shown to the human.

The inspector also exposes the exact prompt sent on every provider launch. Each
fresh, handoff, or correction launch prompt is a short pointer envelope to an
immutable task contract. A literal native Resume receives only “Continue the
interrupted task”; its saved session and stage already own that contract and
context. The graph contract names the task purpose, ontology, current graph,
repositories, cursor state, one scoped conversation directory per provider,
edit guardrails, preferences, optional human message, and output requirement. It
never serializes RCP's internal per-session routing objects or JSON Schema into
the launch prompt. Seed and refresh may still recommend provider-owned
specialist fan-out.

**Retry…** opens the run configuration. Choosing Codex creates a linked clean
attempt with a new native session. When the graph revision, run scope, and
prepared-source snapshot still match the failed attempt, RCP reuses that
attempt's prepared input bundle instead of rebuilding all conversation slices
and restaging the same corpus. The new launch receives a prior-attempt handoff
that names the selected earlier provider, terminal condition, native session
transcript, and retained patch or progress artifact if one exists. Its prompt
directs it to read that handoff first, continue the completed analysis, and
return to original sources only for unresolved gaps.

An attempt's contract content is durable RCP data; its execution-host path is
not. Every new attempt re-stages the immutable contract into its own scratch
folder and launches with a three-line pointer to that new path. A retry never
depends on an ancestor `/tmp/rcp-run.*` contract path surviving retention or
successful cleanup.

The new attempt's inspector separately records whether it reused prepared
context and whether it handed off provider progress, including the concrete
reason when either is unavailable. If the revision, scope, source snapshot,
retained data, or native transcript no longer matches, RCP performs an ordinary
full retry and says explicitly that no prior progress was handed off.

Leaving the provider unchanged after a non-limit failure uses the saved native
checkpoint and stage. If either is unavailable, RCP says so and performs a
clean retry. Leaving an exhausted provider selected creates a clean attempt
rather than resuming the quota-blocked session.

## Setup

A temporary unseeded project and two scripted providers; no real provider or
quota is used. Scripted Claude reads a known part of the seed corpus and leaves
an identifiable progress checkpoint in its persisted native-session record,
then emits Claude's synthetic session-limit message, exits successfully, and
writes no patch. Scripted Codex writes a valid seed patch only after following
the prior-attempt handoff and recovering that checkpoint.

## Drive

1. Start the seed with Claude and let it reach the scripted session limit.
2. Open the failed task inspector and read the terminal error.
3. Choose **Retry…**, switch the provider to Codex, and start the linked attempt.
4. Let Codex continue from Claude's retained progress and finish the seed.
5. Inspect the completed attempt and resulting graph revision.

## Assert

- `session_limit_is_the_visible_terminal_error`
- `missing_patch_does_not_replace_the_provider_error`
- `quota_failure_does_not_launch_patch_correction`
- `every_provider_launch_prompt_is_visible_verbatim`
- `launch_prompts_are_under_200_lines`
- `launch_prompts_contain_pointers_not_serialized_payloads`
- `one_scoped_conversation_directory_is_exposed_per_provider`
- `conversation_roots_are_provider_registry_driven`
- `internal_session_paths_are_not_exposed`
- `every_task_accepts_an_optional_human_message`
- `retry_allows_switching_from_claude_to_codex`
- `same_provider_non_limit_retry_resumes_native_session`
- `same_provider_retry_without_checkpoint_falls_back_visibly`
- `session_limit_retry_never_resumes_the_exhausted_session`
- `cross_provider_retry_uses_a_new_native_session`
- `retry_keeps_attempt_lineage`
- `intermediate_attempt_without_progress_does_not_hide_useful_ancestor`
- `unchanged_retry_reuses_the_prepared_context_bundle`
- `conversation_slices_are_not_rebuilt_for_unchanged_retry`
- `new_provider_receives_the_prior_attempt_handoff`
- `new_provider_recovers_the_prior_progress_checkpoint`
- `native_transcript_is_resolved_outside_repository_scoped_source_index`
- `contract_content_is_durable_and_restaged_to_the_new_attempt`
- `retry_never_depends_on_an_ancestor_contract_path`
- `new_provider_reads_original_sources_only_for_remaining_gaps`
- `context_reuse_is_reported_in_the_task_inspector`
- `progress_handoff_is_reported_separately_from_context_reuse`
- `stale_or_missing_prior_context_falls_back_visibly`
- `remote_claude_records_before_first_cwd_do_not_corrupt_slice_counts`
- `codex_writes_and_applies_revision_one`
- `no_console_or_application_request_errors`

## Assert — same-provider correction

- `same_provider_retry_carries_the_prior_failure_diagnostic` — the resumed
  session receives the failure it is being retried for, and the retained patch
  with it, instead of a bare instruction to continue

## Failure means

RCP hides a provider limit behind a derived patch error, retries the exhausted
provider as if it were healthy, makes a replacement provider repeat context
assembly and completed research from zero, or claims to have continued prior
work without giving the new provider that work.
