---
id: S103-server-operations-are-console-operations
status: pending — not human-confirmed
tier: hermetic
driver: pytest + api
covered_by: none
invariants: [1, 8]
---

# Dangerous operations need the machine, not a login

This scenario is a proposal and is **not yet human-confirmed**. The current
boundary is in
[Server and machine operations](../specs/projects-spaces-and-operations.md#server-and-machine-operations).

Every member has equal space authority and there is no admin role. That only
works if the operations nobody should perform casually are kept off the product
surface entirely, rather than guarded by a rank the design refuses to introduce.

Backup, restore, update, and member removal therefore require being on the
server as the service account. A member token — including a stolen one — cannot
redirect backups to a path the holder controls, install an update, restore over
the space, or remove anyone.

Removal is the operation with product consequences, so it has to say what it
will end before it ends it.

## Setup

A team space with two members, a configured backup destination, a project both
belong to, and one member running a long task and an active campaign.

## Drive — proposal

1. As an authenticated member, attempt through the API to change the backup
   destination, trigger a backup, install an update, restore an archive, and
   remove the other member.
2. Read what Server Settings shows a member about backups and updates.
3. At the console as the service account, run the member-removal command for the
   member with running work, and read what it reports before confirming.
4. Confirm. Read the state of that member's tasks and campaign, their project
   memberships, their token, and their sessions.
5. Read the repository that member's Work task had already written, and the
   canonical history that member authored.
6. At the console, change the backup destination and take a backup.
7. Attempt each console operation as an ordinary member's OS account.

## Assert

- `no_api_route_exposes_backup_configuration_to_a_member`
- `no_api_route_exposes_update_installation_to_a_member`
- `no_api_route_exposes_restore_to_a_member`
- `no_api_route_exposes_member_removal_to_a_member`
- `server_settings_shows_backup_and_update_state_as_read_only`
- `removal_reports_the_tasks_and_campaigns_it_will_stop_before_acting`
- `removal_stops_that_members_running_tasks_and_campaigns`
- `removal_drops_project_memberships_revokes_the_token_and_ends_sessions`
- `removal_leaves_completed_repository_writes_and_external_effects_intact`
- `removal_leaves_authored_canonical_history_and_its_attribution_intact`
- `a_member_leaving_voluntarily_remains_available_in_the_app`
- `console_operations_refuse_without_service_account_privilege`

## UI path (proposal)

Server Settings shows the last successful backup, the latest failure, and
whether a newer RCP release exists — as status, with no control that changes any
of them. Where a member might expect a button, the interface names what to do
instead: these run on the server.

**Leave space** remains an ordinary member action.

Deliberately not possible: any in-app path to configure backups, install an
update, restore, or remove another person.

Open for a human answer: whether the read-only status should name who
administers the server, or stay silent about that.

## Boundary

RCP does not define who may administer the machine. It borrows the machine's
privilege system, so the lab's existing `sudo` policy decides. The consequence
belongs in the docs rather than in a rule RCP enforces: machine privilege also
grants read access to every project's history and every member's token hash.

Stopping a removed member's work is deliberate. Because permission is rechecked
at Apply ([S100](S100-permission-is-checked-twice.md)), a campaign left running
after its authorizer was removed would spend hours of provider budget and then
have every patch rejected.

This scenario asserts that the operations are absent from the API and behave
correctly at the console. The console command surface itself, and its audit
trail, are not yet designed.
