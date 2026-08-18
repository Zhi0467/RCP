---
id: S74-boundary-inputs-fail-closed
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_config.py::test_unknown_agent_capability_is_rejected
  - tests/test_history.py::test_malformed_remove_edges_is_rejected_before_history_admission
  - tests/test_storage.py::test_brief_database_write_contention_waits_then_succeeds
  - tests/test_write_scope.py
  - tests/test_launcher.py
  - web/tests/projectSetup.test.mjs
  - browser 2026-08-06
invariants: [1, 4, 8]
reported_by: review, 2026-08-06
last_passed: 2026-08-06 — automated suites plus browser project-setup verification
last_checked: 2026-08-18 — focused write-scope/provider tests and an
  authenticated live Codex probe allowed an admitted repository write and
  denied direct and symlink writes to canonical `.research`; Claude was not authenticated
---

# Uncommon boundary inputs fail closed without damaging the project

RCP does not turn an unfamiliar authority value into broad agent permissions,
let a malformed graph operation poison append-only history, fail ordinary work
on brief application-database contention, or leave project setup pointing at a
repository that no longer exists. A Work-like task also cannot accidentally
write a different project's repositories or canonical RCP state: its provider
launch enforces only the exact task stage and project roots RCP admitted.

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
5. Resolve project A's Work scope while project B is registered under the same
   execution account. Construct fresh and resumed Codex and Claude Work-like
   launches, then attempt an admitted write and an out-of-scope write.

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
- `project_write_scope_is_exact_and_provider_enforced` — Work and orchestrate
  use native unattended exact-root enforcement, never Codex dangerous bypass or
  Claude `bypassPermissions`; cross-project, parent, app-data, and canonical
  `.research` roots are absent and stale/cross-project resume bindings refuse
  before launch.

## Failure means

An unknown value gains authority, an invalid operation enters canonical history,
routine task concurrency fails spuriously, or the setup UI crashes or submits a
canonical repository alias that is no longer present. It also fails if a
Work-like launch relies on prompt wording, silently restores provider bypass, or
can write outside the exact project scope RCP bound to the task.
