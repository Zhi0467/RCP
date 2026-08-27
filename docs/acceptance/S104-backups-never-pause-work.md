---
id: S104-backups-never-pause-work
status: pending
tier: live
driver: pytest + ssh
covered_by: none
invariants: [1, 2, 7]
---

# A backup interrupts nothing and claims nothing it did not capture

This scenario is human-confirmed and pending implementation. Its boundary is in
[Server and machine operations](../specs/projects-spaces-and-operations.md#server-and-machine-operations).

An earlier design had the server delay dispatch and delay applying results for
the duration of each capture. With canonical history on remote machines that
window is minutes, every night. It is also unnecessary: both halves of the
archive are already consistent by construction.

The operational database is in WAL mode, so an online snapshot is consistent
while writers continue. Canonical history is append-only, so recording each
project's head revision and copying up to it cannot capture half a state.

The other half of the promise is honesty. A lab's projects live on machines that
reboot, and one unreachable host must not turn into no backup at all — but an
archive must never claim completeness it does not have.

## Setup

A team space with several registered projects, one of them on a host that can be
made unreachable. A deterministic agent so a task can be held mid-run, a Sync
that can be held mid-batch, an `age` recipient whose private recovery identity is
held off-server, and a fresh restore host.

## Drive

1. Start a long task and begin a backup. Attempt to dispatch another task and to
   apply a result while the backup runs.
2. Begin a backup during a Sync, with the batch staged but not yet renamed.
   Inspect what the archive captured for that project.
3. Append patches to a project after its head revision was recorded but before
   the copy finished. Inspect the archive.
4. Read the archive manifest.
5. Make one project's host unreachable and run a scheduled backup.
6. Read the archive manifest and Server Settings afterward.
7. Inspect the archive bytes without the recovery identity, decrypt them with the
   off-server identity, and validate the manifest and hashes.
8. Restore the archive into a fresh data directory after explicitly confirming
   that the old copy cannot resume, then open every project that was captured.
9. Inspect the restored tasks that had been running at capture time.
10. Search the archive and the server's backup configuration for private recovery
   identities, provider credentials, Git deploy keys, SSH keys, source repositories,
   materialized outputs, run scratch, and caches.

## Assert

- `a_backup_does_not_delay_dispatch`
- `a_backup_does_not_delay_applying_a_result`
- `the_database_snapshot_is_consistent_while_writers_continue`
- `the_manifest_records_each_projects_captured_head_revision`
- `patches_appended_after_the_recorded_head_are_simply_absent`
- `a_batch_staged_but_not_renamed_is_captured_as_absent_not_as_half`
- `materialized_outputs_are_excluded_and_regenerated_by_replay_on_restore`
- `an_unreachable_project_does_not_fail_the_whole_backup`
- `the_manifest_names_each_uncaptured_project_with_a_reason_and_time`
- `server_settings_shows_which_projects_are_actually_protected`
- `the_server_stores_only_an_age_public_recipient`
- `every_archive_is_age_encrypted_and_integrity_checked_before_restore`
- `the_private_recovery_identity_remains_off_server`
- `a_restored_archive_replays_every_captured_project`
- `tasks_running_at_capture_time_are_restored_as_interrupted`
- `provider_credentials_git_deploy_keys_ssh_keys_and_source_repositories_are_absent`
- `restore_requires_explicit_old_authority_exclusion_before_serving`

## Boundary

Excluding materialized outputs is what makes the no-pause design safe:
`graph.json` captured beside an earlier head revision would be the one way to
produce an inconsistent archive. Restoration replays instead. This is enforcing
an existing rule, not adding one.

A backup does not preserve an in-memory execution and must not claim to. Tasks
that were running come back interrupted, with their receipts and committed
results intact.

Restore's authority boundary is out of scope here. `space_id` surviving a
restore is what makes the replacement the same space, and nothing in this
scenario proves the original server is gone; see
[S95](S95-durable-team-space.md).

The first implementation pins a supported `age` CLI/library version and one
public recipient. Recipient rotation creates new archives for the new recipient;
it does not rewrite old archives or copy a private recovery identity onto the
server. A real restore drill, not successful encryption alone, proves the backup
usable.
