# Graph dreaming: nightly consolidation and operational lessons

Date: 2026-10-03
Status: design settled with the human on 2026-10-02 in a grilling session. The
human asked for one PR, implemented autonomously with Codex astra and Claude
subagents. Nothing is implemented yet.

Decisions: [nightly consolidation writes main](../decisions/2026-10-03-nightly-consolidation-writes-main.md)
and [operational lessons live outside the graph](../decisions/2026-10-03-operational-lessons-live-outside-the-graph.md).
Current behavior is specified in
[graph consolidation and lessons](../specs/graph-consolidation-and-lessons.md);
this file holds only the build plan and what remains.

## Settled with the human

- A member enables a nightly consolidation in project settings with a local
  time. The authorization records the member and expires after
  `CONSOLIDATION_AUTHORIZATION_DAYS` (30). Any member can turn it off or renew it.
- Once a day at that time RCP starts one Work turn. No new main revision since
  the last consolidation means no turn. A missed time runs once when RCP next can.
- The turn lives in one dedicated project chat and starts a fresh provider
  session every night. It runs a pinned official `graph-consolidation` workflow
  with ordinary Work graph authority, applies its Patch inside the turn with a
  keyed `apply`, and writes one HTML report.
- Conflicts with open episode branches are left to semantic merge.
- Proposals are not capped; the workflow guides restraint.
- The report appears in the Inbox until a member keeps or dismisses it. A
  failure appears as a dismiss-only Inbox row listing revisions RCP actually
  committed.
- Lessons: one RCP-owned store per project in SQLite. Any turn with a staged
  command client records one with `lesson add`. The consolidation turn may also
  update and delete agent-written lessons. Every launch gets a pointer to a
  bounded rendered `lessons.md`. Members can list, add, edit, and delete; a
  human-written or human-edited lesson is never changed by an agent.

## Code-level choices (made by the implementer, not the human)

An astra xhigh design review on 2026-10-03 found sixteen P1 gaps; the spec now
states the resolved behavior. Implementation choices:

- No new task contract. The turn is ordinary `work_auto` Work with trigger
  `schedule` (joins `TaskTrigger`). Its extra verbs, watcher refusal, fresh
  session, pinned workflow, and pre-launch authorization check all key off a
  server-owned `consolidation_runs` row bound to the operation id.
- Tables (migration 36 `operational_lessons_v1`, 37 `graph_consolidation_v1`):
  `operational_lessons`, `lesson_command_receipts` (operation, subcommand, key,
  args digest, result); `consolidation_schedules` (authorization id, local
  time, zone, authorizer snapshot, authorized/expires at, next due occurrence,
  covered head, last outcome), `consolidation_runs` (run id, schedule
  occurrence date unique per project, nullable operation id, authorization id,
  input head, kind, outcome fields, verified flag, row state, resolver),
  `consolidation_apply_receipts` (operation, key, digest, retained bytes path,
  source effect id, result). None transfers; all join deletion, alias rewrite,
  backup inventory, and restore detachment.
- Scheduler: a runtime owner modelled on `WatcherPoller`, beside the other
  owners in `api/app.py` (start, update pause/resume, shutdown), gated by
  `seconds_until_automatic_launch`, using `zoneinfo`. It also retries outcome
  settlement and notification observation. Machine keep-awake reuses ordinary
  task demand.
- Keyed apply reuses `_apply_work_patch` and the orchestrator's digest and
  effect-id pattern with operation-scoped receipts; settlement matches by
  operation plus digest.
- Report retention: an open report row nulls the artifact's `expires_at`;
  Dismiss sets it to now plus `RUN_STAGE_RETENTION_DAYS`; keeping from the viewer
  closes the row.
- Notifications: kind `consolidation`, observed by the existing sender with a
  durable marker per run id and per authorization id, enqueued atomically.

## API contract

All routes require project membership; mutations require write admission and a
patch-capable identity.

