---
id: S61-app-scoped-provider-readiness
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: none
invariants: [4, 8]
last_passed: 2026-08-04 — backend and web checks plus browser project-switch drive
---

# Opening a project does not recheck providers

Provider availability is an app-level capability. Opening a project renders
its cached project state without probing every provider on every configured
machine. A provider is checked when the app warms a target, when a
provider-dependent surface first needs it, or when the human explicitly
refreshes it.

## Drive

1. Start RCP with at least one registered project and let the app become
   healthy.
2. Open the project index and open a project. The project shell and graph
   render without a provider readiness request or provider subprocess blocking
   project opening.
3. Open a provider-dependent surface such as the Seed/Refresh dialog or
   Project Settings. Readiness loads there if the app-level cache does not
   already contain the exact provider, host, and executable target.
4. Switch to another project that uses the same target. Its readiness uses the
   app-scoped result rather than probing the target again.
5. Use the explicit provider refresh control. It bypasses the cache and
   updates the displayed readiness.

## Assert

- `project_open_does_not_request_provider_readiness`
- `project_open_does_not_probe_provider_targets`
- `provider_dependent_surfaces_load_readiness_lazily`
- `shared_targets_reuse_one_process_capability`
- `explicit_refresh_reprobes_the_target`
- `project_shell_and_graph_have_no_console_or_request_errors`

## Boundary

Local targets may warm in the background after app health is available.
Remote targets remain lazy because each host and exact executable is a
separate capability; opening unrelated projects must not contact every remote
machine.
