# Graph history is addressable like git

Status: active, 2026-10-12.

## Decision

The Patch log stays the only canonical history. Every history view and agent
query reads one engine of per-step records, and any `main@R` or
`branch:<id>@R` opens without a full replay, through a shared in-memory step log
plus rebuildable checkpoints and a changed-id index under `project-caches`.

## Why

Agents saw only the head snapshot and, on chat continuations, a bare revision
number. Six separate history views each replayed the log for themselves.

Replay cost grows with history length times graph size. On a real 122-Patch,
264-entity project a full replay took about 190 ms, at roughly 9 µs per entity
per Patch; 1,000 Patches over 2,000 entities extrapolates to 15–20 s cold.

Git avoids this by making every state addressable, sharing unchanged parts,
only ever adding, and indexing which paths each commit touched. RCP keeps event
sourcing for canonical truth (invariant 1) and borrows the rest as derived,
rebuildable state.

An in-memory log alone would replay in full after every backend restart,
eviction, or update, and a cache stored on per-request branch managers would be
empty on every call. Checkpoints bound a cold `@R`; the changed-id index bounds a
cold `graph_log`.

## Rejected

- Rebuilding every view from the semantic delta plus its author: it loses
  refresh intent, the merge human-authorship proof, and authored revision prose.
- Replaying per query: fine at today's sizes, but agent query loops would hold
  the history lock for full replays on large projects.
- A persisted second event log: it would compete with the canonical Patch log.
