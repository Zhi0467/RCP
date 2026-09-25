# Runs load from cache and fetch only on change

Status on 2026-09-25: design proposed, not implemented. Waiting for the
human's start.

- Implemented: nothing.
- Remains: everything below.
- Settled (human, 2026-09-25):
  - State storage and sync stay as they are. This work is only caching and
    when to fetch.
  - A display read of SSH-hosted state may use a mirror up to 10 s old.
    Reads that gate a decision keep today's 2 s window.
  - No background refresher.
- Closure: the five changes below merge with their tests, the targets in
  [Verification](#verification) hold on copied data and on the team server,
  and the spec sentences are added.

## What was measured

All numbers are from 2026-09-25.

### Personal projects (desktop server, state on a GPU host over SSH)

- `GET /projects/{id}/episodes`: 2–3.6 s. Each call ran 5 SSH/rsync
  processes to re-sync the mirror first. `refresh_if_stale` treats the mirror
  as stale after 2 s, and the page polls less often than that.
- Graph replay is not the cost: 0.07 s on a copied mirror (124 Patches, one
  branch).
- `GET /api/space/runs` (Home run list): 6.4–9.3 s, 18 SSH/rsync processes.

### Team space (state local to the team server, no SSH sync)

Measured in a signed-in browser through an SSH tunnel. The tunnel adds about
0.2 s per request, and most endpoints answer in 0.2–0.4 s.

| Request, one project with 10 Experiment loops and 2 Auto-research branches | Alone | Inside the real page |
|---|---|---|
| `experiment-episodes` | 2.7–3.8 s | 5.3–5.9 s |
| `episodes` | 1.3–1.6 s | 6.0–7.6 s |
| `usage` | 0.4 s | 4.3–5.5 s |
| `/api/space/runs` | 2.2–3.8 s | not measured |

| Action on that project | First Runs content | Runs list complete |
|---|---|---|
| Open Runs | 6 s | 9 s |
| Switch project tabs and come back | 1 s (partial) | 10 s |
| Switch panels within the project and back | 1 s | 1 s |

The server-side cost of these two lists on the team server is not profiled
yet. See [change 4](#4-the-server-reuses-computed-run-lists).

### What the page does

Traced by wrapping `fetch` in both servers' web apps:

1. **Switching project tabs throws the Runs data away.**
   `useEpisodeDialogs` holds one project's episodes and resets them when the
   project changes. The Experiment loop list is already kept per project, so it
   comes back at once.
2. **The first episode load runs twice.** The mount effect fetches, and
   `startLiveEpisodePolling` starts its first poll 1.5 s later, before the
   first request has returned.
3. **Polls run whether or not anything changed.** While Runs is open, the
   episode list is re-fetched 1.5 s after each response, and the Experiment
   list 5 s after each response. When a response takes longer than the
   interval, one of these requests is almost always in flight.
4. **Opening a project fires about 15 requests at once.** Those, the
   duplicate, and the polls queue on the same server. That is why the page
   numbers above are 2–4 times the standalone ones.

The page already polls `GET /projects/{id}/cached/revision` every second for
each open project tab. That response carries only the graph revision, so it
cannot tell the page that a run or task changed.

## The change

### 1. The page keeps each project's Runs data

Hold the episodes in `useEpisodeDialogs` per project, the way
`useProjectTabs` already holds Experiment loops. On a project tab switch, show
the kept list at once and refresh it in the background.

Keep the existing generation guards, so a late response for one project
never lands in another project's list.

### 2. One change signal for runs and tasks

Add a per-project change counter for operational rows: episodes, agent
tasks, watchers, and Experiment control rows. Every write to those rows for a
project bumps it. Use SQLite triggers on those tables, writing to one small
table, so no write path can forget to bump it. This needs a schema-ledger
migration.

Return the counter from `GET /projects/{id}/cached/revision` beside the graph
revision.

For a branch target, the same response already reports the branch revision.
Merge-state changes show up through the task rows.

### 3. The page fetches the run lists only when something changed

Replace the fixed-interval polls of the episode list and the Experiment list
with one rule. Refetch a list when:

- the Runs view opens for a project with no kept data;
- the graph revision or the change counter from the 1 s revision poll moves;
  or
- the page's own action returns (Run, Stop, Archive, and so on), as it does
  today.

Only one request per list may be in flight. A change seen while one is in
flight schedules exactly one more fetch after it.

This also removes the duplicate first load.

`GET /experiment-episodes` performs the one index-path lifecycle repair:
settling a requested stop after the task ends. It still runs, because a task
ending writes a task row, which moves the counter and triggers the fetch.

The Home run list (`/api/space/runs`) follows the same rule, using the
counters of the projects it lists.

### 4. The server reuses computed run lists

Cache the responses of `GET /projects/{id}/episodes`,
`GET /projects/{id}/experiment-episodes`, and the per-project parts of
`/api/space/runs` and `/api/episodes?mode=experiment_loop` in memory. Key
each entry by the project, the main graph head, every branch head it read,
the change counter, and the member filter the route applies.

- Serve a hit without recomputing.
- Never cache an Experiment index answer that performed or needs a
  lifecycle repair.
- Entries live in memory only. Invariant 8 means one process owns the data
  directory, so a restart simply starts cold.

Also open branches read-only in the Experiment index. `_experiment_episode_entries` in
`src/rcp/api/index.py` calls `for_graph_target(...)` with its default
`initialize=True`. That enters a write transaction, which for SSH state always
takes the lock and runs rsync, once per branch-target Experiment. Every other
branch read route already passes `initialize=False`.

While a run is live its task rows change often, so the cache will often miss.
The first implementation step is to profile both lists on a copy of the
team-server data. If a miss still costs seconds, fix that cost in the same
pull request.

### 5. Display reads may use a mirror up to 10 s old

Add `REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS = 10.0` to `limits.py` beside
`REMOTE_STATE_RECONCILE_WINDOW_SECONDS`. Display-only reads pass it to
`refresh_if_stale`.

Reads that gate a decision keep the 2 s default. `accepted_patch_boundaries`
already states that it must fail closed on stale state. So does every read
inside a transition, merge, Apply, or admission.

The implementation lists every `refresh_if_stale` and `refresh()` call site
under `src/rcp` and marks each one as display or gating. Writes still sync
first through `transaction()`, unchanged. The explicit `refresh()` in
`ProjectCatalog.reconcile_snapshot` also stays unchanged.

### Out of scope

- Any change to how state is stored, synced, locked, or published.
- A background refresher.
- Deferring the requests that the visible panel does not need. Re-measure
  after changes 1–5, and propose it separately only if the burst still
  costs seconds.
- The graph replay itself (0.07 s).

## Invariants touched

- 6 (one canonical state repository; `StateWorkspace` owns locking and
  publication): unchanged. Only the freshness window passed to an existing
  method changes, and only for display reads.
- 8 (one process per data directory): the response cache and the counter
  rely on it. A second process cannot write this data directory's SQLite.
- 10f and the other lifecycle rules: the index repair still runs, driven by
  the counter.

## Verification

Focused tests:

1. Web (`web/tests/`): switching project tabs shows the kept episodes before
   the refresh returns; a late response for project A never replaces project
   B's list; one list never has two requests in flight; an unchanged revision
   and counter start no list fetch; a moved counter starts one.
2. Python: each tracked table write bumps the project's counter; the revision
   response carries it; a cache hit skips recomputation; a moved branch head
   or counter misses; an index answer that needs a repair is not cached.
3. Python: the Experiment index with a branch-target Experiment opens no write
   transaction.
4. Python: a display read within 10 s runs no SSH; a gating read after 2 s
   still syncs.

Served-app journeys, with the `fetch`-wrapping trace used for the
measurements above:

- Desktop server, throwaway `RCP_DATA_DIR`, spare port, copies of the
  projects' remote `.research` at throwaway remote paths:
  - opening Runs shows content within 1 s of the first response;
  - switching project tabs and back shows the kept list at once;
  - with nothing changing, no run-list request fires for 30 s;
  - a write from a second RCP process against the same copy appears within
    10 s.
- Team server, on the next release, with the measured project: Runs complete
  under 2 s on open, and under 1 s on a tab return. Report the measured
  values.

Specs:

- `docs/specs/api-web-and-desktop-projections.md`: the revision response
  carries the change counter; run lists are fetched on change, not on a
  timer; the Runs data is kept per project.
- `docs/specs/projects-spaces-and-operations.md`: display reads of remote
  state may use a mirror up to 10 s old; gating reads and writes sync as
  before.
