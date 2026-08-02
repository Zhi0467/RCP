---
id: S45-floating-node-window-dock
status: implemented
tier: hermetic
driver: browser
covered_by: none
last_passed: 2026-08-02
invariants: [8, 10, 11]
---

# Dock a floating node window without closing it

A floating node detail can be temporarily removed from the research canvas
without closing its state. A visible `−` control docks the window into one
shared, project-level window dock. The dock is a presentation shelf for node
windows; it does not delete the node, alter the graph, or affect chat behavior.

## UI path

The shared node dock sits directly below the project header/navigation and is
available from every project view while the project is open. It is hidden when
there are no docked node windows.

- Open a node from **Research**. Its detail appears as a floating window with a
  `−` button beside the close control.
- Click `−`. The node detail leaves the canvas and becomes one labeled item in
  the shared dock. The selected node and any staged draft edits remain intact;
  this is not a close or navigation action.
- Click the dock item to restore that exact node detail as a floating window.
  Its content and staged state return, and the dock item is removed.
- Docking multiple nodes creates one item per node window. Restoring one does
  not restore or close the others. Switching project views preserves the dock;
  leaving the project clears the presentation without changing graph history.
- The dock item label identifies the node title and has an accessible restore
  label. The window control has an accessible dock label.

The existing close control remains distinct from docking: `−` means “put this
node window in the project dock”; close keeps its existing behavior. Docking
does not cancel agent work or change any staged human draft.

## Drive

1. Open a project's **Research** view and click a node.
2. Click the node detail's `−` control and confirm a labeled item appears in the
   top-level project dock while the underlying view remains usable.
3. Restore the node detail from the dock and confirm its title and staged draft
   state are unchanged.
4. Dock a second node, switch between project views, restore both in either
   order, and confirm that each window remains independent.

## Assert

- `floating_node_has_dock_control`
- `docking_removes_window_but_preserves_node_state`
- `project_node_window_dock_is_visible_at_top_level`
- `docked_node_restores_exact_detail`
- `multiple_docked_nodes_restore_independently`
- `dock_survives_project_view_changes`
- `docking_does_not_change_graph_or_draft_authority`
- `dock_labels_identify_the_node`
- `no_console_or_server_errors`

## Failure means

Clicking `−` closes the node, loses its staged state, changes graph history,
hides the dock from another project view, or restores the wrong node.
