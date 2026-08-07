---
id: S03-views-and-graph-controls
status: implemented
tier: hermetic
driver: browser
covered_by:
  - web/tests/researchProjection.test.mjs
  - web/tests/runProjection.test.mjs
  - web/tests/graphProjection.test.mjs
  - web/tests/dagLayout.test.mjs
  - web/tests/dagZoom.test.mjs
  - web/tests/forceDag.test.mjs
  - web/tests/graphProjectionPerformance.test.mjs
invariants: []
---

# Move between views and work the graph

No agent runs. This is the reading-and-looking half of the app, which is most of
what using it actually is. Projection, layout, zoom, focus, and force behavior
have focused web tests; the complete interaction path still requires a browser
and has no persisted verdict.

Of the browser scenarios, this is the one where a browser is least arguable.
There is no backend fact to fall back on.

## Setup

A temporary copy of the demo project, opened against a data directory that
already holds ingestion-run history: at least one failed Seed or Refresh, one
paused Seed or Refresh, and one retry with its parent. The graph also has an open
Blocker. Runs are rows in the app database, not files in the state repository,
so a fresh copy of the demo project has none of them and step 2 below would have
nothing to sort — produce them beforehand with a fake agent.

No agent runs during the drive itself.

## Drive

1. Open the project. The **Research** projection renders — question-centered
   paths, with unconnected records separated out.
2. Switch to **Runs**. Failed and paused ingestion work plus asserted open graph
   Blockers sort into **Needs action**; retries nest under what they retried.
   Chat and paper-coach tasks do not appear.
3. Back to Research. Open the DAG's **Research flow** columns.
4. Drag a node well away from where the layout put it. Pin it.
5. Switch views and come back.
6. Release that one pin. Then **Release all pins**.
7. Brighten all, dim all.
8. Enter fullscreen. Open a node's details while still fullscreen.
9. Two-finger scroll the canvas. Then pinch to zoom.
10. Scroll the canvas to its boundary and keep going — the page should take over.
11. Click a relation row on a node.
12. Load a large fixture, switch from All to Belief and Action, and continue
    dragging and inspecting nodes while the force layout settles.

## Assert

- `both_projections_render`
- `runs_view_prioritizes_unfinished_ingestion` — failed/paused Seed and Refresh
  attempts plus asserted open graph Blockers appear above the rest; chat tasks
  stay out
- `runs_view_as_of_time_is_truthful` — the timestamp reflects the data, not the
  render
- `research_flow_columns_show_question_depth_then_semantic_stage` — only
  `has_subquestion` assigns question depth; later nodes follow semantic stage,
  not general relation-arrow direction
- `pinned_node_survives_view_switch` — pin state is not lost on remount
- `release_all_pins_clears_every_pin`
- `fullscreen_shows_node_details`
- `two_finger_scroll_did_not_zoom` — ordinary scrolling must not read as a zoom
  gesture
- `pinch_zoom_anchored_at_focal_point`
- `scroll_chains_to_page_at_boundary`
- `relation_row_opens_one_hop_view`
- `repulsion_visibly_changes_spacing`
- `belief_and_action_projection_classifies_edges_once_per_render`
- `large_projection_remains_interactive_while_layout_settles`
- `canvas_allows_dragging_beyond_layout_bounds`
- `no_console_errors`

Do **not** assert node coordinates. The layout is a physics simulation; exact
positions are meaningless and will make this flaky. Assert what a node *is*,
never where it landed.

## Failure means

The part of the app you spend the most time in stopped working, in a way no
backend test would notice — which is not hypothetical, since no backend test
covers any of it.
