---
id: S47-agent-usage-ledger
status: implemented
tier: hermetic
driver: pytest + browser
covered_by: tests/test_launcher.py::test_provider_usage_is_normalized_at_provider_boundaries; tests/test_storage.py::test_agent_usage_is_counted_once_and_snapshot_uses_weighted_cache_share; tests/test_storage.py::test_agent_usage_snapshot_counts_latest_input_context_once_per_native_session; tests/test_api.py::test_project_usage_endpoint_returns_counted_and_excluded_records
invariants: []
last_passed: 2026-08-04 — pytest and browser-driven against the local CRLP project
---

# See counted provider usage in Settings

## UI path (confirmed 2026-08-02)

Open a project and enter **Settings**. The first section, above **Project
boundary**, is simply two read-only usage widgets side by side: **Input
context** and **Generated**. There is no enclosing Agent usage heading,
ledger count, tracking comment, or explanatory strip. The widgets use different
semantic colors. Each widget
shows the project total and a task-by-provider mosaic for Seed, Refresh, Node
Chat, Project Chat, and Paper Coach. A full square represents five percent of
that widget's total; a nonzero remainder is shown as a partially filled square,
so small usage is never silently rounded away. Exact totals remain visible.

For nonzero usage, S48 adds one separate screen-story comparison strip directly
beneath the widget pair. It is the comparison itself, not a heading or an
explanation of these widgets. Zero usage still renders only the two widgets.

Only usage records marked `counted` contribute to the totals. Selecting a
task/provider cell exposes its counted and excluded usage records, operation
IDs, provider profile, token fields, and any duplicate or invalid reason. The
screen does not offer a manual recount control.

Usage is fetched when the project opens, whenever Settings opens, and when an
agent task reaches a terminal state. The ledger is forward-only: historical
invocations that completed before RCP began recording provider usage are not
invented or reconstructed from incomplete transcripts. A project with no
recorded rows shows the two zero-total widgets without additional commentary.

## Drive

1. Open a project with historical tasks but no usage rows and confirm the usage
   section is first and both widgets render an empty zero-total state without a
   request error or any surrounding heading/commentary.
2. Seed the test ledger with counted and excluded records across both providers
   and multiple task kinds, including a cell below one five-percent square.
3. Open **Settings** and confirm the two widgets are side by side, have distinct
   colors, show the correct counted totals, and retain the small cell as a
   partial square.
4. Select a task/provider cell and confirm the detail view identifies the
   operation ID, provider profile, counted status, and exclusion reason.
5. Confirm excluded duplicate records do not affect either widget total.
6. Complete an agent task while the project is open, then leave and re-enter
   Settings; both paths expose the newly recorded usage without a manual refresh
   or recount action.

## Assert

- `usage_summary_is_derived_from_counted_records_only`
- `usage_records_are_idempotent_at_the_provider_boundary`
- `latest_input_context_is_counted_once_per_native_session`
- `input_and_generated_totals_are_grouped_by_task_and_provider`
- `cache_share_is_weighted_over_latest_counted_input_context`
- `small_nonzero_cells_render_as_partial_usage`
- `settings_shows_side_by_side_distinct_usage_widgets`
- `settings_places_usage_before_project_configuration`
- `settings_open_and_task_completion_refresh_usage`
- `empty_usage_is_two_zero_widgets_without_commentary`
- `usage_details_expose_counted_and_excluded_records`
- `no_console_or_application_request_errors`

## Failure means

An invocation is counted twice, a resumed native session contributes every
turn's repeated input context to the input total, excluded usage changes a
total, a provider-specific field is discarded, a small nonzero cell disappears,
or Settings cannot show the two usage widgets side by side.
