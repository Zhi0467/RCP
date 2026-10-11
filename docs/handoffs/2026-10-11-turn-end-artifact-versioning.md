# Turn-end artifact versioning

Status: design, not implemented. Branch `fix/artifact-comments-follow-mode` (PR #292).
Settled with the human on 2026-10-11:

- Any change to a session's artifact file makes its next version, at the end of
  any turn, comment or not. A comment only points the agent at what to change.
- Before a turn starts, a tracked file the agent has not changed is rewritten to
  the artifact's current version (after Undo or a viewer edit), so the agent
  always edits what the human sees.
- No per-turn artifact list in the prompt. The artifact contract already names
  `{workspace}/turns/<turn>/artifacts`; one sentence says earlier turns'
  artifacts stay there, that changing one in place makes its next version, and
  that in-place versus a new file is the agent's judgement (in place when the
  request corrects or enriches it).

## Model

An artifact made by a turn has exactly one **tracked file**: the file discovery
first recorded, `turns/<supplier_id>/artifacts/<source_name>` in the workspace of
its origin task's stage (`stage_host`, `stage_root`). Its id is already derived
from that location (`descriptor_for(scope_id, name)`), and the stage is retained
as long as a non-kept artifact (`RUN_STAGE_RETENTION_DAYS`).

RCP records, per artifact, the **synced digest**: the sha256 of the bytes RCP last
knows are at the tracked file. It is set when discovery creates the artifact,
when RCP publishes from the file, and when RCP re-syncs the file.

A turn's **session artifacts** are the live (unexpired or kept) `supplier="turn"`
artifacts whose origin task shares the turn's stage host and root.

## Turn start: re-sync

For each session artifact whose current version's sha256 differs from the synced
digest: read the tracked file's digest; if it equals the synced digest (the agent
has not changed it), write the current version's bytes there and record the new
synced digest. If the file is gone, skip it. If it differs from both, leave it;
turn end treats it as a change on a stale base (below).

## Turn end: publish

For each session artifact, read the tracked file's digest (one listing per stage;
remote stages use one SSH call). If it differs from the synced digest and the
media type is unchanged, publish the bytes as the next version with base = the
version the synced digest belongs to. A moved current version (version conflict)
keeps today's fork: the bytes become a separate artifact, with a `forked` outcome.
A type change or an oversized file records `failed` with its reason code. A
missing file records nothing (expiry or cleanup). Each outcome is a receipt on
the turn and projects as `result.artifact_edit_outcomes` (a list), rendered one
line each under the reply, except `published`, which shows as the new version.

Unchanged files record nothing, so ordinary turns stay silent. A commented artifact
that ends unchanged still records `unchanged`, so the human learns the comment did
not edit it.

## Comments

A comment, from the composer or the viewer, no longer stages a second copy. It
names the tracked file, re-synced at turn start to the version the comment was
drawn on (refused, as today, when the artifact moved after selection). The
viewer's origin-bound route keeps its session, master, and Discuss authority; it
runs on the origin's stage, which holds the tracked file. Edits of Experiment and
orchestrator artifacts keep their revoking launch, writable only for the tracked
file's folder.

`finalize_artifact_edit` and the edit staging scope are replaced by the turn-end
publish; `ArtifactEditAdmission` keeps only what admission still decides
(artifact, base version, origin binding, launch kind). Retry and recovery need no
staged copy: the tracked file is the edit.

## Resolved points

- Write authority: Work's writable roots include the whole chat workspace
  (`WriteScope.writable_roots`), and Discuss writes conversation scratch, the same
  workspace; earlier turns' folders need no new grant.
- Viewer and revoking edits already run on the origin's recorded workspace, which
  holds the tracked file.
- Old records: an artifact without a synced digest uses its first version's
  digest; the first turn then re-syncs a file left behind by an old staged-copy edit.
- Concurrency: re-sync and publish run under `artifact_lock`.
- Live HTML: every publish still resolves its live version.
- Replay safety (invariants 1, 4b): versions are artifact storage, not graph
  history; nothing reads graph authority from these files.
