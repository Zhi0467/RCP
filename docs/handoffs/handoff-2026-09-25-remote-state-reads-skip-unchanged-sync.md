# Remote state reads skip the sync when nothing changed

Status on 2026-09-25: design proposed, not implemented. Waiting for a design
review and the human's start.

- Implemented: nothing.
- Remains: everything below.
- Settled: a cheap freshness check, not a background refresher (human choice,
  2026-09-25). Reads stay proven-current.
- Closure: the change and its tests merge, the measurements in
  [Verification](#verification) hold on copied data, the spec sentence is
  added, and a team-space Runs request has been measured.

## The problem

A project whose canonical state lives on another machine over SSH re-syncs its
local mirror before almost every read. `SSHStateWorkspace.refresh_if_stale`
skips the sync only when the last one finished less than
`REMOTE_STATE_RECONCILE_WINDOW_SECONDS` (2 s) ago. The web polls less often
than that, so every poll pays the full sync.

One sync is five SSH processes, run in order:

1. `test -f manifest.toml`
2. `mkdir -p` on the state root
3. the remote lock holder for `.refresh.lock`
4. `rsync -a --delete` of the whole `.research` tree
5. the SSH process under that rsync

Measured on 2026-09-25 against the desktop server, with four projects whose
state lives on one GPU host:

| Request | Time | SSH/rsync processes |
|---|---|---|
| `GET /api/projects/{id}/episodes` | 3.5 s | 5 |
| `GET /api/space/runs` (Home run list) | 7.4 s | 18 |
| `GET /api/episodes?mode=experiment_loop` | 6.3 s | not counted |

Graph replay is not the cost. On a copy of one mirror (124 Patches, one
branch), `materialize` takes 0.06 s and `branch_read_snapshots` 0.07 s.

A single SSH call that fingerprints the remote tree takes 0.18 s over a reused
connection and returns the same value on every call while nothing changes.

## The change

### 1. A shipped fingerprint script

Add `src/rcp/transport/remote_state_fingerprint.py`, run remotely through
`_remote_script` like the other remote scripts. It takes the remote
`.research` path and prints one line:

- `missing` when `manifest.toml` does not exist;
- otherwise a SHA-256 over the sorted entries of the tree. Each entry is its
  relative path, file type, size, inode, `st_mtime_ns`, and `st_ctime_ns`.

It skips exactly the entries rsync excludes (`.refresh.lock`,
`.agent-run.lock`, `.append.lock`, `.chat.lock`, `.publish`). Keep one tuple
of those names in `state.py` and use it for both the rsync arguments and the
script argument, so the two lists cannot drift.

The inode and ctime fields catch an atomic replace and an in-place rewrite
that keeps size and mtime.

### 2. A reader refresh that checks first

`SSHStateWorkspace` keeps `_synced_fingerprint: str | None` in memory.

`_refresh_snapshot`, when not inside a publication lease:

1. Run the fingerprint script: one SSH call. It also replaces the separate
   `test -f manifest.toml` call.
2. `missing` keeps today's behavior: mark reachable, return `False`.
3. If the fingerprint equals `_synced_fingerprint`, mark reachable and return
   `True`. No lock, no rsync.
4. Otherwise take today's path: lock, rsync, and then, still holding the same
   lease, run the fingerprint script again and store the result as
   `_synced_fingerprint`.

The fingerprint taken under the lock describes exactly the tree rsync copied,
because no publisher can change it while the lock is held.

### 3. Anything that changes the mirror clears the fingerprint

Set `_synced_fingerprint = None` when:

- a `transaction()` or `_publication_lock()` block ends, success or failure;
- `_mark_unreachable` runs;
- any other path writes the local mirror.

The next read then does a full sync. The implementation must list every path
that writes the local mirror and show that each one clears the fingerprint.

The 2 s window stays. It still saves the one SSH call for back-to-back reads.

### Why a matching fingerprint proves the mirror is current

- The stored value was taken under the lock, right after rsync. At that moment
  the remote tree and the mirror were identical.
- Every later local write clears the stored value.
- So when a fresh remote fingerprint matches, the remote tree has the same
  entries and metadata as at that sync. The mirror is therefore current.
- A read during another device's publication sees at least one changed entry
  and falls back to the locked sync. If the publication has not touched
  anything yet, the mirror is the last committed state, which is a consistent
  snapshot.

### Out of scope

- A background refresher: rejected by the human on 2026-09-25.
- A graph replay cache: replay costs 0.07 s, measured above.
- Refreshing projects in parallel for the Home run list: four checks at 0.2 s
  each is small enough. Revisit only if a measurement says otherwise.
- The web dropping the episode list on a project tab switch
  (`useEpisodeDialogs.ts`). With a sub-second request, the empty state
  flashes briefly and no longer waits seconds.
- Team-space projects whose state is local to the server never take this path.
  Their slowness is unmeasured; see Verification.

## Invariants touched

- 6 (one canonical state repository; `StateWorkspace` owns locking and
  publication): the check stays inside `SSHStateWorkspace`. Routes do not
  change.
- 7 (atomic writes): unchanged. The check writes nothing.
- 8 (one process per data directory): unchanged. Two RCP processes sharing
  remote state still serialize through the remote lock. A process that
  publishes clears only its own fingerprint, and the other process sees the
  changed remote tree.
- Remote-executed code ships from its source module (cross-cutting rule).

## Verification

Focused tests in `tests/test_transport.py`, using the existing SSH/rsync
doubles:

1. A second refresh with the remote unchanged runs one SSH call and no lock or
   rsync.
2. A changed remote fingerprint triggers the locked rsync and stores the new
   value.
3. A publication through this workspace clears the fingerprint, so the next
   refresh syncs.
4. `missing` and an unreachable host keep today's results.
5. The rsync excludes and the script's skip list come from one tuple.

Real-host checks, on a throwaway `RCP_DATA_DIR`, a spare port, and a copy of a
project's `.research` placed at a throwaway remote path:

- A warm `GET /episodes` for a project with auto-research branches takes under
  0.5 s. Report the measured value.
- `GET /api/space/runs` across copies of the four projects takes under 1.5 s.
- A write from a second RCP process against the same copy shows up in the
  first process's next read.
- Count SSH processes per request with the `ps` sampling loop used for the
  measurements above: one per project on a warm read.

Team space: measure one Runs request on the team server. If it is still slow,
that is a new investigation, not part of this handoff.

Spec: add one sentence under "Every project has exactly one canonical state
repository" in `docs/specs/projects-spaces-and-operations.md`. A read of remote
state syncs the mirror only when the remote tree's fingerprint differs from
the fingerprint taken at the last locked sync.
