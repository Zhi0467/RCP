---
id: S100-permission-is-checked-twice
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_dispatch_authority.py
  - tests/test_api.py::test_work_apply_rechecks_authority_after_human_removes_proposal_target
invariants: [1, 3, 4, 10b]
last_passed: 2026-08-12 — dispatch bindings were durable before execution,
  refusals spent nothing, live Apply rejected authority lost to graph movement,
  and replay required no operational authority records
---

# Nothing unauthorized starts, and nothing unauthorized lands

Checking permission only when a patch lands is too late. By then the task has
already spent provider budget, read the project, used credentials, and possibly
written a repository or called an external service. Starting work is itself an
authority-bearing action.

Checking only at dispatch is also wrong, because the graph moves while a task
runs. Both checks exist and they answer different questions. Replay answers
neither: once a patch was admitted, a later change cannot reach back and
invalidate it.

Scope confirmed 2026-08-12, alongside
[S115](S115-beliefs-change-only-through-you.md), whose action table this shares.

## What this covers, and what it does not

This scenario is the **two gates and their independence**, driven against the
machinery that exists today. Team membership is not part of it. Neither is the
orchestrator, its campaign, its budget, or its spawned children.

That is a real narrowing and it costs one thing worth naming. The textbook
demonstration of independence — *permission changes while the task runs* —
needs a permission that can change, which today means membership. So the
independence is driven the other way: the **graph** moves under a patch that was
authorized at dispatch, and Apply refuses it anyway. Same property, reachable
now.

Membership landed on 2026-08-15 ([S101](S101-project-membership.md)), so the
permission-that-changes now exists and is driven here as promised rather than
re-derived. Losing project membership between dispatch and Apply refuses the
patch at Apply, and the two remain deliberately asymmetric with credential
revocation: revoking a token is about a credential and stops no
already-authorized work, while losing membership fences the episode
([S122](S122-project-invitations.md)).

## Setup

A project with an accepted Hypothesis, a pending human-gated removal Proposal,
and a deterministic Work agent. The agent writes one observable repository
effect, returns an answer, and proposes a content change to that Hypothesis.

## Drive

1. Attempt ordinary dispatches without a current human authorizer, and attempt
   an ordinary-profile dispatch carrying the `orchestrate` contract.
2. For every refusal, inspect the task table and usage ledger and prove the
   provider entry point was never reached.
3. Dispatch a legitimate Work turn. At provider entry, read its durable task
   record and exact authorizer, project, profile, contract, and scope binding.
4. Let the provider complete its repository effect and answer, then hold its
   content Proposal immediately before canonical Apply.
5. While it is held, approve the pending removal Proposal for the Hypothesis.
6. Release the Work turn and let RCP revalidate against the live graph while
   holding the append lock.
7. Read the answer, repository effect, graph, retained patch, and task result.
8. Replay the project with an authority resolver that fails if called.
9. Dispatch a second Work turn, and while it is held before Apply, remove its
   authorizer's project membership. Release it.
10. Separately, dispatch a turn and revoke that member's token while it runs.

A Discuss turn that writes a stray `patch.json` is intentionally a different
check: Discuss is authorized to launch, then its inactive Patch channel is
discarded. It does not stand in for a pre-launch refusal.

## Assert

- `an_unauthorized_dispatch_never_launches_a_provider`
- `a_refused_dispatch_spends_no_budget_and_creates_no_scratch`
- `a_refused_dispatch_says_which_action_was_refused`
- `the_durable_task_record_exists_before_execution_begins`
- `dispatch_binds_authorizer_project_profile_contract_and_scope`
- `a_patch_authorized_at_dispatch_can_still_be_refused_at_apply`
- `the_two_checks_are_separate_paths_and_neither_stands_in_for_the_other`
- `the_apply_decision_and_the_canonical_append_are_one_serialized_path`
- `repository_writes_and_external_effects_completed_before_refusal_stand`
- `a_refused_patch_is_not_described_as_retracted_work`
- `the_answer_survives_a_refused_patch`
- `replay_succeeds_with_no_profile_or_permission_records`
- `membership_lost_between_dispatch_and_apply_is_refused_at_apply`
- `revoking_a_token_mid_run_refuses_nothing_and_fences_nothing`

## Boundary

Refusing a patch is not undoing work. Compute, repository writes, and external
calls that already happened stand, and the interface must say so rather than
implying a rollback. Stopping work is a separate operational action.

The answer is not the patch. A turn whose graph change is refused still returns
what it said — the failure mode where a rejected patch discarded a chat reply is
recorded in [`AGENTS.md`](../../AGENTS.md) and must not return.

**A task recorded before dispatch authority existed carries no binding, and that
parent imposes none.** Decided 2026-08-12 against a real store where all 224 rows
predated the column: refusing the continuation would have stranded every Resume
and Retry on upgrade, throwing at click time. An authorization that never happened
cannot be invented after the fact, so the parent constrains nothing. What is
checked twice is unaffected — the continuation still resolves and gates its own
authority at dispatch, and re-checks it at Apply. Only the *equality* rule is
skipped, and only where there is no earlier binding to be equal to.

Still deferred until the console operations exist: the space binding on a
dispatch, and removing another person from a project or the space entirely
([S103](S103-server-operations-are-console-operations.md)). Leaving is your own
act and lands in S122; removal is somebody else's.

Deferred until the orchestrator exists: campaign and budget binding, spawned
children recording their parent, the `orchestrate` contract, and the elevated
profile's actions ([S77](S77-auto-research-stops-at-belief.md),
[S78](S78-one-budget-one-stop.md)).

## Failure means

A task runs for an hour, spends real money, writes a repository, and only then
discovers it was never allowed to do any of it. Or the reverse: a check at
dispatch is treated as a reservation, and a patch lands against a graph that
moved out from under it.
