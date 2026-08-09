---
id: S107-open-project-tabs
status: implemented
tier: hermetic
driver: browser + desktop
covered_by: web/tests/projectTabs.test.mjs
invariants: [8]
last_passed: 2026-08-09 — isolated browser and RCP Dev drives verified ordered
  tabs, project-local view restoration, close and reload semantics, real macOS
  shortcuts, and window reopen persistence; a follow-up served-browser drive at
  900px verified proportional compression with a wider active tab and no dock
  overflow or console errors; full web and backend suites passed.
---

# Keep several projects open in one RCP window

Opening another project is a change of focus, not the end of the project that
was already open. Every project opened during the current RCP session stays in
one project dock until the human closes that tab. Switching projects does not
cancel or pause background work, discard a staged human draft, or make the
project index the only route between projects.

## UI path — confirmed 2026-08-09

- A compact browser-tab-style project dock starts immediately to the right of
  the existing back arrow in the project header. It uses RCP's existing paper,
  ink, rules, and oxblood active treatment; it does not add a second app header
  or repeat project metadata.
- Each tab is named with the project's display name. The active tab is visually
  and accessibly selected. Long names truncate. The dock has a fixed maximum
  span: crowded inactive tabs shrink proportionally while the active tab keeps
  a wider share, and the dock never displaces the project actions on the right.
- Clicking a project card or a project in the cross-project Experiment board
  adds it to the end of the dock and activates it. Opening a project already in
  the dock activates the existing tab without moving or duplicating it.
- The dock remains visible on the project index, beside the RCP index control,
  with no project selected. `Command–T` returns to that index from anywhere in
  the desktop app without closing the open project tabs.
- `Option–Command–Left` and `Option–Command–Right` activate the previous and
  next open project tab. Navigation wraps at the two ends. These shortcuts do
  not run while focus is in an editable text control.
- Every tab has an accessible close control. Closing an inactive tab leaves the
  current project unchanged. Closing the active tab activates the tab to its
  right, otherwise the one to its left; closing the last tab returns to the
  project index.
- Closing a tab means only “remove this project from the dock.” It never invokes
  **Delete project**, changes canonical project state, clears a staged human
  draft, or stops a task. Deleting a project from the index removes any tab for
  it as part of the already-confirmed deletion flow.
- Each open tab remembers the project panel and in-session view state where the
  human left it. Closing the tab discards that ephemeral view state; opening the
  project again starts at Overview while durable project state and staged drafts
  remain. The open-tab list is session state: a page reload or full app
  quit/relaunch starts on the index with an empty dock, while hiding and
  reopening the existing desktop window preserves it.

Deliberately not possible: reordering tabs, closing a tab as a synonym for
deleting a project, or keeping multiple project trees mounted and polling in the
background merely because their tabs are open.

## Drive

1. Start RCP on the project index with three registered projects. Open the first
   project, change to Research, scroll it, and stage one unsynced human edit.
2. Return to the index with `Command–T`, then open the second and third projects.
3. Use both Option–Command arrow shortcuts across the dock ends and confirm the
   sequence wraps. Focus a text input and repeat the shortcuts.
4. Return to the first project and confirm its Research panel, scroll position,
   and staged edit are intact.
5. Close the inactive second tab, then close the active first tab. Close the
   final tab.
6. Reopen the first project, hide and reopen the desktop window, then quit and
   relaunch RCP.
7. Open a project, return to the index, and delete that project through its
   existing confirmed deletion flow.
8. Repeat the dock interactions at a narrow width and inspect console, network,
   and server output.

## Assert

- `opening_projects_appends_one_tab_each`
- `reopening_a_docked_project_neither_duplicates_nor_reorders_it`
- `project_index_keeps_the_dock_visible`
- `command_t_returns_to_index_without_closing_tabs`
- `option_command_arrows_wrap_through_open_projects`
- `project_shortcuts_ignore_editable_text_controls`
- `switching_tabs_restores_each_projects_in_session_view_state`
- `closing_an_inactive_tab_keeps_the_active_project`
- `closing_the_active_tab_selects_the_declared_neighbor`
- `closing_the_last_tab_returns_to_the_index`
- `closing_a_tab_changes_no_project_draft_task_or_canonical_state`
- `deleting_a_project_removes_its_tab`
- `window_hide_preserves_tabs_but_reload_and_relaunch_clear_them`
- `narrow_layout_keeps_the_dock_and_project_actions_operable`
- `no_console_network_or_server_errors`

## Failure means

RCP still makes project switching a repeated trip through the index, a close
control is mistaken for deletion or task cancellation, keyboard navigation
steals input editing, or a convenient dock becomes a second source of project
truth.
