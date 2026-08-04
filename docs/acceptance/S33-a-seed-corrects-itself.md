---
id: S33-a-seed-corrects-itself
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_api.py, tests/test_history.py, tests/test_launcher.py, tests/test_prompts.py, web/tests/runDialog.test.mjs, browser 2026-07-31
last_passed: 2026-07-31
invariants: [1, 8, 9]
---

# A seed that goes wrong corrects itself

A seed run fails in ordinary, recoverable ways: the agent writes the wrong
identifier format, or authors something the graph refuses. None of those is a
reason to stop and ask a human. RCP hands the concrete failure back to the
session that is still holding the analysis, and only stops when continuing
genuinely cannot help.

Three things must be true for that to work.

**RCP owns ingestion bookkeeping.** The agent writes semantic graph operations.
It does not author per-session cursors or coverage keys; RCP advances the
project-level ingestion watermark only after a Seed/Refresh patch applies.

**Continuation carries its cause.** Reusing a native session and knowing *why*
the work is continuing are separate decisions. An interrupted task continues
with "continue the interrupted task". A task whose patch was refused continues
with the refusal. A task that reuses a session must never be told the wrong one
of those.

**Resume uses the context it was resumed from.** A resumed attempt runs against
the prepared context its original attempt was built from — the same provider
roots, project watermark, graph revision, repository pointers, and human
request. It never projects native transcripts or silently substitutes a new
boundary. If that saved context cannot be loaded, or the graph has moved
underneath it, the resume fails and says to Retry.

A rejected agent patch does not consume a graph revision. Validation happens
under the append lock and only an accepted patch enters the log, so a seed that
took three rounds to get right is still revision 1. The rejected patch text and
the exact rejection survive on the task as receipts.

## UI path

The task inspector already lists each provider launch. Each one now states its
continuation cause in the human's words — first attempt, continuing after an
interruption, correcting a rejected patch — so a run that launched three times
reads as one story rather than three unexplained dispatches.

A correction round is ordinary progress, not a warning: the task stays running
and the status line says the agent is correcting its patch. The human is not
asked to press anything. When the bounded rounds are exhausted, the task fails
with the last refusal as its visible error, and **Retry…** behaves as it does
today.

## Setup

A temporary unseeded project and a scripted provider; no real provider or quota
is used. The scripted provider runs a three-act seed:

1. First launch: writes a structurally invalid semantic graph operation.
2. On being handed the schema rejection: fixes the shape, but asserts three
   scientific blockers the graph refuses.
3. On being handed the graph's refusal: converts them to Proposals and writes a
   patch the graph accepts.

A second scripted provider fails once mid-stream with a transient error, so the
interrupt path can be driven separately from the correction path.

## Drive

1. Start the seed. Let all three acts run without human action.
2. Open the task inspector and read the three launches.
3. Separately, pause a running seed, resume it, and inspect the context the
   resumed attempt used.
4. Separately, fail a seed, press **Retry…** leaving the provider unchanged, and
   inspect the instruction the resumed session received.

## Assert — pytest

- `ingestion_bookkeeping_is_not_agent_authored` — the agent schema contains no
  cursor or coverage operation
- `graph_rejection_returns_to_the_same_session` — a `PatchRejected` becomes a
  correction round, not a terminal error
- `replay_halt_and_state_failure_remain_terminal` — the uncorrectable classes
  are unchanged
- `seed_completes_without_human_action` — one task, three launches, one applied
  revision
- `rejected_patch_does_not_consume_a_revision` — the accepted seed is revision 1
- `rejected_patch_text_and_reason_are_retained` — as task receipts, on every
  round
- `correction_rounds_stay_bounded` — an agent that never converges fails with
  the last refusal as the visible error
- `a_correction_never_applies_a_patch_it_did_not_write` — a continuation runs in
  its predecessor's stage, so that attempt's patch file is still there; a launch
  that writes nothing must not have that file collected as its own work, and the
  substantive refusal stays the visible error
- `resumed_attempt_uses_the_saved_prepared_context` — same provider roots,
  project watermark, graph revision, repository pointers, and human request
- `resumed_attempt_never_reassembles_context` — `assemble_run` is not called on
  a resumed attempt
- `resume_without_prepared_context_fails_visibly` — and names Retry
- `resume_against_a_moved_graph_revision_fails_visibly` — no silent rebase
- `resume_after_interrupt_carries_no_correction_instruction`
- `same_provider_retry_after_failure_carries_the_failure` — the rejection
  reaches the resumed session, and the retained patch with it
- `continuation_cause_is_recorded_per_launch`
- `provider_exit_evidence_is_recorded` — return code, event counts, and whether
  a patch file existed, on a clean exit as well as a failure

## Assert — browser

- `inspector_names_each_launch_cause`
- `correction_round_reads_as_progress_not_failure`
- `no_console_or_application_request_errors`

## Failure means

RCP stops an otherwise healthy run to ask a human for something the agent could
have fixed; or it resumes a provider against a corpus that provider never read;
or it reuses a native session while telling it the wrong reason for continuing,
so the agent repeats the work that just failed.
