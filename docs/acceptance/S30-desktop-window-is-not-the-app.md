---
id: S30-desktop-window-is-not-the-app
status: implemented
tier: hermetic
driver: desktop
covered_by:
  - web/src-tauri/src/lib.rs
  - web/src-tauri/src/windows.rs
  - desktop 2026-08-08 — live close, reopen, browser-start, and second-launch drive
invariants: [8]
last_passed: 2026-08-08 — a desktop-started Codex Discuss task stayed active after
  Close and completed while hidden; reopen showed the result, a second launch kept
  one shell and backend, and a browser-started task appeared on desktop reopen
---

# Closing the desktop window never cancels agent work

Every agent invocation is durable background work, and closing the surface it
was launched from must not cancel it. In a browser that follows from the tab and
the server being different things. On a desktop the window looks like the
application, so the promise has to be made deliberately.

Closing the window hides RCP. Agent tasks continue. Reopening shows their
current status, including tasks that started, finished, or failed while no
window was visible. Launching the application a second time focuses the window
that already exists instead of starting anything.

**Showing the window is what makes it truthful.** The human never sees a stale
frame and then watches it correct itself. That matters because the only
mechanism that could keep a hidden window current is the task poll in
[App.tsx](../../web/src/App.tsx), and it has two limits: it runs only
while a task is already active, and it is a timer chain inside a webview that is
occluded for exactly as long as the window is hidden. A task started from the
**browser** entrance while the desktop window is hidden sets no `activeTask` in
that window at all, so nothing would poll and the window would reopen showing a
project with no runs in it. This promise does not depend on how an occluded
webview schedules timers.

## UI path

Confirmed with the human on 2026-07-31.

- **Close** hides the window; the backend and its tasks are untouched. The app
  stays in the Dock with no window, which is the macOS convention.
- **The Dock icon** reopens and focuses the existing window.
- **A second launch** focuses the existing instance rather than racing it to the
  lock. This is the cheapest place to close the launch race in
  [S28](S28-one-backend-two-entrances.md), so it is a correctness control as much
  as a convenience one.
- **Window-show does exactly three things**: re-verify the backend's instance id
  (S28), refetch the task list, and let [S22](S22-fast-project-open.md)'s
  existing display-snapshot-then-reconcile path handle project state. Not a full
  authoritative replay — focusing a window must never cost an SSH round trip,
  which is the whole point of S22.
- **Quit** is the only control that stops anything, and it is
  [S31](S31-quit-stops-what-it-started.md).

Deliberately not possible: a window close that cancels, pauses, or detaches a
running task; a second launch that produces a second window; and a visible frame
rendered from state gathered before the window was shown.

## Drive

1. Open the desktop application and start an agent task that takes minutes.
2. Close the window while the task is running.
3. Confirm from a browser against the same backend that the task is still going.
4. Reopen from the Dock, then launch the application again from Finder.
5. Let the task finish while the window is closed, then reopen.
6. With the window hidden, start a task from the browser entrance, then reopen.

## Assert

- `closing_the_window_leaves_the_run_active`
- `closing_the_window_stops_no_backend_process`
- `dock_reopen_focuses_the_existing_window`
- `second_launch_focuses_rather_than_starting`
- `showing_the_window_refetches_before_the_first_frame`
- `reopening_shows_current_task_status`
- `a_task_that_finished_while_hidden_is_visible_on_reopen`
- `a_task_started_from_the_browser_is_visible_on_reopen`
- `showing_the_window_costs_no_authoritative_replay`
- `the_browser_and_the_window_agree_throughout`

## Failure means

A human closes a window during a long run and loses the run — the exact failure
the durable-background-work rule exists to prevent, reintroduced by the shell
rather than by the backend. Or the window reopens confidently showing work that
finished twenty minutes ago.
