# Release updates and rollbacks stay clean

Date: 2026-09-24. Status: full checkpoint restoration implemented in this PR;
maintenance entrance and installed-artifact qualification remain open.

## Implemented

After maintenance returns its receipt, candidate `inventory` projects backup's
captured registrations through `_project_restore_location`, shared with prepare;
there is no second SQLite or manifest reader. Before stopping the service,
supervisor `check-space` refuses with `checkpoint_capacity` if the checkpoint
filesystem lacks room. Supervisor 0.1.6 then stops the service and snapshots every
entry of each replacement root before old prepare can mutate it, including
credentials, jobs, cursors, unknown files and empty directories. Backup inclusion
rules cannot define rollback contents: replacing a whole root from a selected
subset loses omitted state. Old prepared payloads remain validation input only.

Rollback copies the checkpoint back and rereads live files against it. Mismatches
keep the service stopped and record bounded relative paths with missing, extra
or changed classifications, without contents or secrets. There is no automatic
repair or new recovery command. The existing previous-release startup probe
follows file readback; interruption resume and in-place quarantine retention
remain. No old-version replay is added. Regression coverage forces rollback with
a synthetic token, job, cursor and empty directories, then requires a subsequent
backup with no uncaptured projects.

## Remaining work

1. **Sign-in quiescence:** drain pending sign-in threads and credential writes,
   including threads waiting before an in-progress marker exists.
2. **Source floor:** inventory promoted artifacts; share direct-update admission
   policy with release qualification and retain permanent schema-era fixtures.
3. **Installed-artifact CI:** prove rollback, subsequent backup, successful update
   and ordinary startup using unmodified old wheels on disposable systemd hosts.
4. **Legacy first-update entrance:** establish admission for sources without
   stronger quiescence capability; the current online entrance stays unchanged.
5. **Read-only post-update check:** reconstructing a missing local display cache
   can rewrite derived files in a local canonical folder; rollback restores them,
   but a read-only reconstruction would be cleaner. Remote projects are already
   left unopened until the release commits.

## Open human decisions

- Refuse online updates from legacy sources until an operator stops them, or
  retain the online entrance with its unproven sign-in quiescence.
- Choose the source floor after reviewing artifacts and older migration paths.
- Decide whether installed-artifact systemd/SSH CI is required and fund its runner.

The [disposable qualification handoff](handoff-2026-09-06-disposable-supervisor-qualification.md)
owns broader adoption/restore checks. This handoff authorizes no live-data access
or production fault injection. Delete it when remaining decisions and checks close.
