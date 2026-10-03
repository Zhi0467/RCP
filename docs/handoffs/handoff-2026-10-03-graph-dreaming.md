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

- Capability: a new ordinary-profile task contract `consolidate` beside
  `work_auto` (`core/authority.py`). It has `work_auto`'s graph authority,
  `trigger="schedule"`, and `authorized_by` set to the schedule's authorizer,
  re-checked for membership at dispatch and at Apply. Its verb set is exactly
  `validate`, `apply`, `lesson` (add/update/delete); no `ask`, no `launch`.
- Idempotency for the keyed `apply` is keyed by operation id, not episode id,
  and the end-of-turn settlement skips a consumed `patch.json`.
- The dedicated chat id is `uuid5(NAMESPACE, "rcp:consolidation:{project_id}")`,
  `chat_scope="project"`, title "Graph consolidation". Each scheduled turn forces
  a fresh native session through the existing fresh-session path.
- The workflow is pinned like the episode report (bypassing project skill
  defaults), so a project need not enable it.
- The report is an ordinary turn artifact at the turn artifacts root named
  `consolidation-report.html`; the run row stores its artifact id. Keep uses the
  existing artifact keep path.
- The scheduler is a new runtime owner modelled on `WatcherPoller`: a daemon
  thread passing every `CONSOLIDATION_POLL_SECONDS` (60), gated by
  `seconds_until_automatic_launch`, started, paused, resumed, and stopped with
  the other owners in `api/app.py`. Due times live in SQLite; an overdue row
  fires once and the next due time is computed from the actual run time.
- Storage: migration 36 `operational_lessons_v1`, migration 37
  `graph_consolidation_v1` (schedules and runs). Lessons transfer with a
  project; schedules and runs are excluded from transfer. Project deletion and
  project-id alias rewrite cover all three tables.
- Notifications: one new kind `consolidation` (report ready, failure,
  authorization expiring), on by default.
- A running consolidation adds a keep-awake reason in `machine_power`.

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

Schedule = { local_time, timezone, authorized_by: { user_id, display_name },
             authorized_at, expires_at, expired: bool, next_due_at,
             last_run_at | null, last_outcome: "succeeded"|"failed"|"skipped"|null }
InboxItem = { run_id, kind: "report"|"failure", created_at, operation_id, chat_id,
              report: { artifact_id, title } | null,
              applied_revisions: [{ revision, summary }],
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

Staged command (consolidation owner and every owner with a staged command
client): `lesson add --key <key> --text <text>`; consolidation only:
`lesson update --key <key> --id <lesson_id> --text <text>` and
`lesson delete --key <key> --id <lesson_id>`. Limits in `limits.py`:
`LESSON_TEXT_MAX_CHARS` 600, `LESSONS_PER_PROJECT_MAX` 200,
`LESSONS_RENDER_MAX_BYTES` 32 KiB (newest human-owned first, then newest
agent-written, truncated with a stated omission count).

## Slices

1. **Backend: lessons** (Codex). Storage, migration 36, limits, routes, the
   `lesson` verb on every staged command client, rendered `lessons.md` staged
   into every launch with one prompt pointer, transfer and deletion inventories,
   tests.
2. **Backend: consolidation** (Codex, parallel to 1). Storage, migration 37,
   `consolidate` contract, scheduler owner, dispatch into the dedicated chat with
   a fresh session and pinned workflow, keyed `apply`, run settlement into report
   or failure rows, routes, notification kind, keep-awake reason, inventories,
   tests.
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
