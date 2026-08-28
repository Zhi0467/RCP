---
id: S102-team-runs-execute-as-the-space-account
status: pending
tier: remote
driver: pytest + api
covered_by: none
invariants: [4, 4b, 5]
---

# Team work runs only through the server's configured execution account

This scenario is human-confirmed and pending implementation. Its boundary is in
[Team execution accounts and credentials](../specs/providers-and-containment.md#team-execution-accounts-and-credentials).

Each provider resolves its own native authentication from the executing
account's ordinary environment and home, not from where its binary happens to
live. RCP does not create or select an alternate provider home, and a remote run
uses that remote account's login shell. So the **execution account** selects
which provider identity a run uses — a machine's `host` field, never a configured
binary path.

A local team run uses the server's `rcp` service account. A remote team run uses
the exact SSH account selected by the project profile and authenticated from the
server. It need not be named `rcp`; what matters is that the server owns the
provider call, the account is explicit and ready, and the repositories are
available to it. No team path silently resumes on a member laptop or personal
checkout.

## Setup

A team space whose `rcp` account has an authenticated provider CLI and central
checkout, plus a reachable SSH execution machine with one explicitly configured
remote account and a different control account. Each has distinguishable
provider readiness.

## Drive

1. Authenticate with the provider's own command directly as the local `rcp`
   account, run `rcp server provider check --project <project-id>`, then run a
   team task and read its execution account and provider readiness.
2. Authenticate directly as the reachable SSH execution account, check it
   through the same server CLI, and run a task there.
3. Select the control account while pointing at the same provider binary path.
   Run a task and read the failure or distinct identity.
4. Point a machine's provider path at a binary inside another account's home
   while keeping the configured login. Read which credentials were used.
5. De-authenticate the provider on the selected target account and start a task.
6. Make the remote SSH account unavailable after a task records its binding and
   attempt Resume.
7. Configure a team project to execute on a member's laptop or use a member's
   personal provider login.
8. Run Codex exec, Codex app-server, and Claude through the common local/SSH
   provider-call boundary where those providers are installed.
9. Inspect the scratch workspace, the canonical `.research`
   directory, and where `patch.json` was written and read from.

## Assert

- `provider_identity_follows_the_execution_account_not_the_binary_path`
- `rcp_checks_and_uses_provider_native_auth_but_never_manages_it`
- `provider_login_is_performed_outside_rcp_as_the_execution_account`
- `provider_check_resolves_a_request_or_project_profile_not_an_ad_hoc_account`
- `a_local_team_run_executes_as_the_rcp_service_account`
- `a_remote_team_run_executes_as_the_explicit_configured_ssh_account`
- `an_execution_machine_must_be_reachable_from_the_team_server_as_that_account`
- `remote_transport_uses_the_rcp_accounts_existing_openssh_auth_not_a_member_key`
- `an_unauthenticated_provider_is_reported_as_a_readiness_failure_not_a_crash`
- `a_readiness_failure_names_the_account_and_the_provider`
- `no_team_task_falls_back_to_a_member_laptop`
- `no_team_task_falls_back_to_a_personal_checkout_or_provider_login`
- `resume_preserves_the_exact_remote_account_and_fails_if_it_is_unavailable`
- `every_runtime_uses_the_same_provider_call_identity_boundary_locally_and_over_ssh`
- `canonical_research_is_read_in_place_and_never_copied_into_scratch`
- `patch_json_in_the_run_scratch_is_the_only_graph_change_channel`
- `a_personal_project_still_runs_as_its_owner_with_that_persons_provider`

## Boundary

This is a constraint, not a preference. A remote execution account is
configurable because remote hosts have their own account model; a member laptop
fallback is not. Widening filesystem permissions is not an escape: each task
still receives its exact Work or Discuss scope.

Concurrency is not at issue and is not re-asserted here. Several members' work
executing through the same OS account's existing provider-native authentication
is the arrangement RCP already runs under; concurrent task durability is specified in
[Durable task lifecycle](../specs/providers-and-containment.md#durable-task-lifecycle).

Remote behavior cannot be verified without a reachable host. This scenario is
`tier: remote` and must never be reported as passing from a machine that has
none.
