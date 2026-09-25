# Remote state reads skip the sync when nothing changed

Status on 2026-09-25: design proposed and revised after an xhigh Codex design
review, not implemented. Waiting for the human's start.

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

A single SSH call that hashes the content of the whole remote tree takes 0.26 s
over a reused connection, for a 5.2 MB tree, and returns the same value on every
call while nothing changes.

## The change

### 1. One content digest, shipped to the remote and run locally

Add `src/rcp/transport/remote_state_digest.py`. It holds one function that walks
a `.research` tree and returns:

- `missing` when `manifest.toml` does not exist;
- otherwise a SHA-256 over the sorted entries of the tree. Each entry is its
  relative path, file type, mode, symlink target, and full file bytes.

It skips exactly the entries rsync excludes (`.refresh.lock`,
`.agent-run.lock`, `.append.lock`, `.chat.lock`, `.publish`). Keep one tuple of
those names in `state.py` and pass it to both rsync and the digest, so the two
lists cannot drift.

The same module runs remotely through `_remote_script`, like the other remote
scripts, and is imported locally for the mirror. One implementation computes
both sides.

The digest hashes content, not metadata. Coarse timestamps, same-size rewrites,
and cached attributes cannot make two different trees look equal.

### 2. A reader refresh that compares both trees first

`_refresh_snapshot`, when not inside a publication lease:

1. Run the digest remotely: one SSH call. It also replaces the separate
   `test -f manifest.toml` call.
2. `missing` keeps today's behavior: mark reachable, return `False`.
3. Compute the digest of the local mirror. If the two are equal, mark the
   workspace reachable and synced, and return `True`. No lock, no rsync.
4. Otherwise take today's path: lock and rsync.

Nothing is stored between reads. Each read compares the two trees as they are
now, so:

- another `SSHStateWorkspace` on the same cache root cannot hold a stale
  answer;
- a damaged or locally edited mirror no longer matches and gets re-synced,
  which keeps what the full sync restores today;
- explicit `.refresh()` in reconciliation also skips rsync only when there is
  nothing to restore.

### 3. rsync compares content

Add `--checksum` to `_sync_remote_tree`. By default rsync skips a file whose
size and mtime match, even when its bytes differ. That file would then never be
copied, and every later read would see a mismatch and run a full sync again.
The trees are a few MB (3.7 MB and 5.2 MB for the two largest projects), so the
extra reads are cheap.

### 4. The Experiment index opens branches read-only

`_experiment_episode_entries` in `src/rcp/api/index.py` opens each
branch-target Experiment with `for_graph_target(...)` and its default
`initialize=True`. `BranchHistoryManager.initialize()` enters
`workspace.transaction()`, and an SSH transaction always takes the lock and
runs rsync. So the Home run list pays one full sync per branch-target
Experiment, whatever the refresh does.

Pass `initialize=False` there, as every other branch read route already does
through `get_graph_service(..., initialize=False)`. This matches the spec rule
that branch reads validate refreshed state without repairing or publishing.

The 2 s window stays. It still saves the SSH call for back-to-back reads.

### Why an equal digest proves the mirror is current

- Equal digests mean the mirror and the remote tree have the same entries and
  bytes, apart from the lock and staging entries rsync also skips.
- A read during another device's publication sees at least one changed file
  and falls back to the locked sync. If the publication has not changed a file
  yet, the remote tree is still the last committed state.
- The remote digest opens and reads each file. On NFS, close-to-open
  consistency makes a fresh open see the last closed write. Cached attributes
  alone cannot hide a change.

### Out of scope

- A background refresher: rejected by the human on 2026-09-25.
- A graph replay cache: replay costs 0.07 s, measured above.
- Refreshing projects in parallel for the Home run list: four checks at about
  0.3 s each is small enough. Revisit only if a measurement says otherwise.
- Using the check inside write transactions. Writes keep the locked sync.
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
  remote state still serialize through the remote lock. The check keeps no
  state, so a write by either process shows up as a changed digest.
- Remote-executed code ships from its source module (cross-cutting rule).

## Verification

Focused tests in `tests/test_transport.py`, using the existing SSH/rsync
doubles:

1. With remote and mirror equal, a refresh runs one SSH call and no lock or
   rsync.
2. A changed remote file triggers the locked rsync.
3. A damaged mirror (a missing Patch, or an extra local file) triggers the
   locked rsync while the remote is unchanged.
4. `missing` and an unreachable host keep today's results.
5. rsync and the digest skip the same entries, taken from one tuple.

One test in `tests/test_api.py`: the Experiment index with a branch-target
Experiment opens no write transaction.

Real-host checks, on a throwaway `RCP_DATA_DIR`, a spare port, and a copy of a
project's `.research` placed at a throwaway remote path:

- A warm `GET /episodes` for a project with auto-research branches takes under
  0.5 s. Report the measured value.
- `GET /api/space/runs` across copies of the four projects, including
  branch-target Experiments, takes under 1.5 s.
- A write from a second RCP process against the same copy shows up in the
  first process's next read.
- A same-size rewrite of a remote file that keeps its mtime (`touch -r`) is
  copied on the next read.
- Count SSH processes per request with the `ps` sampling loop used for the
  measurements above: one per project on a warm read.

Team space: measure one Runs request on the team server. If it is still slow,
that is a new investigation, not part of this handoff.

Spec: add one sentence under "Every project has exactly one canonical state
repository" in `docs/specs/projects-spaces-and-operations.md`. A read of remote
state syncs the mirror only when the content digests of the remote tree and
the mirror differ.
