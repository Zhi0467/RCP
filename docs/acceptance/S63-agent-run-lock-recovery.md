---
id: S63-agent-run-lock-recovery
status: implemented
driver: pytest + api
tier: hermetic
covered_by:
  - tests/test_transport.py
  - tests/test_transport.py::test_process_advisory_lock_acquires_when_contention_resolves_within_one_read
  - tests/test_transport.py::test_process_advisory_lock_reclaims_an_empty_legacy_directory
  - tests/test_api.py::test_seed_waits_for_live_canonical_owner_without_failing
  - tests/test_api.py::test_seed_can_pause_while_waiting_for_canonical_owner
  - tests/test_api.py::test_seed_pauses_and_retains_its_patch_when_run_lock_ownership_is_lost
  - tests/test_api.py::test_work_lock_ownership_loss_preserves_the_answer_and_skips_graph_apply
  - tests/test_api.py::test_background_work_can_pause_while_waiting_for_canonical_state
invariants: [8, 9]
---

# RCP recovers agent-run ownership; the human never removes a lock

## Promise

A Seed, Refresh, or graph-writing conversation may leave its canonical-state
lock behind only if ownership is no longer live. That stale artifact is RCP's
problem, not the human's.

When a new graph-writing run reaches canonical state:

- if no process still owns the lock, RCP reclaims it and starts the run;
- if a live process owns it, the new work waits without launching a duplicate
  provider and without becoming a failed task;
- an empty lock directory left by a crashed mkdir-era run is reclaimed
  automatically, without a diagnostic and without a human step;
- if RCP still cannot establish ownership safely, it does not remove the lock.
  The task surface explains the exact location, the ownership evidence RCP could
  not verify, why replacement would be risky, and the safe next action;
- no error tells the human to find or remove `.research/.agent-run.lock`;
- navigating away from the project has no effect on ownership or recovery.

Lock-file existence alone is never treated as ownership. A crashed RCP process
or severed lock-holder connection releases ownership without a cleanup command,
so a later run does not inherit a dead directory as permanent project state.
Canonical publication is performed by the same process that owns the short
refresh lock; a pre-write ownership check is not treated as a substitute for a
fenced commit point.

## Drive

Use a temporary remote-state double whose lock holder is a separate process.
Acquire the project lock, contend from a second workspace, then exercise three
cases: the holder remains live, the holder exits normally, and the holder is
terminated without running application cleanup. Also place a bare mkdir-era lock
directory, and one holding an owner artifact whose provenance cannot be
verified.

Through the API, launch a Seed against each state and inspect its task lineage
and diagnostic receipts.

## Assert

1. Live contention leaves one provider launch and a non-terminal waiting task.
2. Normal release and holder death both let the waiting run acquire ownership
   automatically.
3. An empty legacy lock directory is reclaimed and the run proceeds silently.
4. A legacy directory holding an unverifiable owner artifact is preserved, keeps
   that artifact intact, and produces a readable, bounded diagnostic instead of
   destructive recovery.
5. None of the task errors, events, or contracts contains a manual lock-removal
   instruction.
6. Local canonical state retains its process-scoped `flock` behavior.
7. Killing the refresh-lock holder after staging cannot run an unfenced commit;
   a commit-channel loss is reconciled as present, absent, or unknown.

## UI path (confirmed)

Confirmed by the human on 2026-08-04: no new lock-management control is added.
Runs remains the recovery surface: a waiting task names the canonical-state
location it is waiting on; an ownership state that cannot be proved safe
preserves the entry and explains what older deployment must be stopped before
Retry can safely proceed.
There is deliberately no file path presented as an instruction to edit project
state by hand.
