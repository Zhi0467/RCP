---
id: S22-fast-project-open
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_api.py::test_project_open_reuses_its_single_materialization
  - tests/test_api.py::test_project_get_creates_then_reuses_display_snapshot_without_reopening
  - tests/test_api.py::test_moved_head_refreshes_in_background_singleflight
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
last_passed: 2026-08-11 — 223 API/transport tests plus an isolated served
  two-project drive; a retained tab rendered with no loading, spinner, or inert
  frame while background reconciliation advanced it independently
---

# A known project opens immediately and then reconciles

The append-only patch log remains the source of truth. A previously opened
project also has one rebuildable, durable **display snapshot** in the RCP app
data directory, and each live frontend keeps a bounded per-project render cache.
Activating a known tab restores the frontend state before paint and reads only
the backend display snapshot; it never waits for a remote request. The cache is
never read by history, agents, Sync, paper persistence, or any canonical graph
write path. Canonical writes still validate against live state, so cached
display freshness is not itself a reason to freeze the view or disable Sync.

A cache-only heartbeat schedules a throttled patch-head probe. Only a changed
head starts one replay and replaces the display snapshot; unchanged state starts
no refresh, and transient probe failure leaves the cached project usable. Every
concurrent request for a first-ever uncached project still joins the same
initialization. Task history loads independently, while provider readiness is
app-scoped and loads only when a provider-dependent surface needs it. All
blocking project work runs off the web event loop so it cannot stall the cache,
health, task history, or other UI requests behind it.

Opening a coherent remote project is a read. It refreshes canonical state once
and does not republish derived files merely because the project was opened;
publishing is reserved for a detected materialization repair or a real write.

## Drive

1. Open a remote fixture once so RCP records an authoritative display snapshot.
2. Switch between two retained project tabs, then restart the server and click
   the project while timing frontend-cache and backend-snapshot renders.
3. Hold a changed-head reconciliation open and request task history and the
   cached snapshot concurrently. Also request the graph, Chats, and health while
   two first-open requests arrive together.
4. Change canonical state while the project stays visible and wait for the
   heartbeat-triggered reconciliation.
5. Use Sync, Ask, Refresh, Settings, and Paper before and after reconciliation.

## Assert

- `one_replay_opens_the_project`
- `concurrent_requests_share_one_project_initialization`
- `cached_project_shell_is_visible_within_250ms`
- `cached_snapshot_survives_server_restart`
- `cached_snapshot_is_display_only`
- `cached_render_does_not_wait_for_probe_or_reconciliation`
- `authoritative_snapshot_replaces_cached_content`
- `remote_refresh_does_not_block_the_event_loop`
- `blocking_project_routes_do_not_block_the_event_loop`
- `coherent_remote_open_does_not_republish_derived_files`
- `task_history_does_not_gate_first_render`
- `summary_reuses_the_open_snapshot`
- `paper_and_graph_reuse_the_open_snapshot`
- `provider_readiness_does_not_block_first_view`
- `readiness_populates_agent_controls_on_demand`
- `canonical_change_is_visible_without_reopening`
- `loading_state_says_opening_not_materializing`

## Failure means

The user waits on SSH, replay, task history, or provider probes before seeing a
known project; the remote request freezes unrelated UI requests; or cached
display state is mistaken for authority or allowed to drive a write.
