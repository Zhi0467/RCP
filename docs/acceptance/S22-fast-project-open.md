---
id: S22-fast-project-open
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_project_open_reuses_its_single_materialization
  - tests/test_api.py::test_authoritative_project_get_creates_and_replaces_display_snapshot
  - tests/test_api.py::test_cached_project_survives_restart_without_opening_history
  - tests/test_api.py::test_cached_project_rejects_malformed_mismatched_and_oversize_files
  - tests/test_api.py::test_slow_project_open_does_not_block_concurrent_task_history
  - tests/test_api.py::test_blocking_project_source_read_does_not_stall_health
  - tests/test_api.py::test_project_readiness_does_not_open_or_materialize_project
  - tests/test_api.py::test_catalog_summary_reuses_project_snapshot
  - tests/test_api.py::test_concurrent_project_calls_share_first_open_without_blocking_health
  - tests/test_api.py::test_cached_catalog_open_returns_service_without_building_snapshot
  - tests/test_api.py::test_non_main_project_route_does_not_build_project_snapshot
  - tests/test_transport.py::test_coherent_remote_initialization_reuses_refreshed_snapshot_without_publish
invariants: [7b]
last_passed: 2026-07-30 — 247 backend tests plus a restarted live CoT Steering project: 6.6 ms cached state and 3.5 ms task history while authoritative remote reconciliation took 6.89 s
---

# A known project opens immediately and then reconciles

The append-only patch log remains the source of truth. A previously opened
project also has one rebuildable, durable **display snapshot** in the RCP app
data directory. Clicking that project renders the cached shell and content
immediately while one authoritative refresh runs in the background. The cache
is never read by history, agents, Sync, paper persistence, or any canonical
write path, and mutation controls remain unavailable until current state has
been confirmed.

The authoritative request remains one replay and replaces the display snapshot
when it arrives. Every concurrent request for that unopened project joins the
same initialization instead of starting another refresh. Task history loads
independently, while provider readiness is app-scoped and loads only when a
provider-dependent surface needs it. All blocking project work — remote
refresh, replay, Sync, source reads, paper persistence, and settings writes —
runs off the web event loop so it cannot stall the cache, health, task history,
or other UI requests behind it.

Opening a coherent remote project is a read. It refreshes canonical state once
and does not republish derived files merely because the project was opened;
publishing is reserved for a detected materialization repair or a real write.

## Drive

1. Open a remote fixture once so RCP records an authoritative display snapshot.
2. Return to the project index, restart the server, and click the project while
   timing click-to-project-shell and authoritative reconciliation separately.
3. Hold the authoritative remote refresh open and request task history and the
   cached snapshot concurrently. Also request the graph, Chats, and health while
   two first-open requests arrive together.
4. Change canonical state, reopen the project, and wait for reconciliation.
5. Use Sync, Ask, Refresh, Settings, and Paper before and after reconciliation.

## Assert

- `one_replay_opens_the_project`
- `concurrent_requests_share_one_project_initialization`
- `cached_project_shell_is_visible_within_250ms`
- `cached_snapshot_survives_server_restart`
- `cached_snapshot_is_display_only`
- `mutation_controls_wait_for_authoritative_state`
- `authoritative_snapshot_replaces_cached_content`
- `remote_refresh_does_not_block_the_event_loop`
- `blocking_project_routes_do_not_block_the_event_loop`
- `coherent_remote_open_does_not_republish_derived_files`
- `task_history_does_not_gate_first_render`
- `summary_reuses_the_open_snapshot`
- `paper_and_graph_reuse_the_open_snapshot`
- `provider_readiness_does_not_block_first_view`
- `readiness_populates_agent_controls_on_demand`
- `canonical_change_is_visible_on_the_next_open`
- `loading_state_says_opening_not_materializing`

## Failure means

The user waits on SSH, replay, task history, or provider probes before seeing a
known project; the remote request freezes unrelated UI requests; or cached
display state is mistaken for authority or allowed to drive a write.
