---
id: S82-view-state-survives-navigation
status: implemented
tier: hermetic
driver: browser
covered_by: none
last_passed: 2026-08-07 — agent-driven at 1280x720 against a real remote project.
  Research restored scroll 1200 after a Chats round trip, the nav returned to the
  DAG subpanel with pan (1280, 1613.5) and zoom ~1.62 intact, Runs opened at the
  top while Research held 800 and then restored its own 300, and no view state
  reached localStorage. Clicks and scrolls were dispatched through the page
  because the browser pane runs hidden, which suppresses rAF-driven scroll events.
invariants: []
---

# Leaving a panel and coming back lands where you left it

Moving to Chats to read something and then returning to Research is a glance,
not a fresh start. The panel a human was reading, the subpanel they had chosen,
and the place they had scrolled or panned to are all part of where they were.
Losing them turns every glance sideways into re-finding the row they were on.

This is view state, not project truth: it lives for as long as the project stays
open in the tab. A reload is a fresh start and lands on Overview at the top.

## UI path — confirmed 2026-08-07

- **Research** in the primary navigation returns to whichever of its two
  subpanels — **Research** or **DAG** — was last open, rather than always the
  path projection. The subpanel switch itself is unchanged.
- The project panel's scroll offset is remembered per view. Returning to a view
  restores it; a view never visited this session opens at the top.
- The DAG remembers its own pan offset and zoom level across a departure and
  return.
- Nothing new appears on screen. There is no control for this, no indicator, and
  no label.

## Setup

A temporary copy of the demo project with enough nodes for the Research path
list and the DAG canvas to both overflow their viewport.

No agent runs.

## Drive

1. Open the project and go to **Research**. Scroll the path list well down.
2. Go to **Chats**, then back to **Research**. The path list is where it was.
3. Switch to the **DAG** subpanel. Pan the canvas away from its origin and zoom
   in with a touchpad pinch.
4. Go to **Runs**, scroll it, then click **Research** in the navigation.
5. Go to **Inbox** and back to **Runs**.
6. Read `localStorage` and confirm no key holds the view, an offset, or the DAG
   viewport.

## Assert

- `research_returns_to_last_open_subpanel` — step 4 lands on DAG, not the path
  projection
- `dag_pan_and_zoom_survive_the_departure` — the canvas scroll offset and zoom
  level from step 3 are restored, within the tolerance of one frame of force
  settling
- `panel_scroll_offset_is_restored_per_view` — Research in step 2 and Runs in
  step 5 both return to their own prior offset, not each other's and not the top
- `unvisited_view_opens_at_the_top`
- `view_state_never_reaches_storage` — nothing about the view, its offset, or the
  DAG viewport is written to `localStorage`; a reload therefore starts fresh
- `no_new_visible_control_or_label`
- `no_console_or_application_request_errors`

## Failure means

Navigation costs the human their place, so moving between panels is expensive
enough to avoid — or view state leaked into storage and a reopened project now
lies about where the human was.

## Known limits

Unpinned DAG nodes re-run their force layout on return, so individual node
coordinates may differ. This scenario asserts the viewport, not the node
positions; pinned positions are S03's promise and already persist.

Opening the DAG through a node's relation row is an explicit request to look
somewhere. That framing wins over the remembered viewport, by design.
