---
id: S62-direct-provider-log-ingestion
status: implemented
tier: remote
driver: pytest + browser
covered_by:
  - tests/test_direct_ingestion_context.py
  - tests/test_direct_ingestion_contract.py
  - tests/test_direct_ingestion_run.py
  - tests/test_source_preflight.py
last_checked: 2026-08-04 — hermetic path passes; live remote Seeds for Edit Agent
  and HyperTree read direct provider roots, survived project navigation and a
  development reload through Resume, applied revision 1, and advanced each
  project watermark. The live Refresh half was not driven.
invariants: [4b, 5, 8, 10f]
---

# Seed and Refresh point agents at provider logs instead of moving them

## User promise

RCP is the control plane for source ingestion, not a conversation-processing
pipeline. Each project already declares its Claude and Codex log roots. Seed and
Refresh give the agent those paths on the execution machine, the last successful
Seed/Refresh time, and the current optional human request. The agent reads the
provider logs in place and may use provider-owned, read-only subagents when the
corpus is too large for one context.

RCP never reads conversation records to prepare a run, normalizes them, creates
per-session slices, transfers them, copies native provider transcripts into a
task stage, or validates claims against per-log record cursors. The only source
preflight is a bounded existence/readability probe of the configured provider
roots. An unavailable root becomes an exact warning in the task contract and
does not prevent the agent from attempting the remaining sources.

The ingestion boundary is project-global: the canonical graph's
`last_refresh_at`, set by RCP only when a Seed/Refresh patch applies. A failed,
paused, interrupted, or graph-rejected attempt never advances it. A fresh Seed
has no boundary; the optional human request may narrow the historical period the
agent should inspect. This is a run watermark, not an exactly-once per-record
claim: the agent tolerates overlap and deduplicates provider records when needed.

## UI path

1. Create a project whose state, repository, provider logs, and Seed execution
   all live on one remote machine.
2. Start Seed with an optional historical cutoff in the human request, then
   leave the project and open another one.
3. Inspect the running task contract. It names the configured provider roots,
   says that no prior successful ingestion boundary exists, contains a pointer
   to the human request, and tells the parent agent how to delegate bounded
   read-only log inspection.
4. Let Seed finish, return to the project, and note its successful time.
5. Add provider activity, start Refresh, and inspect its contract. The boundary
   is exactly the prior successful Seed time.
6. Force one Refresh failure and Retry it. Confirm the failed attempt did not
   move the boundary. Let a later attempt apply and confirm only that success
   advances it.

## Assert

- `graph_run_never_builds_a_conversation_index`
- `graph_run_never_materializes_or_transfers_conversation_files`
- `graph_retry_never_projects_a_native_provider_transcript`
- `contract_names_direct_provider_roots_and_project_watermark`
- `contract_points_to_the_optional_human_request`
- `contract_allows_bounded_read_only_provider_subagents`
- `missing_source_root_warns_without_blocking_launch`
- `agent_patch_schema_has_no_coverage_or_cursor_operation`
- `failed_run_does_not_advance_ingestion_watermark`
- `applied_seed_or_refresh_advances_ingestion_watermark`
- `run_survives_project_navigation`
- `no_console_or_application_request_errors`

## Failure means

Any provider conversation byte appears in an RCP cache, scratch input bundle,
or transfer solely to prepare Seed/Refresh; a launch depends on per-session
record metadata; or RCP advances the project watermark before a graph patch
actually applies.
