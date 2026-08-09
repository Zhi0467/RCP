---
id: S102-team-runs-execute-as-the-space-account
status: pending — not human-confirmed
tier: remote
driver: pytest + api
covered_by: none
invariants: [4, 4b, 5]
---

# Team work runs where the space can reach it, as the space

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Spaces and project homes](../design/spaces-and-project-homes.md#team-runs-execute-as-the-spaces-service-account).

Provider credentials are resolved from the executing process's `$HOME`, not from
where the provider binary happens to live. RCP launches providers without
overriding the environment, and a remote run goes out through a login shell. So
the **execution account** selects which provider identity a run uses — a
machine's `host` field, never a configured binary path.

For team work that account is the space's service account, and this is forced
rather than preferred: RCP hands agents *paths* to canonical `.research` and to
run-scope repositories and deliberately never copies them into scratch, so an
agent running as anyone else cannot read the graph it was launched to work on.
Work additionally has to write repositories the service account owns.

## Setup

A team space whose service account has an authenticated provider CLI, plus a
reachable SSH execution machine. A second account on that machine with its own
provider installation, authenticated differently or not at all.

## Drive — proposal

1. Run a team task on the server itself. Read which provider identity it used.
2. Configure a machine whose `host` names the service account on the remote
   machine, and run a task there.
3. Configure a machine whose `host` names the *second* account while pointing at
   the same provider binary path. Run a task and read the result.
4. Point a machine's provider path at a binary inside another account's home
   while keeping the service account as the login. Read which credentials were
   used.
5. De-authenticate the provider on the target account and start a task.
6. Configure a team project to execute on a member's laptop.
7. Run a task and inspect the scratch workspace, the canonical `.research`
   directory, and where `patch.json` was written and read from.

## Assert

- `provider_identity_follows_the_execution_account_not_the_binary_path`
- `a_team_run_executes_as_the_spaces_service_account`
- `an_execution_machine_must_be_reachable_as_that_account`
- `an_unauthenticated_provider_is_reported_as_a_readiness_failure_not_a_crash`
- `a_readiness_failure_names_the_account_and_the_provider`
- `no_team_task_falls_back_to_a_member_laptop`
- `canonical_research_is_read_in_place_and_never_copied_into_scratch`
- `patch_json_in_the_run_scratch_is_the_only_graph_change_channel`
- `a_personal_project_still_runs_as_its_owner_with_that_persons_provider`

## Boundary

This is a constraint, not a preference, and the interface should say so when a
person tries to configure otherwise. Widening filesystem permissions is not an
escape: it would have to be done per task contract, because Work needs write
access where Discuss needs only read.

Concurrency is not at issue and is not re-asserted here. Several members' work
sharing one provider login is the arrangement RCP already runs under, and
concurrent agent tasks against one account are covered by
[S65](S65-concurrent-agent-tasks.md).

Remote behavior cannot be verified without a reachable host. This scenario is
`tier: remote` and must never be reported as passing from a machine that has
none.
