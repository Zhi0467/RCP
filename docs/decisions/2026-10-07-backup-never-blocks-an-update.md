# Backup never blocks an update

Confirmed by the human 2026-10-07.

## Decision

The core service must update. Backup, restore and other side modules deserve
fixes, but they never refuse an update. When backup cannot read a project, the
update still checkpoints that project's folder byte for byte, so rollback stays
complete. Only that project skips the pre-switch copy check, and the update
names it as a warning.

Identity, writer ownership, migration, proof mismatches on proven projects,
checkout identity, and path safety stay hard refusals.

Readers of stored data (backup, restore, transfer) check only what they need to
be safe: plain filenames, absolute paths, length bounds. They never re-check a
writer's rule with a stricter copy, and the secret redaction filter is never a
validity rule for stored data.

`rcp server update` without `--confirm-target` runs the target's real
preparation on a consistent copy, and pre-promotion runs the same command
against each team server.

## Why

On 2026-10-07 two promoted builds passed CI and every candidate check, then the
team server refused the update. First a store-kept artifact, then an
answered-question follow-up id in bare hex, failed backup's record checks, and
the update borrowed those checks. CI data came from setup code, so it never held
the shapes real use writes. An update is the operation that must not fail; a
partial nightly backup is visible in Server Settings and recoverable.

## What it gives up

A project backup cannot read gets no application proof: neither the copy
check before the switch nor the live check after it reads that project. The
update names it as a warning, and its folder is still in the byte-for-byte
checkpoint, so rollback restores it exactly.

## Not done

Version-negotiated supervisor contracts, a capture-free maintenance stop, and a
module-health framework were considered and deferred; the smaller cut met the
goal without new contract versions.
