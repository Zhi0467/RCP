# Unified tab refresh

Status: design only. Nothing is implemented; implementation waits for an
explicit start. Implemented: none. Remaining: the design sections marked
pending, then every slice below.

## Goal

Opening or switching to any open project tab is almost always instant, and
the Web app keeps its copy of backend data current through one mechanism
instead of the ~25 refresh paths it has today (polls, heartbeat branches,
effects, events). PR #299 did this for agent status alone: a heartbeat
`task_status_signal` plus a background task reread for inactive tabs. Its six
review rounds were almost all races between two copies of the same data.

Scope: how the Web app keeps backend data current. Backend jobs that pull
from schedulers, SSH hosts, or providers (watchers, compute probes) are out of
scope.

## Measured problem

On a copy of real data with remote canonical state, a tab switch fires about
15 requests. The server is CPU-bound for about 1.5 s, the browser runs at most
6 at once, and the chat inventory (44 ms alone) takes about 1.8 s because
`SSHStateWorkspace.refresh_if_stale` holds the per-root snapshot lock across
the SSH sync. Task actions such as Retry also wait 2-3 s for their response on
remote-state projects.

## Settled decisions

1. **One shared store.** Each backend resource is held once in the app, keyed
   by backend, member, project, resource, and ref or entity where it applies.
   Every tab reads from it. Saved tab state keeps UI only: selection, panels,
   drafts, scroll.
2. **One batched watermark request** for all open projects. Each resource has
   a cheap token (a database counter or file timestamp), never computed by
   rebuilding a display snapshot. The server returns only projects the member
   can read.
3. **Cadence.** The active project is in every beat (1 s); hidden open
   projects in every third beat (3 s). One request in flight; the next is
   scheduled after the previous completes. Failures back off 3, 6, 12, 30 s.
   The app keeps its last good data with a visible stale marker. The server
   never waits on a remote sync to answer.
4. **Remote canonical state.** The backend syncs open remote-state projects in
   the background every 10 s, and immediately when a tab is activated. Reads
   serve the last completed local copy and never wait for a sync in progress.
   Writes still check real state where correctness needs it. Local-state
   projects gain no cost.
5. **Instant pending state** for every task action (retry, pause, resume,
   stop, send a chat message) through the shared store. The server's answer
   replaces it; a failure rolls back and shows the error on the card.
6. **Kept current:** each open tab's current view (project plus the ref it is
   on; project-wide data once). Other visited branch views stay cached and are
   validated only when reopened. Closing a tab frees its data. No count cap.

## Design

### Server watermarks

Pending.

### Remote state sync and non-blocking reads

Pending.

### Web store, scheduler, and sequencing

Pending.

## Open questions

- Paper: may it open from the store instead of its mandatory entry read?

## Slices

Pending.
