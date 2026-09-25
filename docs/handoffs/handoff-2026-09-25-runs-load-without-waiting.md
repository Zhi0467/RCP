# Runs load without waiting

Status on 2026-09-25: design proposed and revised after two xhigh Codex design
reviews, not implemented. Waiting for the human's start.

- Implemented: nothing.
- Remains: everything below.
- Settled (human, 2026-09-25):
  - State storage, sync, locking, and publication stay as they are. This work
    is only caching and when to fetch.
  - The page keeps its timer polls. A "fetch only on change" signal was
    rejected: it would have to track about a dozen independent inputs, and
    any miss would show stale data indefinitely. Timer polls retry by
    themselves.
  - A display read of SSH-hosted state may use a mirror up to 10 s old.
    Reads that gate a decision keep today's 2 s window.
  - No background refresher.
  - One pull request carries this doc, every change below, and their tests.
- Closure: that pull request merges, the targets in
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

Why the team server spends seconds on these two lists is not profiled yet.
Profiling them is step 1 of [change 3](#3-the-run-lists-cost-less-to-compute).

### What the page does

Traced by wrapping `fetch` in both servers' web apps:

1. **Switching project tabs throws the Runs data away.**
   `useEpisodeDialogs` holds one project's episodes and resets them when the
   project changes. The Experiment loop list is already kept per project, so
   it comes back at once.
2. **The first episode load runs twice.** The mount effect fetches, and
   `startLiveEpisodePolling` starts its first poll 1.5 s later, before the
   first request has returned.
3. **Opening a project fires about 15 requests at once.** Those requests,
   the duplicate, and the timer polls queue on the same server. That is why
   the in-page numbers above are 2–4 times the standalone ones.

## The change

### 1. The page keeps each project's Runs data

Hold the episodes in `useEpisodeDialogs` per project, the way
`useProjectTabs` already holds Experiment loops. On a project tab switch, show
the kept list at once and refresh it in the background.

Keep the existing generation guards, so a late response for one project never
lands in another project's list. The exact-episode fetch for a selected
historical Auto-research episode stays as it is.

### 2. One request per list at a time

- The mount fetch counts as the first poll. The timer schedules the next
  poll after that request settles, so no duplicate is in flight.
- The poll intervals and their visibility conditions stay as they are.
  Re-measure after changes 1–3, and change an interval only if a
  measurement shows it still loads the server.

### 3. The run lists cost less to compute

Step 1: profile, on copied data, the requests that are slow today:

- `GET /projects/{id}/episodes`
- `GET /projects/{id}/experiment-episodes`
- `GET /api/space/runs`
- `GET /api/episodes?mode=experiment_loop`
- `GET /projects/{id}/cached/revision`, which the page calls every second
  for each open project tab

Use a copy of the team-server data (database plus the project state
repositories) and a copy of a personal project's mirror. Report where the time
goes before changing code.

Step 2: fix the dominant costs in the same pull request. Two are already
known.

- **The Experiment index opens branches as writes.**
  `_experiment_episode_entries` in `src/rcp/api/index.py` calls
  `for_graph_target(...)` with its default `initialize=True`.
  `BranchHistoryManager.initialize()` then enters a write transaction and
  replays main again. For SSH state, that transaction always takes the lock
  and runs rsync, once per branch-target Experiment. Every other branch read
  route already passes `initialize=False`. Pass it here too.
- **The revision poll builds live controls.** `/cached/revision` calls
  `cached_project_snapshot()`, which completes the project's live Experiment
  controls on every call. The poll needs only the revision, the snapshot
  freshness, and the last sync time. Read those without completing the rest.
  Keep its existing reconciliation scheduling.

Any other cost the profile finds follows the same rule: compute less for the
same answer. Do not add a response cache keyed on change signals.

### 4. Display reads may use a mirror up to 10 s old

Add `REMOTE_STATE_DISPLAY_READ_MAX_AGE_SECONDS = 10.0` to `limits.py` beside
`REMOTE_STATE_RECONCILE_WINDOW_SECONDS`.

Shared helpers keep the 2 s default. A caller passes the 10 s bound
explicitly, and only when every path from its route leads to display. Two
shared paths show why:

- The branch summary used by the episode list also gates merge admission
  (`episode_routes.py`).
- `current_materialization()` also feeds Experiment admission
  (`api/experiments.py`).

So the bound travels down from the display routes: the episode list, the
Experiment index, the Home run list, and the revision poll. It is never set
at a shared call site.

Writes still sync first through `transaction()`, unchanged. The explicit
`refresh()` in `ProjectCatalog.reconcile_snapshot` stays unchanged.

### Out of scope

- Any change to how state is stored, synced, locked, or published.
- A background refresher.
- A change counter, fetch-on-change, or a response cache keyed on change
  signals. Rejected on 2026-09-25, see Settled.
- Deferring the requests that the visible panel does not need. Re-measure
  after changes 1–4, and propose it separately only if the burst still costs
  seconds.

## Invariants touched

- 6 (one canonical state repository; `StateWorkspace` owns locking and
  publication): unchanged. Only the freshness bound passed to an existing
  method changes, and only from display routes.
- 10g and the lifecycle rules: unchanged. The Experiment index keeps its
  settle repair, and the timer polls keep triggering it.

## Verification

Focused tests:

1. Web (`web/tests/`):
   - switching project tabs shows the kept episodes before the refresh
     returns;
   - a late response for project A never replaces project B's list;
   - one list never has two requests in flight, including on mount.
2. Python:
   - the Experiment index with a branch-target Experiment opens no write
     transaction;
   - the revision poll does not complete live controls;
   - a display route within 10 s of the last sync runs no SSH;
   - a gating read after 2 s still syncs, including through the shared
     branch summary and `current_materialization()`.

Served-app journeys, with the `fetch`-wrapping trace used for the
measurements above:

- Desktop server, throwaway `RCP_DATA_DIR`, spare port, copies of the
  projects' remote `.research` at throwaway remote paths:
  - switching project tabs and back shows the kept Runs list within 0.5 s;
  - no list ever has two requests in flight;
  - a write from a second RCP process against the same copy appears within
    10 s.
- Team server, on the next release, with the measured project:
  - a tab return shows the kept list within 0.5 s;
  - Runs complete on open under 2 s. This target depends on what step 1 of
    change 3 finds. Report the measured value either way.

Specs:

- `docs/specs/api-web-and-desktop-projections.md`: the Runs data is kept per
  project, and each run list has at most one request in flight.
- `docs/specs/projects-spaces-and-operations.md`: display routes may read
  remote state from a mirror up to 10 s old; gating reads and writes sync as
  before.
