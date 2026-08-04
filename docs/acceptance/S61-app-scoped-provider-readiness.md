---
id: S61-app-scoped-provider-readiness
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_provider_warmup_starts_after_health_is_available
invariants: [4, 8]
last_passed: 2026-08-04 — browser opened a project, switched to Chats, and enabled
  the composer while provider readiness was app-managed in the background
---

# Provider readiness warms once without freezing the app

Provider availability is an app-level capability. The app process warms every
known provider/host/executable target once in the background after health is
available. Opening a project and changing views only reads the process cache;
it never decides whether probing starts.

## Drive

1. Start RCP with at least one registered project and let the app become
   healthy.
2. While provider checks are still running, open a project, switch to Chats,
   type into the composer, and move between views. The shell remains usable;
   only the provider status shows a spinner.
3. Once the background checks finish, project surfaces show the cached result
   without starting another probe.
4. Switch to another project that uses the same target. Its readiness reuses
   the same process-scoped result.
5. Use the explicit provider refresh control. It bypasses the cache and
   updates the displayed readiness.

## Assert

- `project_open_does_not_request_provider_readiness`
- `project_open_does_not_probe_provider_targets`
- `provider_warmup_is_async_and_process_scoped`
- `shared_targets_reuse_one_process_capability`
- `explicit_refresh_reprobes_the_target`
- `project_shell_and_graph_have_no_console_or_request_errors`

## Boundary

All known targets may warm in the background after app health is available.
Failures are isolated per target and remain visible as readiness state; they do
not prevent the app from becoming interactive.
