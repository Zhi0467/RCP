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

- **One endpoint.** `POST /api/resource-watermarks` takes a client id, the
  open projects with each one's current ref and subscribed resources, which
  projects are due this beat, and an optional activated project. It returns
  `{epoch, as_of}` and, per project, `{key, version, valid_until, state}` for
  each resource plus remote sync health (`last_completed_at`, status, error).
  Unknown and forbidden ids get the same `unavailable` answer; membership is
  checked with the existing space predicates, never a member id from the body.
- **Cheap by construction.** Three set-based reads: authorization and aliases,
  requested counters, indexed deadlines. No service open, replay, snapshot
  completion, filesystem walk, or SSH. Cost scales with requested keys, not
  with task or history size.
- **Token sources.** A new `resource_versions(scope, resource, entity,
  generation)` table with counters bumped in the same transaction as each
  owner's writes (tasks and usage, watchers and jobs, chat display
  preferences, questions, consolidation, digest, lessons, episodes and
  Experiment state, branch names, artifacts, settings and readiness, members).
  File-backed data (graph history per ref, chat transcripts, Paper, manifest,
  terminal metadata) uses the published read generation from the remote-state
  section, or a file generation for local state.
- **Time.** Things that change with time alone (a finished task dropping out
  of the list, question liveness, artifact expiry, readiness rechecks) carry
  indexed deadlines; tokens include the latest elapsed deadline at `as_of`.
- **Data carries its version.** Every payload read and mutation response
  returns `{key, version, as_of, value}`, read in one transaction or from one
  pinned generation, so the app never pairs old data with a new token.
- **Retired.** `/cached/revision`, `task_status_signal`, `steer_epoch`,
  `latest_task_id`, `latest_watcher_id`, and the settings, Experiment, and
  compute signals become registry tokens. Graph revision stays domain data;
  remote freshness stays separate health metadata. `epoch` rotates on
  restart, restore, and projection-schema change.

### Remote state sync and non-blocking reads

- **Coordinator.** A lifespan-managed coordinator beside `ProjectCatalog`
  schedules syncs; `StateWorkspace` stays the only publication owner, inside
  the existing data-directory owner (invariants 6 and 8). Demand comes from the
  watermark request: each client's open set, filtered by membership first, held
  under a 60 s lease, unioned across clients. Every 10 s per open remote
  project, never overlapping; an activation bypasses the cadence and coalesces
  one follow-up if a sync is running. Deduplicated by canonical workspace root.
  Local-state projects bypass the coordinator entirely.
- **Immutable read generations.** Today rsync runs with `--delete` straight
  into the mirror while the lock is held, so moving the age check out of the
  lock would be unsafe. Instead the sync downloads into private staging while
  holding the remote `.refresh.lock`, validates, then publishes a generation
  descriptor atomically. Each display read pins one resolved generation
  directory for its whole read; old generations live until their readers
  finish. Captures and write publications share one per-root sequencing owner,
  so an older capture never replaces a newer committed generation.
- **Readers that move to pinned generations:** graph snapshots, history and
  transition inputs; branch snapshots, logs, refs, merge previews; chat
  inventory and transcripts; Paper display and digest; manifest, settings, and
  repository inventory. A cold display read returns unavailable or loading and
  schedules work; it never enters `prepare_state_workspace`.
- **Writes.** Graph commits, merges, settings writes, the Paper base-hash
  check, and chat-log append keep their authoritative transactions.
- **Why Retry takes 2-3 s.** Branch Retry calls `get_graph_service` with
  `initialize=True`, which runs a full branch initialization transaction:
  remote lock, manifest probe, full transfer, replay. `initialize=False` alone
  still refreshes and waits on the snapshot lock. Fix: non-initializing branch
  resolution for Retry, Resume, and chat admission, then pinned reads for
  request preparation, with capability and scope revalidated before execution.
  Pause validates without opening graph state. Stop persists its episode and
  target fence from SQLite before rebuilding display controls. Retry and
  Resume get the same idempotency key as chat send, so a lost response never
  duplicates recovery.
- **Tokens.** A changed capture publishes `(epoch, mirror_generation)` plus
  narrower family and ref counters derived from the changed paths. An
  unchanged successful sync advances `last_success_at`, not the content token.
  A failed sync keeps the last good generation and reports error and age.

