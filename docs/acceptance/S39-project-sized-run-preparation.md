---
id: S39-project-sized-run-preparation
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_sources.py::test_matching_local_histories_are_fully_normalized
  - tests/test_sources.py::test_second_index_build_reuses_unchanged_matching_and_unmatched_metadata
  - tests/test_sources.py::test_changed_source_reparses_only_that_file
  - tests/test_sources.py::test_deleted_source_is_evicted_from_metadata_cache
  - tests/test_sources.py::test_unchanged_terminal_cursor_does_not_reopen_local_source
  - tests/test_sources.py::test_changed_local_source_identity_disables_terminal_cursor_shortcut
  - tests/test_api.py::test_graph_stream_reuses_revision_from_assembled_context
  - tests/test_sync.py::test_graph_sync_builds_from_the_single_in_lock_current_replay
  - tests/test_sync.py::test_batch_reuses_pending_replay_for_committed_outputs
invariants: [5, 10d]
last_passed: 2026-08-01 — 455 backend tests passed. Against 1,415 actual Claude
  and Codex source files, the exact first build took 6.6626 s and the identical
  second build took 0.0175 s (381.1x faster) with identical output.
---

# Repeated run preparation reuses unchanged source metadata

The first source index remains deliberately exact: RCP reads and normalizes each
candidate far enough to classify it, and fully validates every matching
conversation before it can enter agent context. The long-lived project service
then retains only that parsed metadata in memory, keyed by provider, path,
device, inode, size, and nanosecond modification time. A later Seed or Refresh
stats the candidates, reuses metadata for exact identity matches — including
unmatched files — and reparses only new or changed sources. Deleted sources are
evicted. Nothing persists across an app restart, and malformed or uncertain
sources are never cached as valid.

An unchanged conversation whose cursor already names the indexed terminal record
produces an empty delta without reading the file from byte zero a second time.
That shortcut is allowed only while the source identity — device, inode, size,
and nanosecond modification time — still matches what was indexed. If identity
changes, RCP takes the validating path and retains its existing refusal or cursor
repair behavior; performance never weakens source-integrity checks.

The graph materialization and revision assembled for a run travel with that run
context. Preparing the launch does not replay canonical history again merely to
recover a revision integer. Sync retains its required freshness replay under the
append lock, but does not perform redundant pre-lock or post-rename replays.

## UI path (confirmed 2026-08-01)

There is no new control. The human presses Seed or Refresh in the existing
surface. Preparation remains visible through the existing task lifecycle, but a
large unrelated global provider history no longer leaves the task waiting before
its provider starts.

## Drive

1. Point the source roots at a fixture containing many conversations from
   unrelated repositories and a small matching project history.
2. Build the source index twice in the same project service, with the matching
   history already at its terminal cursor.
3. Change one source, delete another, and build again.
4. Mutate the terminal-cursor source while preserving its terminal cursor text.
5. Start Refresh, then Sync against a multi-patch history.

## Assert

- `first_build_keeps_full_matching_validation`
- `unchanged_matching_and_unmatched_sources_are_not_reopened`
- `only_changed_sources_are_reparsed`
- `deleted_sources_leave_the_cache`
- `unchanged_terminal_cursor_does_not_rescan_the_source`
- `changed_source_identity_disables_the_empty_delta_shortcut`
- `source_rewrite_detection_and_cursor_repair_are_unchanged`
- `assembled_run_carries_its_graph_revision`
- `launch_does_not_replay_to_read_the_revision`
- `sync_keeps_the_in_lock_freshness_replay`
- `sync_reuses_the_validated_pending_result_after_commit`

## Failure means

Every run reparses gigabytes of unchanged provider logs; an optimization admits
a changed source without validation; or a run replays canonical state only to
rediscover information its context already held.
