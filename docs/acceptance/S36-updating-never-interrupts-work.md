---
id: S36-updating-never-interrupts-work
status: blocked-external
tier: packaged
driver: desktop
covered_by:
  - web/src-tauri/src/updates.rs
  - web/src-tauri/src/backend.rs
requires: a built application bundle and a published update manifest
invariants: [8, 9]
---

# An update waits for idle, and never interrupts work without being asked

A new version is published to GitHub. Installed applications notice, tell the
human, and update themselves on request.

Applying an update means stopping the backend and relaunching — which is
[S31](S31-quit-stops-what-it-started.md) under another name, and it must inherit
S31's care rather than invent a second, weaker shutdown. The difference is
consent: with Quit the human asked to stop *now*, while with an update they asked
at some earlier point, for something to happen eventually. An update that
replaces the application in the middle of a twenty-minute agent run undoes
everything [S30](S30-desktop-window-is-not-the-app.md) and S31 promise.

So an update is **offered, never applied on its own**, and the offer waits for
idle. When work is in flight the prompt defers and leaves a quiet marker that an
update is ready. Idle is the common case and deferring costs nothing. A human who
chooses to update anyway goes through S31's ownership-aware pause — pause,
replace, relaunch, resume — not a shortcut.

If the application is **reusing** a source-built server rather than owning a
sidecar, updating updates only the application. The exact-version ownership rule
will then correctly refuse to reuse that server until the
checkout catches up. That is right, and it is written here because it is
confusing when discovered and unremarkable when stated.

## UI path

Confirmed with the human on 2026-07-31.

- **A signed update manifest published to GitHub**, verified by the application's
  own update key before anything is replaced. That key is independent of Apple
  code signing.
- **Update is offered, never automatic**, and never silent.
- **The offer respects live work.** With a task running, the prompt defers to a
  quiet "update ready" marker; the app already knows what is in flight from the
  task list [S30](S30-desktop-window-is-not-the-app.md) refetches on show, so
  this needs no new machinery.
- **Choosing to update during live work** runs S31's path: re-derive ownership,
  pause recoverable work, wait past the graceful window, replace, relaunch. The
  resumed work is found on the next launch exactly as after a Quit.
- **Never downgrade**, and never replace on a failed signature or a truncated
  download.

An unsigned, un-notarized bundle that replaced itself meets Gatekeeper on
relaunch, and the quarantine story for a self-replaced app is worse than for one
the human unzipped deliberately — which is how Margin ships today, as a zip plus
`SHA256SUMS`. A working one-click update may be the thing that forces Apple
signing, and that trade is decided when the updater is built, not assumed now.

Deliberately not possible: an automatic update, an update that interrupts live
work without an explicit choice, an update applied through any shutdown path
other than S31's, and an update that stops a backend the application does not own.

## Drive

1. Publish a newer version to the update channel.
2. Open the application with no work running, and take the offered update.
3. Repeat with an agent task running: observe the deferral, then choose to
   update anyway.
4. Relaunch and inspect the paused task.
5. Update an application that is reusing a source-built server, then try to
   reuse that server again.
6. Serve a manifest with a bad signature, and one naming an older version.

## Assert

- `an_update_is_offered_and_never_applied_automatically`
- `a_running_task_defers_the_prompt_to_a_quiet_marker`
- `updating_during_live_work_requires_an_explicit_choice`
- `an_applied_update_uses_the_quit_path_and_pauses_recoverable_work`
- `paused_work_is_resumable_after_the_relaunch`
- `updating_while_reusing_leaves_the_external_backend_untouched`
- `a_bad_signature_replaces_nothing`
- `an_older_version_is_never_installed`
- `the_relaunched_application_starts_without_a_gatekeeper_prompt`

## Failure means

A human loses an agent run to an update they agreed to in the abstract, weeks
earlier. Or the update lands and the application will not reopen, which is the
one failure that cannot be fixed from inside the application.
