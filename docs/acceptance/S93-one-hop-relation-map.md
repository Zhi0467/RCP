---
id: S93-one-hop-relation-map
status: implemented
tier: hermetic
driver: browser
covered_by: web/tests/relationMap.test.mjs, web/tests/nodeRelationWindows.test.mjs,
  web/tests/floatingWindow.test.mjs
last_passed: 2026-08-08 — agent-driven at desktop, 700px, and 400px widths
  against the live 34-revision plasticity-loss project. A four-edge Experiment
  rendered independent vertical branches; companion replacement and focus
  raising kept exactly two windows; expanded inspection, Escape, the close
  control, and responsive card stacking passed with no console or request errors.
invariants: []
reported_by: human, 2026-08-08
---

# Read a node's immediate structure without leaving it

Confirmed by the human on 2026-08-08.

A node detail presents every immediate graph relation as one stable vertical
map rather than a text inventory. Incoming neighbors sit above the focused
node, outgoing neighbors sit below it, and labelled arrows make both relation
type and direction readable. A neighbor connected by several relations appears
once with each relation represented; nothing is truncated, grouped behind a
count, or moved into a second scroll area.

The map supports comparison without turning node inspection into navigation.
Clicking a neighbor opens one companion node window beside the originating
window. At most two node windows are open: another neighbor replaces the
companion, while selecting an already-open node raises its window. When the
viewport cannot hold two readable windows side by side, both remain open and
the companion overlaps the original with a visible offset instead of shrinking
either window or discarding the original.

A compact zoom control expands the same one-hop map into a full-screen overlay
without changing the active project view. Clicking a node there opens one
compact, read-only inspection card inside the overlay; selecting another node
updates that card without recentering the graph. The card offers **Open node
window** for full details. Closing the overlay returns to the same windows and
scroll positions. The relation map does not add edge authoring, judgment, or
other graph authority.

## UI path

1. Open **Research** and open a node with incoming and outgoing relations,
   including two relations to the same neighbor and enough relations to make
   its detail window scroll.
2. Read the **Relations** section, click a neighbor, then click a different
   neighbor from the originating window. Bring the original window forward by
   selecting its node in the map.
3. Narrow the viewport until two node windows no longer fit side by side.
4. Return to the originating window, expand its relation map, select two nodes
   in turn, and use **Open node window** from the inspection card.
5. Close the full-screen overlay with its close control and with Escape.

## Assert

- `relation_map_has_stable_vertical_direction` — incoming neighbors render
  above the focused node and outgoing neighbors below it, with arrow direction
  and relation labels visible.
- `relation_map_shows_every_one_hop_edge` — every immediate edge appears once,
  including multiple labelled edges to one deduplicated neighboring node, with
  validation warnings attached to their edge.
- `relation_map_uses_the_detail_scroll` — a dense map grows with the section;
  it has no truncation, hidden overflow, pan or zoom canvas, or nested scrollbar.
- `neighbor_opens_one_companion_window` — a neighbor opens beside the original,
  a second choice replaces only the companion, and choosing an already-open
  node raises rather than duplicates it.
- `narrow_view_keeps_both_windows_reachable` — the companion overlaps with a
  visible offset and both independently draggable windows remain reachable.
- `expanded_map_stays_in_context` — expansion opens a full-screen overlay over
  the unchanged project view and closing it restores both windows and their
  scroll positions.
- `expanded_node_card_is_read_only` — selecting a node updates one compact card
  without recentering the one-hop graph; the card exposes **Open node window**
  and no editing or authority action.
- `relation_map_is_keyboard_and_screen_reader_legible` — node actions and the
  expand and close controls have visible focus and explicit accessible names;
  Escape closes only the overlay.
- No console, network, or server error occurs.

## Failure means

Understanding a node's immediate structure still requires mentally parsing a
relation list or leaving the current view; dense or repeated relations vanish;
comparison destroys the original node window; or the expanded map becomes a
second graph-authoring surface.
