# An episode timeline shows who ran, and what passed between them

Date: 2026-09-24
Status: design confirmed by the human on 2026-09-24 against a rendered mockup
built from a real Auto-research run (nine orchestrator turns, three workers, five
Experiment episodes). Not yet implemented. The mockup is kept outside the
repository because it embeds production records; the implementer is given a
copy and must match it.

Remaining: everything below, on one pull request, in two implementation slices
(projection, then web) plus the spec update.

Close this handoff when the Auto-research card and the Experiment run detail
both render the roster from the new projection, the served app has been driven
against a copy of real Auto-research data, and the specs below describe the
roster instead of the event list.

## The problem

The current timeline projects one row per stored record and nests rows by
parent task id. On a real run the human could not answer:

1. How many agents ran, what they were called, and how long each lived.
2. Which launch led to which wake.
3. Which failures were recovered, and how.
4. What each party told the other: assignments, messages, and RCP's notices.

The records hold every one of these answers. The projection throws the joins
away: workers are labelled "Worker" without their node, a wake is shown as its
own actor instead of the orchestrator waking, notices repeat task statuses,
retries nest under unrelated rows, and every turn reads "Agent task completed."

## Settled decisions

- **One projection, shaped by actors.** `GET /api/projects/{project_id}/episodes/{episode_id}/timeline`
  keeps its route and is replaced by an actor-shaped response. The event-list
  model, its render configuration, and the nested component are deleted, not
  kept beside the new view. Auto-research and Experiment run detail use the
  same projection and component.
- **Actors are rows.** A human, the orchestrator, each worker (named from its
  assignment's first heading, subtitled by its node), and each Experiment
  episode. Experiment episodes on the same node share one row, in order. An
  Experiment episode's own detail shows its own turns and shell watchers as
  its rows.
- **Spans are what an actor did.** Orchestrator turns, worker attempts,
  Experiment turns, and report turns, each with start, finish, status,
  invocation number when one was spent, error text, and a headline: the first
  sentence of the stored final answer (label `answer`, invariant 11), bounded.
  No wording heuristics beyond sentence splitting.
- **What passed between actors is typed, and each item says where it landed.**
  - *Hand-off*: the assignment or goal a turn wrote when it started a worker or
    Experiment, from the recorded command file, attributed to the issuing turn.
  - *Message*: sender, recipient, sent time and span, delivery time and the span
    that received it, and a landing state: read by a turn, delivered into an
    attempt that failed, never delivered, or pending.
  - *Signal*: a lifecycle notice (verbatim payload) or a graph watcher firing
    (its condition and the turn that armed it), with the time it landed and
    whether it woke a turn or was read during one already running.
- **Nothing is inferred.** A link is drawn only from a recorded id. Where the
  record lacks one, the projection omits the link. Known gaps on the real run:
  a replacement Experiment whose `replaces_episode_id` is empty, a worker stop
  with no recorded issuer, and a later worker that took over an earlier one's
  unfinished job. Recording those links is out of scope for this pull request.
- **Bodies load on demand.** The response carries bounded previews. The full
  text of a hand-off or message is read on click from one read-only text
  endpoint scoped to the episode.
- **The view.** One SVG timeline with a fixed label column: a date row, then a
  time row, and no grid lines except the "now" marker. It zooms with pinch,
  Ctrl/Cmd+wheel, the − and + buttons, and double-click, and pans by dragging or
  sideways scrolling. Presets: whole run and first 90 minutes. Solid lines mean
  "started by" and dashed lines mean "woke the orchestrator". Clickable icons
  for hand-offs, message envelopes on arrows to where they landed, and notice
  and watcher icons open one popover card with the full text or payload. There
  are no commentary captions in the chart. Below the chart, an orchestrator
  wake table (turn, time, cause, what landed, what it did, headline) and a
  detail panel for the selected actor or turn. A summary strip gives counts
  that add up. The mockup is the reference for layout and interaction.

## Slices

1. **Projection.** Replace `src/rcp/api/episode_timeline.py` and its response
   models, add the text endpoint, register the route, and rewrite
   `tests/test_episode_timeline_api.py` against a fixture that exercises every
   actor, hand-off, message landing state, and signal kind. Keep the existing
   event bound and `truncated`, and the chain (continuation) behavior.
2. **Web.** Replace `web/src/timeline.ts` and `web/src/components/EpisodeTimeline.tsx`
   with the roster model and chart, update `web/src/types.ts` and `web/src/api.ts`,
   wire both call sites, and update the web unit and browser tests that route
   the timeline.
3. **Docs.** `docs/specs/api-web-and-desktop-projections.md` (Episode timeline)
   and `docs/specs/interface-and-visual-design.md` (the timeline paragraph).