```
GET    /api/projects/{id}/consolidation
  -> { schedule: Schedule | null, inbox: InboxItem[] }
PUT    /api/projects/{id}/consolidation/schedule   { local_time: "HH:MM", timezone: IANA }
  -> { schedule: Schedule }        # enables or renews; authorizer = acting member
DELETE /api/projects/{id}/consolidation/schedule   -> { schedule: null }
POST   /api/projects/{id}/consolidation/runs/{run_id}/keep     -> { item: InboxItem }  # report rows only
POST   /api/projects/{id}/consolidation/runs/{run_id}/dismiss  -> { item: InboxItem }

Schedule = { authorization_id, local_time, timezone, authorized_by: { user_id, display_name },
             authorized_at, expires_at, expired: bool, next_due_at,
             last_run_at | null, last_outcome: "succeeded"|"failed"|"skipped"|null }
InboxItem = { run_id, kind: "report"|"failure", occurrence_date, created_at,
              operation_id | null, chat_id | null,
              report: { artifact_id, title } | null,
              applied_revisions: [{ revision, summary }], revisions_verified: bool,
              proposals_created: int,
              error: { code, message } | null,
              state: "open"|"kept"|"dismissed" }
  # GET returns only open items; expiry is read from Schedule (expired, expires_at).

GET    /api/projects/{id}/lessons          -> { lessons: Lesson[] }
POST   /api/projects/{id}/lessons          { text }  -> { lesson: Lesson }
PATCH  /api/projects/{id}/lessons/{lesson_id} { text } -> { lesson: Lesson }
DELETE /api/projects/{id}/lessons/{lesson_id} -> {}

Lesson = { lesson_id, text, created_at, updated_at,
           author: { kind: "agent", operation_id } | { kind: "human", user_id, display_name },
           human_owned: bool }
```

Staged command (owners with a served mutating mailbox, listed in the spec):
`lesson add --key <key> --text <text>`; consolidation only:
`lesson list [--cursor <c>]` (bounded page with ids and ownership),
`lesson update --key <key> --id <lesson_id> --text <text>` and
`lesson delete --key <key> --id <lesson_id>`. Limits in `limits.py`:
`LESSON_TEXT_MAX_CHARS` 600, `LESSONS_PER_PROJECT_MAX` 200,
`LESSONS_RENDER_MAX_BYTES` 32 KiB (newest human-owned first, then newest
agent-written, truncated with a stated omission count).

## Slices

1. **Backend: lessons** (Codex). Storage and receipts, migration 36, limits,
   routes, the `lesson` verb on the owner matrix in the spec, rendered
   `lessons.md` with one prompt pointer, inventories and restore, tests.
2. **Backend: consolidation** (Codex, parallel to 1). Storage, migration 37,
   run-bound owner, scheduler, scheduled-chat admission, pre-launch
   authorization check, keyed `apply` with receipts, outcome settlement, report
   retention, routes, notification observation, inventories and restore, tests.
3. **Workflow prose** (Claude): `src/rcp/skills/workflows/graph-consolidation`.
4. **Web** (Claude subagent, after 1 and 2 land): types, a Nightly consolidation
   card in Project settings, Inbox section with Keep, Dismiss, open-report, and
   expiry renewal, a Lessons card in Project settings, notification kind and
   deep link, tests.
5. **Docs** (Claude): spec, design boundary, AGENTS pointer, provider-command
   spec lines.

## Close this handoff when

- on disposable data, an enabled schedule fires at its time, consolidates a
  seeded duplicate pair, commits one revision, and leaves one report row that
  Keep moves into Artifacts;
- an unchanged main the next night starts no provider turn;
- a failed turn leaves a dismiss-only row naming the committed revisions;
- an expired authorization starts nothing and shows in the settings card;
- a Work turn's `lesson add` appears in the Lessons card and in the next
  launch's `lessons.md`; a human-edited lesson refuses an agent update.
