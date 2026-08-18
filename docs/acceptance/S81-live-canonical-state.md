---
id: S81-live-canonical-state
status: implemented
tier: hermetic
driver: api + browser
covered_by:
  - tests/test_api.py::test_project_revision_probe_is_small_and_does_not_replay_history
  - tests/test_api.py::test_project_revision_probe_returns_normal_project_not_found
  - tests/test_api.py::test_cached_revision_heartbeat_is_cache_only_and_unchanged_head_starts_no_refresh
  - tests/test_api.py::test_moved_head_refreshes_in_background_singleflight
  - tests/test_api.py::test_work_proposal_is_applied_as_a_proposal_not_a_universal_gate
  - tests/test_transition_api.py
  - tests/test_transition_control_projection.py
  - web/tests/canonicalRevisionRefresh.test.mjs
  - web/tests/humanDraft.test.mjs
  - web/tests/projectTransition.test.mjs
  - web/tests/transitionAppIntegration.test.mjs
last_passed: 2026-08-18 — served browser drive previewed and synced a Blocker
  resolution, replaced graph/control/guidance at one exact head, retained the
  reversible draft reset, and kept browser console and server log clean
invariants: [1, 2, 3, 6, 10b]
---

# Canonical graph changes appear without reloading the UI

An open project stays aligned with canonical graph state. When any RCP surface
applies a graph revision, every visible client for that project notices the new
revision and reconciles one coherent project snapshot. Graph, Experiment
control, guidance validity, and transition head therefore change atomically;
new content and Proposals appear without a browser reload, desktop restart, view
change, or agent Refresh run.

Detection is cheap and read-only. A cache-only client heartbeat schedules a
throttled patch-head probe; an unchanged project does not replay canonical
history or transfer the full graph. A client fetches the full project snapshot
only after background reconciliation advances the cached revision.
Reconciliation preserves unsynced human draft edits. Entries whose canonical
node moved become behind and cannot enter Sync untouched; independently pinned
entries remain committable.

## UI path

- Keep a project's Research or Inbox view open in the desktop app or browser.
- Apply a Work patch from that client or another client connected to the same
  backend.
- The visible graph, Inbox, Experiment control, and guidance validity reconcile
  automatically within a short bounded interval at one head. There is no new
  refresh control and no client rule engine.
- The circular project Refresh action keeps its existing meaning: run the
  Seed/Refresh ingestion agent. It is never repurposed as a display reload.
- A transient detection or reconciliation failure leaves the last truthful
  snapshot visible and marks only display-snapshot freshness stale; it never
  declares canonical state unreachable, clears the graph, or drops staged work.

Deliberately not possible: a second canonical state path, a client-side graph
patch, silent draft loss, or continuous full-project replay while nothing has
changed.

## Drive

1. Open the same ready-Experiment fixture in two clients against one
   acceptance-agent server. Leave client B on Research or Inbox.
2. In client A, run the deterministic Experiment fixture through its watcher
   completion. Its accepted Work patch creates Evidence and a Proposal.
3. Do not reload, navigate, hide, or refocus client B.
4. Repeat while client B has an unsynced human draft based on the old revision.
5. Observe the lightweight change check while the project remains unchanged.
6. Stage a Blocker resolution whose backend trigger manifest requires preview.
   Confirm preview is noncanonical, then Sync and confirm the browser replaces
   graph/control/guidance/head together rather than splicing projections.

## Assert

- `canonical_revision_probe_is_small_and_read_only`
- `unchanged_probe_does_not_replay_or_return_the_graph`
- `visible_client_detects_a_new_canonical_revision`
- `new_graph_content_appears_without_manual_reload`
- `new_proposal_appears_without_manual_reload`
- `external_client_changes_are_detected`
- `same_client_work_completion_still_reconciles_immediately`
- `unsynced_human_draft_is_preserved_and_moved_entries_are_behind`
- `refresh_agent_control_keeps_its_existing_meaning`
- `probe_or_reload_failure_keeps_the_last_snapshot_visible`
- `rule_triggering_human_edit_uses_backend_preview`
- `mutation_projection_replaces_graph_control_guidance_and_head_atomically`

## Failure means

The backend has committed a graph change while an open RCP window continues to
present an older graph or Inbox until the human manually reloads the page.
