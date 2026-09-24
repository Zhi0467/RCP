# An episode timeline shows who ran, and what passed between them

Date: 2026-09-24
Status: design confirmed by the human on 2026-09-24 against a rendered mockup
built from a real Auto-research run (nine orchestrator turns, three workers, five
Experiment episodes), then revised the same day after an xhigh design review
whose five findings were verified against the code. Not yet implemented. The
mockup is kept outside the repository because it embeds production records; the
implementer is given a copy and matches its layout and interaction. Where the
mockup draws a link this contract does not record (a worker stop attributed to
a turn, a watcher firing attributed to one Experiment episode, "read" wording),
this contract wins.

Remaining: everything below, on one pull request, in two implementation slices
(projection, then web) plus the spec update.

Close this handoff when the Auto-research card and the Experiment run detail
both render the roster from the new projection, both have been driven in the
served app against a copy of real data, and the specs below describe the
roster instead of the event list.

## The problem

The current timeline projects one row per stored record and nests rows by
parent task id. On a real run the human could not answer:

1. How many agents ran, what they were called, and how long each lived.
2. Which launch led to which wake.
3. Which failures were recovered, and how.
4. What each party told the other: assignments, messages, and RCP's notices.

The records hold these answers. The projection throws the joins away: workers
are labelled "Worker" without their node, a wake is shown as its own actor
instead of the orchestrator waking, notices repeat task statuses, retries nest
under unrelated rows, and every turn reads "Agent task completed."

## Settled decisions

- **One projection, shaped by actors.** The timeline route keeps its path and
  returns the actor-shaped response below. The event-list model, its render
  configuration, and the nested component are deleted, not kept beside the new
  view. Auto-research and Experiment run detail use the same projection and
  component.