### Web store, scheduler, and sequencing

- **Store.** One external store; `CachedProjectTabState` becomes
  `ProjectTabUiState` (ref, selections, panels, drafts, scroll, editor
  generations), and the eight-entry retention limit goes. Resources are keyed
  `{backend, member, project?, resource, ref?, entity?, query?}`; profiles
  (`key`, `watermark`, `fetch`, `reconcile`) live beside their feature owners.
  Components read through `useResource(profile, selector)` on
  `useSyncExternalStore`, never mirroring a resource into `useState`. Hidden
  tabs keep their current view subscribed; other visited refs stay cached and
  dormant; closing releases. Feature hooks (`useAgentTasks`, `useChatState`,
  `useQuestions`, `useProjectHistory`, consolidation, digest, episodes)
  become selectors plus commands, and their private caches go.
- **Graph session.** Canonical snapshots and manifests move into resources;
  human drafts and preview coordination stay per ref, keeping the existing
  draft reconciliation and preview-input guards. Graph, control, guidance,
  attention, and head stay one atomic resource (the API spec's snapshot rule).
- **Scheduler.** One completion-scheduled timer replaces the two interval
  loops. Payload reads use three slots, at most one for background work,
  leaving room under the browser's six connections for the watermark beat and
  commands; active work first, with aging so hidden projects do not starve.
  Activation paints immediately, promotes queued reads, and sends the
  activation notice without awaiting a sync. Hidden pages pause; resume
  coalesces into one validation. Backend-contact freshness and remote-mirror
  freshness are tracked and shown separately. The scheduler only calls
  profile callbacks; it never branches on resource names (AGENTS.md).
- **One acceptance rule.** Every read captures its key, lifecycle generation,
  and start sequence, and commits only if all still match; starting a newer
  read fences older ones. Observed watermark and committed version are
  tracked separately; only an accepted payload acknowledges a version, so
  failed or superseded reads keep their debt.
- **One mutation path.** Commands declare affected keys, fence their reads,
  and install an operation-id pending overlay; conflicting commands
  serialize. Success replaces the overlay with the server response; failure
  removes only that overlay and keeps newer state and typed text. Reads never
  regress behind an accepted mutation response, and the backend honours that
  floor when serving its mirror.
- **Each race #299 hit is impossible by construction:** tasks are one
  project-wide resource (no branch-keyed copies), there is no restore write
  or active/inactive path, one read owner settles completion, resources queue
  independently, and immutable target keys stop a late watcher response from
  landing on another branch.
- **Stays outside the store:** mutations and explicit probes (they publish or
  invalidate through it), draft previews (demand-only, keyed by input hash),
  PTY sockets, artifact iframe execution and standalone live data, voice
  session watches, and the landing Runs page.

## Open questions

- Should hidden tabs refetch on agent progress? The server design folds
  progress into the task token; decision 3 assumed status changes only.
- Paper: may it open from the store instead of its mandatory entry read? It
  first needs conditional saves against the Paper resource version, because
  saving today writes the stored draft before checking the canonical
  `base_hash`.

## Slices

All slices land as commits in this one PR. Each removes the migrated
resource's old reader in the same slice.

1. **Transport first.** Immutable read generations, generation pinning, and
   concurrency tests; the non-initializing Retry/Resume/chat admission fix.
2. **Watermarks and scheduler.** `resource_versions`, deadlines, the batched
   endpoint, the store and scheduler; migrate questions, consolidation, and
   digest. Verify unchanged beats issue no payload reads.
3. **Tasks and actions.** Tasks, progress, details, and pending overlays for
   every task action; replay each #299 race with deferred responses.
4. **Graph and conversations.** Graph session ownership, chats, watchers,
   episodes, history, refs; remove retention copies and reload bundles.
5. **Everything else.** Remaining resources and Paper; the remote sync
   coordinator and activation sync; retire `/cached/revision` and the old
   polls and events.

Each slice: focused tests and builds, a served-app journey with network,
console, and server logs, phone width 390 px, and on a disposable real-data
copy (local and remote state) the warm-switch paint time, request count,
queue delay, and CPU.
