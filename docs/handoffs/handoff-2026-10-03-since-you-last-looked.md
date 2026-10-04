# Since you last looked

Date: 2026-10-03
Status: design settled with the human on 2026-10-03 in a grilling session,
and revised after an astra design review the same day. The human asked for
implementation with Codex astra and Claude subagents, with review rounds and a
served-app check. The backend now has the event log, transactional operational
writers, independent graph projector, digest and Caught up routes, project-card
counts, and migration/transfer/restore integration. Backend focused/regression
checks and a seeded HTTP journey have passed. The Web slice is owned by a
parallel implementation.

Decision: [the digest moves only when you say you caught up](../decisions/2026-10-03-digest-moves-only-on-caught-up.md).
Current backend behavior is recorded in
[API, Web, and desktop projections](../specs/api-web-and-desktop-projections.md).
Delete this file after the parallel Web slice and integrated journey are complete.

## Why

Overnight work (Auto-research, nightly consolidation, compute jobs) changes a
project while nobody watches. Today a member who comes back must open the
Inbox, the history, the Runs page, and the graph separately to learn what
happened. The scarce resource is the human's attention, so RCP should answer
one question on return: what changed since I last looked, and what needs me.

## Settled with the human

- **Per member, per project.** Each member has their own marker for each
  project. Stored on the server, so it follows the member across devices.
- **The marker moves only on "Caught up".** Opening the project, the Overview,
  or the graph does not move it. Pressing Caught up records the exact cursor
  of the digest that was on screen, never "now", so anything that arrived
  after the digest was loaded stays new.
- **Starts empty.** A member with no marker gets one at the current cursor on
  first read and sees an empty digest. No backfill.
- **Three groups:**
  1. **Needs you.** New since the marker and still waiting: pending
     Proposals, Decisions awaiting a choice (`ready` or `revisit`), and open
     `ask` questions.
  2. **Changed on main.** Accepted main revisions after the marker, grouped by
     who or what changed them, for example "Nightly consolidation: 12 edits →
     report", "Episode *title* merged: 7 edits", "*Member*: 3 edits", "Chat
     *title*: 2 edits", "Ingestion: 4 edits". A node appears in one group only,
     the latest that touched it.
  3. **Ran.** Episodes that ended, compute jobs that ended, failed non-chat
     tasks, and new reports (consolidation reports, episode reports).
