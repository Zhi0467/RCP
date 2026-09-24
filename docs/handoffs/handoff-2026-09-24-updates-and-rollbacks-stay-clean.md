# Release updates and rollbacks stay clean

Date: 2026-09-24. Status: whole-folder checkpoint rollback is implemented;
the items under "Remaining work" are open.

## Implemented

- **Whole-folder checkpoint.** Before the old release prepares, the supervisor
  (0.1.7) copies every entry of the data folder and each local project's
  `.research` folder: credentials, jobs, cursors, unknown files and empty
  directories included. The folder list comes from the backup's captured
  registrations through the location helper prepare also uses. Rollback restores
  only from this checkpoint; the old prepared payload is validation input only.
- **Checks before the service stops.** An update is refused when a filesystem
  lacks bytes or inodes for both the checkpoint and the sibling copy a rollback
  builds (`checkpoint_capacity`), or when an entry has extended attributes or a
  foreign owner the copy cannot reproduce (`checkpoint_unsafe_entry`).
- **File check after restore.** Live files are reread and compared with the
  checkpoint. A mismatch keeps the service stopped and names the first differing
  paths (`rollback_tree_mismatch`); there is no automatic repair.
- **Two stages for remote projects.** Before the release commits, the update
  touches only local state. A remote project is never opened until the release
  is chosen; validation already replayed its captured history on copies.

## Remaining work

1. **Sign-in quiescence:** maintenance must also drain pending provider sign-in
   threads, including ones that have not yet written their in-progress marker.
2. **Source floor:** declare the oldest release a server may update from, and
   enforce it with the same rule the CI gate uses.
3. **Installed-artifact CI:** prove update, forced rollback, backup and a second
   update with unmodified old wheels on disposable systemd hosts.
4. **Legacy first update:** decide how sources without stronger quiescence are
   admitted; the online entrance is unchanged today.
5. **Read-only cache rebuild:** rebuilding a missing local display cache during
   the post-update check can rewrite derived files; rollback restores them, but
   a read-only rebuild would be cleaner.

## Open human decisions

- Refuse online updates from legacy sources until an operator stops them, or
  keep the online entrance with its unproven sign-in quiescence.
- Choose the source floor.
- Decide whether installed-artifact CI becomes a required check, and its budget.

The [disposable qualification handoff](handoff-2026-09-06-disposable-supervisor-qualification.md)
owns broader adoption and restore checks. Delete this handoff when the remaining
work and decisions close.
