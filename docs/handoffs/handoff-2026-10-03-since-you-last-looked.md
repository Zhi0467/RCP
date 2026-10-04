# Since you last looked

Date: 2026-10-03
Status: design settled with the human on 2026-10-03 in a grilling session. The
human asked for implementation with Codex astra and Claude subagents, with
review rounds and a served-app check. Nothing is implemented yet.

Decision: [the digest moves only when you say you caught up](../decisions/2026-10-03-digest-moves-only-on-caught-up.md).
When this lands, current behavior moves into
[API, Web, and desktop projections](../specs/api-web-and-desktop-projections.md)
and this file is deleted.

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

### Storage

- New table `digest_marks(project_id, user_id, revision, transition_id, at,
  marked_at)`, primary key `(project_id, user_id)`. Migration 38
  `digest_marks_v1`. Follow `chat_reads` (migration 26) for shape and for
  every place a per-member project table must appear: project transfer,
  project delete, project-id rewrite, schema normalization, and the restore
  schema registry hashes. Check whether the upgrade fixture boundary from
  #248 needs a new entry.
- `revision` and `transition_id` are the main graph head the digest showed.
  `at` is the server time the digest's operational reads started.
- The marker never moves backward. A Caught up with a cursor older than the
  stored marker is a no-op; a cursor ahead of the current main head is
  refused.

### Digest service

- One module owns the digest (for example `src/rcp/digest.py`), with a thin
  route module. It reads; it never writes the graph.
- Graph groups come from main history after the marker revision, skipping
  rejected revisions. Attribution comes from the Patch: `branch_merge`
  (episode merged), the consolidation run's operation id (nightly
  consolidation, linked to its report), `authorized_by` with no task (a
  member), a chat Work task (that chat), `seed`/`refresh` (ingestion), and
  anything else as "Agent".
- Needs-you items come from the current graph and the questions table,
  filtered to those raised after the marker (`raised_rev`, `created_at`).
  Resolved items drop out.
- Ran items come from `episodes.ended_at`, `compute_jobs.ended_at`,
  `graph_runs.finished_at`, `consolidation_runs`, and `episode_reports`, after
  `at`.
- Operational timestamps are overlap tolerant: an item may reappear once, but
  must never vanish unseen.
- Reuse the notification sender's item builders and the history readers
  where they fit. Do not add a second graph differ beside `branch_changes` and
  `revision_summaries`.

### API

- `GET /api/projects/{id}/digest` returns the cursor, the stored marker, the
  three groups, the branch lines, and the changed node ids. Inserts the
  baseline marker when the member has none.
- `POST /api/projects/{id}/digest/caught-up` with the cursor from that GET.
  Skips the write-admission fence like the chat read marker does.
- `ProjectCard.digest_count` on `GET /api/projects`. The landing page must not
  replay graph history to compute it.
- Membership gates as for other project routes.

### Web

- `ProjectOverview.tsx`: the digest card at the top, hidden when empty.
- `DagView` and the Research cards: a dot class on changed node ids.
- `ProjectLanding.tsx`: the count on the card meta line.
- Caught up clears the card, the dots, and the count without a reload.

## Checks

- Backend: marker lifecycle (baseline, monotonic, refuse ahead), each group's
  attribution, a node appearing once, branch lines, exclusions, transfer and
  delete of marks, migration and restore registry.
- Web: the card, dots, count, and Caught up, as unit tests over the payload.
- Served app on seeded data: return to a project after a consolidation run,
  an episode merge, and a finished job; see each group; press Caught up; see
  it clear; a second member's marker is unaffected.

## Remaining

Everything above.