- **Branch edits are one line per episode** ("*Episode*: 14 edits on its
  branch"), never node by node, until the branch merges into main.
- **Excluded:** lessons, chat messages and chat turns as such (a chat Work
  turn's main edits still count under Changed on main), and the viewer's own
  direct edits.
- **No new push notification.** The digest is pull only.
- **UI:**
  - Overview: a card at the top with the three groups and a **Caught up**
    button. Hidden when the digest is empty.
  - Research graph: a dot on each node changed on main since the marker, until
    Caught up.
  - Space landing page: a count on the project card, for example "5 new".
    Zero shows nothing.

## Build plan

An astra xhigh design review on 2026-10-03 found that timestamps and history
replay cannot carry this: a remote job's exit can be recorded after a Caught
up with an older `ended_at`, branch revisions overlap main numbers, Decisions
have no `raised_rev`, and every history reader replays the whole log. The plan
below replaces both with one append-only event log in SQLite.

### One event log, one sequence

- New table `digest_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id,
  kind, item_id, target, source_key, source_label, actor_user_id,
  node_ids_json, payload_json, created_at)`. SQLite has one writer, so `seq`
  follows commit order. A digest read sees every event with `seq` up to the
  maximum in its own read snapshot, and every later commit gets a larger
  `seq`. That ordering, not any timestamp, is what Caught up acknowledges.
- New table `digest_marks(project_id, user_id, seq, marked_at)`, primary key
  `(project_id, user_id)`. Migration 38 `digest_v1` creates both.
- **Operational events** are inserted in the same SQLite transaction as the
  state change they describe:
  - a question is created, or leaves the open state;
  - an episode ends;
  - a compute job reaches a terminal status (on the write that records it,
    whatever `ended_at` it carries);
  - a non-conversation task fails;
  - a consolidation run settles (report or failure);
  - an episode report is published.
  Each writer is a store method; one helper in the digest storage module
  appends the row. No timestamp comparison decides coverage.
- **Graph events** come from a projector that follows accepted transitions,
  like the notification sender, but with its own head and no shared state.
  - It listens on `on_accepted_transition`, and catches up on startup, so a
    crash between the canonical commit and the event insert only delays
    events.
  - Per accepted revision after its stored head, it diffs the before and
    after boundary states and writes one `graph_change` event: source,
    touched node ids (including removed nodes and both endpoints of changed
    edges), and Proposal or Decision entries into and out of human attention
    (`project_graph_attention`, Proposals and ready/revisit Decisions only).
  - The comparison helper is extracted from `build_semantic_delta` into the
    history delta module and shared with `branch_changes`; no second differ.
  - Branch targets: one `branch_change` event per accepted branch revision,
    with the episode id and an edit count, so the digest shows one line per
    episode. Confirm the branch history fires the same hook; if not, add it
    at the branch append point.
  - If the stored projector head is not a prefix of the current history
    (restore or reset), the projector moves its head to the current head,
    writes no events, and logs it.
  - Replay cost stays where the notification sender already pays it: only on
    a signalled or lagging project, never in a request.
- **Attribution** is decided once, at projection time, by precedence: branch
  merge (episode), consolidation run operation (nightly consolidation, with
  its report), human producer (that member), ingestion (`seed`/`refresh`), a
  captured conversation Work task (that chat, joined through `graph_runs`),
  another agent producer (Agent), system (System), and legacy unattributed
  human Patches (Unattributed). `source_key` is stable; labels are display
  only. A missing task row loses the enrichment, never the event.

### Digest read

- `GET /api/projects/{id}/digest` reads in one SQLite read transaction: the
  member's mark, events after it, and the maximum `seq`. It returns that
  maximum as the cursor.
  - Needs you: attention entries whose latest event is an entry, still
    present in the current graph snapshot; open questions (pending and not
    withdrawn); episodes that currently need the human (the same pure health
    predicate the notification sender uses for `episode_needs_action`, which
    covers a ready Decision on an unmerged Auto-research branch).
  - Changed on main: `graph_change` events grouped by `source_key`. A node
    id is listed under the latest event that touched it. Events whose actor
    is the viewer as a direct human edit are dropped, but an earlier agent
    touch of the same node still shows.
  - Branch lines: `branch_change` counts per episode.
  - Ran: the operational events.
  - Changed node ids: the union, filtered to nodes on the current main
    graph, for the Research dots.
- A member with no mark gets one at the current maximum `seq` with an
  atomic insert-if-absent inside the read path, rechecking project
  membership. Listing projects never creates a mark.
- `POST /api/projects/{id}/digest/caught-up {seq}`: stores
  `max(stored, seq)`; refuses a `seq` above the current maximum. Uses
  `acting_user()`, skips the project work fence like the chat read marker,
  and keeps global maintenance admission and POST origin/JSON checks.
- `ProjectCard.digest_count` on `GET /api/projects`: one batched SQL count of
  rendered digest lines from the event log for the visible projects, with the
  same grouping function the digest uses. No graph read, no replay. No mark
  means zero.
- The notification sender is not called and its observations are not read.
  Only pure helpers (attention membership, labels, deep links) are shared.

### Storage obligations

- Project transfer excludes `digest_events` and `digest_marks`; target
  members start empty. Add both to the transfer disposition table.
- Project delete and project-id rewrite cover both tables.
- Register migration 38, update the fresh and upgraded restore schema
  hashes, and add populated rows to a backup/restore round trip. The existing
  frozen upgrade fixtures stay as they are.

### Web

- The project session owns digest state: load, refetch on revision or
  operational change, and Caught up. Requests carry a generation; a late
  response for an older generation or another project is dropped.
- Caught up posts the cursor of the digest on screen, then refetches. Items
  that arrived after that cursor stay.
- `ProjectOverview.tsx`: the card at the top, hidden when empty.
- `DagView` and Research cards: a dot class on changed node ids, on the main
  target only.
- `ProjectLanding.tsx`: the count on the card meta line; updated after a
  Caught up.

## Checks

- Event log: a terminal job recorded after a Caught up with an older
  `ended_at` still appears; a writer that commits after a digest read
  appears in the next one.
- Projector: catch-up after a missed signal; reset/restore rebaseline;
  touched ids for an edge change, a node removal, and a Proposal-only
  change; attention entry for created-ready, open-to-ready,
  decided-to-revisit, and no entry for a title edit while ready.
- Attribution precedence, including legacy and system Patches.
- Marks: baseline, monotonic, refuse ahead, two members independent.
- Transfer exclusion, delete, id rewrite, migration and restore hashes.
- Landing count makes no history call (instrument it).
- Web: the card, dots, and count over payloads; late responses in both
  orders; Caught up keeps an item that arrived meanwhile.
- Served app on seeded data: return after a consolidation run, an episode
  merge, a branch in progress, and a finished job; press Caught up; a second
  member's digest is unchanged.

## Remaining

- Complete and verify the parallel Web slice: card, dots, landing count, and
  response-generation handling.
- Drive the combined browser journey with consolidation, episode merge, branch
  progress, and a finished job. The backend HTTP journey passed on disposable
  data: Caught up retained a later event and did not move a second member's mark.
- Delete this handoff once the integrated journey and review are complete.

The backend behavior is recorded in
[API, Web, and desktop projections](../specs/api-web-and-desktop-projections.md#since-you-last-looked).
