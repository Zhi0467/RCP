---
id: S31-quit-stops-what-it-started
status: pending
tier: hermetic
driver: desktop
covered_by:
  - web/src-tauri/src/backend.rs
  - web/src-tauri/src/lib.rs
  - desktop 2026-08-08 — reused-backend Quit path
invariants: [8, 9]
last_checked: 2026-08-08 — quitting the desktop shell removed its sole process
  while the reused CLI backend stayed healthy at the same PID; owned-sidecar,
  takeover, and forced-timeout paths remain outstanding
---

# Quit stops what it started, and nothing else

Quitting RCP is the one action that ends the backend — but only when the desktop
application is the backend's owner. If it reused a server the human started from
the terminal, quitting closes the window and leaves that server running.

When it does own the backend, Quit must produce the same graceful stop the CLI's
takeover already produces. Recoverable work is paused before the process exits,
because the pause is what makes it resumable on the next launch. That pause runs
inside the FastAPI lifespan's teardown and takes real time — up to the worker
join window in `BackgroundAgentTasks.shutdown`. A shell that ends its child by
forced termination skips the teardown entirely and turns every live run into an
interrupted one. So Quit signals and waits. Forced termination exists only as the
observable last resort after the wait expires, and the human is told it happened.

**Ownership is re-derived at quit time, never remembered from launch.** It can
change in both directions while the app is running. A `rcp serve --force` can
replace the app's own sidecar, after which the app reconnects to a backend it
does not own — and a cached "I own it" would make Quit kill someone else's
server, which is the exact failure this scenario exists to prevent, produced by
its own bookkeeping. The reverse is just as reachable: an app that reused a
terminal server, then started its own after that server went away, would leave an
orphan sidecar holding the lock with no window attached to it.

## UI path

Confirmed with the human on 2026-07-31.

- **Ownership is one comparison.** When the app starts a sidecar it records that
  instance's id. At Quit it asks `/api/health` for the current `instance_id` and
  stops the backend only if they match — reusing the field
  [S28](S28-one-backend-two-entrances.md) already added. Match means it is still
  my child; mismatch means the thing I started is gone and something else is
  answering. This is automatically correct across reattachment paths, including
  ones nobody anticipated.
- **Quit while owning the backend**: signal, wait past the graceful-shutdown
  window, exit. The window may close immediately; the process does not. No
  confirmation dialog — the work is recoverable either way, and a modal in front
  of a Quit is a poor place to decide that.
- **Quit while reusing an external backend**: close the application only.
- **A wait that expires** reports that work may not have been paused. It does not
  report a clean exit.
- **The next launch** finds the paused work and offers resume exactly as it does
  after a terminal `Ctrl-C` — this is not a second recovery path.

Stopping an owned backend also ends any browser tab pointed at it. That is
correct — the app started that server — and it is written down here so it reads
as intended behavior rather than as something discovered later.

Deliberately not possible: forced termination as the normal path, a Quit that
stops a backend the app did not start, a Quit decision made from launch-time
state, and a "quit anyway" that hides whether work was paused.

## Drive

1. Launch the desktop application so it owns the backend, start a long agent
   task, and Quit.
2. Relaunch and inspect the task.
3. Start a server from the terminal, launch the desktop application so it reuses
   that server, and Quit.
4. With the app owning its sidecar, run `rcp serve --force` from a terminal, let
   the app reconnect to the new server, then Quit.
5. Force the wait to expire with a backend that will not stop.
6. After each Quit, run `uv run rcp open`.

## Assert

- `quit_when_owned_signals_and_waits_past_the_shutdown_window`
- `recoverable_work_is_paused_before_the_process_exits`
- `paused_work_is_resumable_on_the_next_launch`
- `quit_when_reused_leaves_the_external_backend_running`
- `ownership_is_re_derived_not_remembered`
- `quit_after_a_takeover_leaves_the_replacement_running`
- `forced_termination_is_a_reported_last_resort`
- `an_expired_wait_is_never_reported_as_a_clean_exit`
- `rcp_open_works_unchanged_after_a_quit`

## Failure means

An agent run that could have been resumed is killed instead, and the human finds
out on the next launch. Or quitting the desktop app stops a server it did not
start, which looks from the terminal like the server crashed.
