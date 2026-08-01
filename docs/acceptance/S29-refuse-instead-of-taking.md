---
id: S29-refuse-instead-of-taking
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_main.py
  - tests/test_server_runtime.py
  - web/src-tauri/src/backend.rs
  - web/src-tauri/src/lifecycle.rs
last_passed: 2026-07-31 — full backend and Rust suites plus packaged exact-instance reuse
invariants: [8]
---

# Nothing takes a backend that is doing work without saying what it interrupts

`rcp serve` replaces the current owner on purpose: it sends `SIGTERM`, waits for
recoverable work to pause, and takes the lock. That is right for a human who
typed the command, and it is defended by
`tests/test_main.py::test_serve_replaces_the_server_that_holds_the_instance_lock`.

It is wrong for anything launched automatically. A desktop application that
starts its backend the ordinary way would stop the server the human is already
using, in the middle of an agent run, without anyone asking for it. So automatic
launch uses a mode that never terminates another process. When it cannot own the
backend and cannot safely reuse one, it **refuses with a reason** and leaves
everything running.

The protection runs in both directions, because the desktop makes the reverse
case worse rather than better. Once a backend can be owned by an application, the
owner becomes invisible — behind a window that may be hidden in the Dock, running
a task nobody can see — and it becomes a child process, so a terminal `SIGTERM`
kills the app's own sidecar out from under it. Someone typing `rcp serve` in a
fresh shell has no way to know any of that.

So the rule is not "the CLI may, the app may not." It is that **nothing takes a
backend that is doing work without telling someone what it is about to
interrupt.** Where the two entrances still differ is in the remedy: the CLI stays
blunt when bluntness is free, and asks only when what it would interrupt is
invisible to the person typing.

Each refusal is distinguishable, because the remedies differ: an incompatible
version needs an upgrade or a deliberate replace; an occupied port needs a
different port; an owner that stopped answering needs a decision about a server
that may still be doing work.

## UI path

Confirmed with the human on 2026-07-31.

- **Stable exit codes**, one per refusal reason, so the caller does not parse
  prose to learn what happened.
- **Version compatibility is exact match** on the single application version,
  because a mismatched backend also serves a mismatched frontend bundle, and the
  desktop window would silently render the old UI. This holds in both
  directions — an older app refuses a newer backend and vice versa.
- **A human-authorized replace.** On refusal the desktop offers to replace the
  running server, and choosing it performs the same graceful takeover the CLI
  does. The alternative — telling the human to find and kill a PID — contradicts
  a preference this repo already records.
- **`serve` reads before it signals.** It fetches the owner's metadata and
  health, and when the owner reports live recoverable work it names what it is —
  *"RCP.app is running 1 agent task"* — and asks. When the owner is idle it
  replaces silently exactly as it does today.
- **`--force` skips the question**, for scripts and unattended use.
- **An app that loses its own sidecar** routes into [S28](S28-one-backend-two-entrances.md)'s
  reconnect surface and offers to start a new backend, rather than leaving a
  dead window.
- **One owner for the boot timeout.** How long to wait for readiness is decided
  on one side and read by the other, never invented independently in two places.

Deliberately not possible: automatic replacement, replacement on a probe
timeout, silently loading a backend whose version does not match, and an
interactive prompt in the path a script takes.

## Drive

1. Launch in automatic mode while a healthy compatible server holds the lock.
2. Repeat with the running server reporting a different application version, in
   each direction.
3. Repeat with the lock free but the port held by an unrelated process.
4. Repeat with metadata naming an address that nothing answers.
5. Take the offered replace action, and separately run `rcp serve` by hand.
6. Run `rcp serve` while a desktop-owned backend is idle, then while it is
   running an agent task, then again with `--force`.

## Assert

- `automatic_launch_never_sends_a_signal_to_another_process`
- `compatible_owner_is_reused_and_reported_as_reused`
- `incompatible_version_refuses_in_both_directions`
- `port_held_under_a_free_lock_refuses_distinctly`
- `unanswered_recorded_address_refuses_distinctly`
- `each_refusal_has_a_stable_exit_code`
- `human_authorized_replace_performs_the_graceful_takeover`
- `serve_replaces_an_idle_owner_without_asking`
- `serve_names_live_work_before_interrupting_it`
- `force_skips_the_question_and_never_prompts`
- `an_app_that_loses_its_sidecar_offers_to_start_one`
- `boot_timeout_has_one_owner`

## Failure means

A launch stops a server someone was using and interrupts an agent task, or a
race between two launches produces two owners. Either way the failure lands on
work someone was in the middle of, not on the launch that caused it.
