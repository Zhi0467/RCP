---
id: S100-permission-is-checked-twice
status: pending — not human-confirmed
tier: hermetic
driver: pytest
covered_by: none
invariants: [1, 3, 4, 10b]
---

# Nothing unauthorized starts, and nothing unauthorized lands

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Identity, permissions, and agent profiles](../design/identity-permissions-and-agent-profiles.md#permission-is-checked-before-execution).

Checking permission only at Apply is too late. By then an unauthorized task has
already spent provider budget, read project context, used server credentials,
and possibly written a repository or called an external service. So starting
work is itself an authority-bearing action.

Checking only at dispatch is also wrong, because permission can change while a
task runs. Both checks exist, and they answer different questions. Replay
answers neither: once a patch was admitted, later permission changes cannot
reach back and invalidate it.

## Setup

A team space with two members, a project the first belongs to and the second
does not, and a project-owned orchestrator profile. A deterministic agent so
timing is controllable.

## Drive — proposal

1. As the second member, attempt to start a task in that project.
2. As the first member, start a long-running Work task that writes a file in a
   repository and then produces a patch.
3. While it runs, remove the first member's project membership.
4. Let the task finish and let RCP attempt to apply its patch.
5. Inspect the repository the task wrote, the task record, and the graph.
6. Start an orchestrator campaign and let it dispatch a child worker. Read what
   each task recorded.
7. Attempt to reach an orchestration command from an ordinary Work task, and
   attempt an elevated semantic action from the orchestrate contract while
   carrying the ordinary profile.
8. Replay the project with every user, membership, and profile record deleted.

## Assert

- `an_unauthorized_dispatch_never_launches_a_provider`
- `a_refused_dispatch_spends_no_budget_and_creates_no_scratch`
- `the_durable_task_record_exists_before_execution_begins`
- `dispatch_binds_authorizer_space_project_profile_contract_scope_and_budget`
- `a_patch_forbidden_at_apply_time_is_rejected_even_though_dispatch_allowed_it`
- `repository_writes_and_external_effects_completed_before_rejection_stand`
- `a_rejected_patch_is_not_described_as_retracted_work`
- `the_apply_decision_and_the_canonical_append_are_one_serialized_path`
- `a_spawned_child_records_its_parent_and_campaign_and_uses_ordinary_authority`
- `the_orchestrate_contract_is_required_for_orchestration_commands`
- `the_orchestrator_profile_is_required_for_elevated_semantic_actions`
- `replay_succeeds_with_no_identity_or_permission_records`

## Boundary

Rejecting a patch is not undoing work. Compute, repository writes, and external
calls that already happened stand, and the interface must say so rather than
implying the task was rolled back. Stopping work is a separate operational
action.

Because of that gap, revoking access in practice is paired with stopping the
person's running work rather than letting it fail hours later at Apply; that
behavior belongs to
[S103](S103-server-operations-are-console-operations.md).

This scenario asserts the two gates and their independence. The closed action
vocabulary itself — the exact list of semantic actions and their target grammar
— is not yet settled and is not promised here.
