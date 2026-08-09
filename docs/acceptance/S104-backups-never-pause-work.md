---
id: S104-backups-never-pause-work
status: pending — not human-confirmed
tier: hermetic
driver: pytest
covered_by: none
invariants: [1, 2, 7]
---

# A backup interrupts nothing and claims nothing it did not capture

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[Team server operations](../design/team-server-operations.md#backups-do-not-pause-work).

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
made unreachable. A deterministic agent so a task can be held mid-run, and a
Sync that can be held mid-batch.

## Drive — proposal

1. Start a long task and begin a backup. Attempt to dispatch another task and to
   apply a result while the backup runs.
2. Begin a backup during a Sync, with the batch staged but not yet renamed.
   Inspect what the archive captured for that project.
3. Append patches to a project after its head revision was recorded but before
   the copy finished. Inspect the archive.
4. Read the archive manifest.
5. Make one project's host unreachable and run a scheduled backup.
6. Read the archive manifest and Server Settings afterward.
7. Restore the archive into a fresh data directory and open every project that
   was captured.
8. Inspect the restored tasks that had been running at capture time.
9. Search the archive for provider credentials, SSH keys, source repositories,
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
- `a_restored_archive_replays_every_captured_project`
- `tasks_running_at_capture_time_are_restored_as_interrupted`
- `provider_credentials_ssh_keys_and_source_repositories_are_absent`

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

Encryption is not asserted beyond its shape. Scheduled unattended backup with
the recovery secret held off the server forces a public-key scheme; the
algorithm, key rotation, and recovery flow are unsettled.
