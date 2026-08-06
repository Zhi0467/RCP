---
id: S74-boundary-inputs-fail-closed
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_config.py::test_unknown_agent_capability_is_rejected
  - tests/test_history.py::test_malformed_remove_edges_is_rejected_before_history_admission
  - tests/test_storage.py::test_brief_database_write_contention_waits_then_succeeds
  - web/tests/projectSetup.test.mjs
  - browser 2026-08-06
invariants: [1, 4, 8]
reported_by: review, 2026-08-06
last_passed: 2026-08-06 — automated suites plus browser project-setup verification
---

# Uncommon boundary inputs fail closed without damaging the project

RCP does not turn an unfamiliar authority value into broad agent permissions,
let a malformed graph operation poison append-only history, fail ordinary work
on brief application-database contention, or leave project setup pointing at a
repository that no longer exists.

These are boundary conditions, not alternate workflows. Their safe behavior is
explicit and testable without adding fallback authority or silently repairing
canonical state.

## UI path (confirmed)

Confirmed by the human on 2026-08-06: there is no new screen, control, warning,
or configuration. In project setup, at least one repository remains visible and
the canonical-state selection always names a visible repository. The other
boundaries are backend contracts and have no browser path.

## Drive

1. Ask the permission contract for a value outside its declared surfaces and
   capabilities.
2. Validate a raw `remove_edges` operation with extra keys, a non-list
   `edge_ids`, and non-string edge ids.
3. Hold a short SQLite write transaction while another application-store writer
   attempts to begin.
4. In project setup, remove the canonical-state repository and exercise the
   state update with an unexpectedly empty repository list.

## Assert

- `unknown_agent_capability_is_rejected` — an unfamiliar value raises instead
  of receiving the widest permission envelope.
- `malformed_remove_edges_is_rejected_before_history_admission` — bad structure
  becomes an ordinary validation rejection and never degrades replay.
- `brief_database_write_contention_waits_then_succeeds` — the store has an
  explicit 30-second busy timeout, and a short competing transaction does not
  fail with `database is locked`.
- `project_setup_never_points_at_a_missing_repository` — removing the selected
  repository selects a remaining repository; an empty list clears the selection
  safely rather than indexing past the list.

## Failure means

An unknown value gains authority, an invalid operation enters canonical history,
routine task concurrency fails spuriously, or the setup UI crashes or submits a
canonical repository alias that is no longer present.
