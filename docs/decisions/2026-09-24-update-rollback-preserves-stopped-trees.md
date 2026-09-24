# Update rollback preserves stopped trees independently of backup policy

Status: accepted for the exact stopped-tree rollback on merge of the pull
request that implements it, 2026-09-24; that human merge is the approval. The
legacy first-update entrance, the supported source floor, and installed-artifact
CI stay open in the
[handoff](../handoffs/handoff-2026-09-24-updates-and-rollbacks-stay-clean.md).
The [operations specification](../specs/server-and-machine-operations.md)
describes the implemented rollback.

## Recommendation

The application identifies its state roots and owns schema interpretation,
quiescence, replay, and operational recovery. The independent supervisor owns
an exact snapshot of the stopped local roots, restoration, and a proof against
that snapshot. An old application's prepared subset must never define the
replacement contents of a larger live root.

Protected backup and update rollback have different purposes. Backup may
intentionally exclude managed credentials and machine-local files. Rollback
must preserve those bytes, unknown regular entries, and empty directories
inside every root it can replace. It must not import backup exclusion lists.
Unsafe or unrepresentable filesystem entries cause refusal, not omission.
Checkpoint payloads containing credentials stay private and local; they are
never uploaded as diagnostics or added to protected backup scope.

The equality boundary is the stopped, quiescent tree before any preparation or
candidate mutation, compared with the restored tree before ordinary startup.
It includes entry names, types, file sizes and hashes, allowed symlink text,
and required access metadata. Normal startup subsequently changes runtime
files and operational records. Equality after a running service resumes is
neither the intended contract nor a meaningful byte-level assertion.

Rollback verification must not depend on an old live cache verifier that can
refuse a stale cache the old server was already serving. Verify old semantics
on a disposable copy, and verify restored live bytes without opening the live
application. Persist the previous-release choice before starting old code.
Failure after either durable choice preserves that choice's data. This changes
the current pre-choice old-live-probe procedure and must be qualified explicitly.

## Why this boundary must outlive the repair

Adding today's omitted directories to application preparation fixes only a
future source release. The installed old release prepares the next update,
and future state owners can repeat the omission. Exact filesystem preservation
belongs to the component that replaces those files. Application proofs still
matter: exact rollback does not prove the candidate interprets the graph or
operational state correctly.

The existing checkpoint traversal, hashing, atomic replacement, journal and
retention owners should be extended, not duplicated. Keep `StateWorkspace` as
the canonical publisher; a release update performs no new graph transition.
Remote canonical state is verified read-only before choice and is never
restored from a local rollback checkpoint.

## Compatibility policy

Keep the [permanent schema compatibility promise](2026-08-27-server-schema-compatibility.md).
For direct packaged updates, test every promoted source release since the
first supported paired-wheel installation, including releases no longer listed
by the release service. The floor describes installation capability, not age.
Raising it requires an explicit migration path and human decision. Existing
frozen schema-era fixtures remain required, including pre-packaged eras.

Put the machine-readable source policy in the application release contract;
candidate capabilities expose it to the supervisor, and CI imports that same
owner. The supervisor refuses an unsupported source before maintenance. No
operator setting can narrow testing or widen runtime eligibility.

## Rejected approaches

- Add a few missing paths to the old backup-derived payload: leaves old sources
  and the next unknown state owner unsafe.
- Overlay a prepared subset onto candidate data: retains candidate-only files
  and can combine incompatible schemas.
- Test only a recent count or the known fleet: pins and offline installations
  can outlive either list; neither is a compatibility boundary.
- Run old verification on restored live state: it can mutate the state being
  proved or refuse an otherwise usable old display cache.
- Promise arbitrary external rollback: provider homes, repositories, schedulers
  and remote systems require effect fencing, not a local tree replacement.
