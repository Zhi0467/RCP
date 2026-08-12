# Dispatch — quit stops what it started

**Date:** 2026-08-12
**Scenario:** [S31](../acceptance/S31-quit-stops-what-it-started.md) — confirmed
by the human 2026-07-31. The UI path in it is settled; nothing below needs a new
design decision.

Read [`AGENTS.md`](../../AGENTS.md), then the scenario, then
[`backend.rs`](../../web/src-tauri/src/backend.rs).

## Why now

The graph-condition wake route is landing in parallel. It makes conversations
that sleep for hours the ordinary case rather than the exception. Today, quitting
the desktop app when it owns the backend skips the FastAPI lifespan teardown, so
every one of those sleeping runs becomes `interrupted` instead of `paused` — and
pausing is what makes them resumable. This route is what lets the other route's
waiting survive a quit.

## What is already true

The reused-backend path passed on 2026-08-08: quitting the desktop shell removed
its own process while a CLI backend at the same PID stayed healthy. Outstanding:
the owned-sidecar path, the takeover path, and the forced-timeout path.

## What you are building

- **Ownership is one comparison, re-derived at quit time.** When the app starts a
  sidecar it records that instance's id; at Quit it asks `/api/health` for the
  current `instance_id` and stops the backend only on a match. Reuse the field
  [S28](../acceptance/S28-one-backend-two-entrances.md) already added.
- **Owned Quit signals and waits** past the graceful-shutdown window — up to the
  worker join window in `BackgroundAgentTasks.shutdown`. The window may close
  immediately; the process may not. No confirmation dialog.
- **Reused Quit closes the application only.**
- **An expired wait reports that work may not have been paused.** It never
  reports a clean exit.
- **Forced termination is the observable last resort**, and the human is told it
  happened.

## Invariants you must not break

- **Never cache ownership from launch.** It changes in both directions while the
  app runs: `rcp serve --force` can replace the app's own sidecar, and a cached
  "I own it" would then make Quit kill someone else's server — the exact failure
  this scenario exists to prevent, produced by its own bookkeeping.
- **Invariant 8 stands.** One RCP process per data directory, `fcntl`-held. Quit
  must not leave an orphan sidecar holding the lock with no window attached.
- **Invariant 9 stands.** A paused run is resumable; the next launch offers
  resume exactly as it does after a terminal `Ctrl-C`. This is not a second
  recovery path.

## Verify it cold

**A warm backend on 8421 hides every startup and shutdown ordering bug.** This
repo has already shipped a desktop build that looked perfect on every warm
relaunch and stayed blank forever on a cold start. Kill nothing of the human's —
probe `http://127.0.0.1:8421/api/health` first — but do your verification runs
against a data directory nobody else owns.

"The bundle built and the process exited" is not "the work was paused." Read the
startup and shutdown milestones on stderr, and inspect the task records after
relaunch.

Desktop builds:

```bash
sh web/src-tauri/scripts/build-dev.sh
```

`tauri build --debug` is not a diagnostics flag — `debug_assertions` selects the
checkout backend and dev navigation policy, so `RCP Dev.app` cannot be built
without it.

When the desktop work is done and no more Tauri work is planned:

```bash
cargo clean --manifest-path web/src-tauri/Cargo.toml
```

## Out of scope

- [S32](../acceptance/S32-artifacts-in-the-desktop-window.md) (native preview and
  download isolation) and
  [S90](../acceptance/S90-desktop-chat-dictation.md) (dictation). Both are
  confirmed and both live in the same directory, but each is its own scenario and
  its own drive.

## Done means

S31 passes — all nine assertions, including the takeover and expired-wait paths,
not only the two that already work. `driver: desktop`, so there is no pytest
substitute for it.

Then `git add -A` and `uv run pre-commit run --all-files`, and stamp the
scenario.
