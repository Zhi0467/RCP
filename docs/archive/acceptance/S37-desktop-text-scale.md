---
id: S37-desktop-text-scale
status: implemented
tier: hermetic
driver: desktop
covered_by:
  - web/tests/textScale.test.mjs
  - web/tests/desktopRuntime.test.mjs
  - web/src-tauri/src/windows.rs
  - desktop 2026-07-31
last_passed: 2026-07-31 — rebuilt RCP Dev.app, drove the visible control and
  Command-minus/Command-0, verified the 80% and 140% bounds, persisted 110%
  through quit/relaunch and the project index, then restored 100% before exit
invariants: []
---

# Text stays readable throughout the desktop app

Text size is one application preference, not a per-project formatting choice.
Changing it updates the whole RCP interface immediately and the chosen size
survives navigation, project changes, window close and reopen, and application
restart.

The preference changes interface text, including navigation, settings, node
details, and chat. It does not change the DAG's own pan or zoom state, alter
project data, or become part of canonical project settings.

## UI path

Confirmed on 2026-07-31.

- Project **Settings** contains a **Display** section with decrease, reset, and
  increase controls and the current text-size percentage.
- `Command–Minus` decreases the same preference, `Command–0` resets it, and
  `Command–Plus` increases it from anywhere in the Tauri window.
- Each shortcut changes the preference exactly once; the webview's built-in
  zoom handling does not also apply a second change.
- The allowed range is bounded so RCP cannot be made unusably small or large.
- The control is desktop-only and uses native webview zoom, so browser geometry
  and the DAG's independent coordinate system are not rescaled by CSS.
- The preference is local to the application, shared by every RCP project, and
  never written into a project's manifest or `.research/` history.

Deliberately not possible: separate font sizes per project, a shortcut that
changes graph truth, or a saved size that is lost when the desktop app closes.

## Drive

1. Open a project in the desktop app and note the text size in the project
   navigation, a node detail, and a chat.
2. Press `Command–Plus` twice, then move between project panels.
3. Open **Settings → Display** and decrease the size once with the visible
   control.
4. Press `Command–0`.
5. Choose a non-default size, close and reopen the window, switch projects, then
   quit and relaunch RCP.

## Assert

- `command_plus_increases_text_once`
- `command_minus_decreases_text_once`
- `command_zero_restores_default`
- `settings_and_shortcuts_control_one_value`
- `text_scale_applies_across_project_surfaces`
- `dag_pan_and_zoom_are_unchanged`
- `layout_remains_usable_at_both_limits`
- `preference_survives_window_reopen_and_app_restart`
- `preference_is_shared_across_projects`
- `project_state_and_history_are_unchanged`

## Failure means

RCP is hard to read in its native window, a keyboard shortcut double-zooms the
webview, or a display preference leaks into research truth.
