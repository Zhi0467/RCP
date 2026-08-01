---
id: S28-one-backend-two-entrances
status: implemented
tier: hermetic
driver: pytest + api
covered_by:
  - tests/test_main.py
  - tests/test_server_runtime.py
  - tests/test_api.py::test_stale_instance_guard_rejects_mutation_before_side_effect
  - web/tests/desktopRuntime.test.mjs
  - web/src-tauri/src/navigation.rs
last_passed: 2026-07-31 — 382 backend tests, 79 web tests, 10 Rust tests,
  packaged reuse on a non-default port, a browser drive against a throwaway project,
  and both bundles opened through macOS LaunchServices into the project index against
  the already-running backend
invariants: [8]
---

# One backend, two entrances

RCP can be entered from the terminal or from a desktop application, and both
reach the same running server. Whichever entrance arrives second finds the
first, identifies it, and uses it. It never starts a second backend, and it
never stops the first one to make room for itself.

The `fcntl` lock on the data directory stays the only thing that confers
ownership. What is new is that the owner also **publishes where it is**: today
`rcp.lock` records a PID and nothing else, so a client that does not already
know the host and port has no way to find the server it is forbidden to
duplicate.

Ownership metadata is written after the lock is acquired, through the atomic
temp-then-replace helper, and is discarded as evidence the moment it disagrees
with a live probe. A metadata file that outlives its process describes a server
that no longer exists; it must never be readable as permission to skip the lock,
and the address it names must never be assumed to still belong to RCP.

**Identity is standing, not a launch formality.** A desktop window that verified
its backend an hour ago may no longer be talking to it: `rcp serve` still
performs a graceful takeover, so the server behind a given address can be
replaced by another one — with a different instance, and possibly a different
data directory and therefore a different set of projects. The window would keep
rendering the old server's state and write to the new one. A window never
presents or mutates state from a server other than the one it identified.

## UI path

Confirmed with the human on 2026-07-31. No new visible UI except the reconnect
state; the rest is a process contract with three surfaces:

- **Ownership metadata** beside the lock in the data directory: schema version,
  instance id, pid, host, port, application version.
- **`/api/health` reports its identity** — instance id and version alongside the
  existing status. A client that reads metadata and then probes the address must
  match the instance id it got back. Without that, "the recorded address answered
  healthily" can mean an unrelated process now holds the port.
- **A serve mode that reuses instead of replacing**, reporting which of three
  things happened: it owns the backend, it is reusing someone else's, or it
  refused ([S29](S29-refuse-instead-of-taking.md)).

Re-verification reuses seams that already exist rather than adding a heartbeat,
which would only run when nobody is looking. The instance id is re-checked on
window show (with the refresh in [S30](S30-desktop-window-is-not-the-app.md)), on
the failure branch of the existing task poll at [App.tsx:296](../../web/src/App.tsx:296),
and on any `ApiError` from a mutation. On mismatch the window stops presenting
state and shows a reconnect surface instead of silently re-rendering.

A backend serving a **different data directory** on the same address is not
reusable. The two would disagree about what the projects are, so it refuses.

Deliberately not possible: reading metadata as ownership, binding a port the
lock did not protect, starting a backend because a probe timed out, and
continuing to render state from an instance that no longer answers.

## Drive

1. Start a server from the terminal on a throwaway data directory. Read the
   published metadata.
2. Launch the second entrance in reuse mode against the same data directory.
3. Kill the first server with `SIGKILL` so its metadata survives it, then launch
   again.
4. Hold the recorded port with an unrelated process, then launch again.
5. With both entrances live against one backend, change canonical state through
   one and read it from the other.
6. With a desktop window attached and idle, replace its backend from a terminal.
   Focus the window, then attempt a mutation.

## Assert

- `lock_owner_publishes_its_address`
- `metadata_is_written_after_the_lock_and_atomically`
- `second_entrance_reuses_the_owner`
- `reuse_matches_the_instance_id_reported_by_health`
- `no_second_backend_is_started`
- `stale_metadata_never_confers_ownership`
- `a_stranger_on_the_recorded_port_is_not_mistaken_for_rcp`
- `a_backend_serving_another_data_directory_is_refused`
- `both_entrances_show_the_same_canonical_state`
- `identity_is_re_verified_on_show_on_poll_failure_and_on_mutation`
- `a_replaced_backend_is_noticed_before_it_is_used`
- `a_mismatched_instance_stops_the_window_presenting_state`

## Failure means

Two backends run against one data directory, or one entrance loads a stranger's
HTTP server and calls it RCP, or a window shows one server's projects while
writing to another's. The first corrupts the append-only log's single writer;
the others are a correctness question that looks like a display bug.