- **Actors are rows.** A human, the orchestrator, each worker (named from its
  assignment's first heading, subtitled by its node), and each Experiment
  episode. Experiment episodes on the same node share one row, in order. In
  `experiment_loop` mode the rows are the Experiment agent itself and one row
  per shell watcher.
- **Spans are what an actor did.** Orchestrator turns, worker attempts,
  Experiment turns, and report turns, each with start, finish, status,
  invocation number when one was spent, error text, and a nullable headline:
  the first sentence of the stored answer, bounded. Report spans carry no
  headline and render as "Report". No wording heuristics beyond sentence
  splitting, and never a trace or status text in its place.
- **What passed between actors is typed, and each item says where it landed.**
  - *Hand-off*: the assignment or goal a turn wrote when it started a worker or
    Experiment, from the recorded command file, attributed to the issuing turn.
  - *Message*: sender, recipient, sent time and span, delivery time and the span
    it was delivered to, and a disposition. Delivery proves allocation, not
    reading, so the words are *delivered with a wake*, *harvested* or *cleared*
    (from the inbox receipt's mode), *delivered to an attempt that failed*, or
    *not delivered*. Never "read".
  - *Signal*: a lifecycle notice (verbatim payload, inline) or a graph watcher
    (armed by a turn, then fired, stopped, or still armed). A watcher names a
    node, not an episode, so its line leaves the node's row. A signal says
    whether it woke a turn or was harvested or acknowledged during a turn
    already running. Only a wake is drawn dashed to the turn's start; in-turn
    consumption is drawn dotted to the moment it was recorded.
- **Nothing is inferred.** A link is drawn only from a recorded id. Where the
  record lacks one, the projection leaves the link null. Known gaps on the
  real run: a replacement Experiment whose `replaces_episode_id` is empty, a
  worker stop with no recorded issuer, and a later worker that took over an
  earlier one's unfinished job. Recording those links is out of scope here.
- **Bodies load on demand.** The response carries bounded previews of
  hand-offs and messages; the full text is read on click from one text
  endpoint. Signal payloads are small and travel inline.
- **Controls stay where they are.** The roster is read-only. Watcher controls,
  the task inspector, and child navigation keep their existing owners; the
  detail panel links to them rather than rebuilding them.
- **The view.** One SVG timeline with a fixed label column: a date row, then a
  time row, and no grid lines except the "now" marker. It zooms with pinch,
  Ctrl/Cmd+wheel, the − and + buttons, and double-click, and pans by dragging or
  sideways scrolling. Presets: whole run and first 90 minutes. Solid lines mean
  "started by"; dashed lines mean "woke the orchestrator". Clickable icons for
  hand-offs, message envelopes on arrows to where they landed, and notice and
  watcher icons open one popover card with the full text or payload. There are
  no commentary captions in the chart. Below the chart: an orchestrator wake
  table (turn, time, cause, what landed, what it did, headline) and a detail
  panel for the selected actor or turn. A summary strip gives counts that add
  up. The mockup is the reference for layout and interaction.

## Wire contract

`GET /api/projects/{project_id}/episodes/{episode_id}/timeline` returns:

```text
episode_id, mode ("auto_research" | "experiment_loop"), generated_at, truncated
members:  [{episode_id, started_at, ended_at|null, continues_episode_id|null}]  # chain, oldest first
actors:   [{actor_id, kind: human|orchestrator|worker|experiment|agent|watcher,
            label, subtitle|null, row_key, owner_episode_id, started_at|null,
            ended_at|null, outcome|null, started_by_span_id|null, links}]
spans:    [{span_id, actor_id, kind: turn|attempt|report, started_at,
            finished_at|null, status, attempt|null, invocation_number|null,
            headline|null, error|null, cause|null, task_id, owner_episode_id}]
handoffs: [{item_id, kind: assignment|goal, from_span_id, to_actor_id, at,
            preview, text_ref}]
messages: [{item_id, from_actor_id, to_actor_id, sent_at, sent_span_id|null,
            delivered_at|null, delivered_span_id|null,
            disposition: wake|harvested|cleared|failed_attempt|undelivered|unknown,
            preview, text_ref}]
signals:  [{item_id, kind: notice|watcher, source_actor_id|null, source_row_key,
            event, recorded_at, landed_at|null, landed_span_id|null,
            landing: woke|harvested|acknowledged|null,
            armed_span_id|null, armed_at|null, state|null, payload}]
marks:    [{item_id, actor_id, kind: started|stop_requested|stopped, at,
            by_span_id|null}]
```

- Ids are stable and prefixed by kind. Any `*_span_id` or `*_actor_id` may name
  a record outside the returned set; the web then draws that endpoint at the
  window's edge rather than dropping the item.
- Joins resolve across the whole continuation chain before any bound applies,
  so a child route moved to a newer member keeps its original issuing span.
  Hand-offs join through `auto_research_child_admission_command()` and its
  planned child id. An Experiment started without a goal has no hand-off.
- **Bound:** `EPISODE_TIMELINE_EVENT_LIMIT` (400) counts spans plus hand-offs,
  messages, signals, and marks, newest first; `truncated` is set when any were
  dropped. Actors are returned when at least one of their spans or items is.
  Preview, headline, and error lengths live in `limits.py`. The page's summary
  counts describe the returned items and say so when truncated.
- **`experiment_loop`:** actors are the human, the Experiment `agent` (its turns,
  retries as later attempts on the same row, reports as `report` spans), and
  one `watcher` actor per shell watcher, spanning armed to completed or
  stopped. A watcher's `started_by_span_id` is its origin turn, and its
  notification is a signal that woke a turn. Continuation boundaries come from
  `members`.
- **Text:** `GET /api/projects/{project_id}/episodes/{episode_id}/timeline/text/{text_ref}`
  with `text_ref` = `handoff:<command_id>` or `message:<message_id>`. It
  resolves only inside the requested episode's chain, under the route's
  existing project membership, and returns 404 when the record belongs
  elsewhere. Response: `{text_ref, kind, owner_episode_id, body, sha256}`, the
  immutable stored content.

## Slices

1. **Projection.** Replace `src/rcp/api/episode_timeline.py` and its response
   models, add the text endpoint, register the route, and rewrite
   `tests/test_episode_timeline_api.py`: one test per contract rule (actor
   kinds, each message disposition, each signal landing, the hand-off join,
   the bound and truncation, a chain with a moved child route,
   `experiment_loop` rows, and text-endpoint ownership with its 404).
2. **Web.** Replace `web/src/timeline.ts` and `web/src/components/EpisodeTimeline.tsx`
   with the roster model and chart, update `web/src/types.ts` and `web/src/api.ts`,
   wire both call sites, and update the web unit and browser tests that route
   the timeline.
3. **Docs.** `docs/specs/api-web-and-desktop-projections.md` (Episode timeline)
   and `docs/specs/interface-and-visual-design.md` (the timeline paragraph).
