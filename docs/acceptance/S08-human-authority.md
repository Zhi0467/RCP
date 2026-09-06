---
id: S08-human-authority
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_sync.py::test_graph_sync_commits_staged_wording_and_judgment_once
  - tests/test_sync.py::test_graph_sync_withdraws_to_asserted_and_rewrites_research_once
  - tests/test_sync.py::test_graph_sync_refuses_stale_project_draft
  - tests/test_history.py::test_direct_human_prose_edit_preserves_node_standing
  - tests/test_history.py::test_direct_human_edit_rejects_non_prose_fields
  - tests/test_human_graph_editing.py::test_human_nodes_edges_preview_sync_remove_preserve_history
  - tests/test_human_graph_editing.py::test_edge_replacement_stale_draft_and_invalid_endpoint_are_atomic
  - web/tests/graphEditing.browser.test.mjs
  - web/tests/humanDraft.test.mjs
invariants: [1, 2, 3]
last_checked: 2026-09-05 — node/edge creation, reload, connection undo, prose editing and removal driven in the served browser; proposal and truth-scope browser steps were not redriven.
---

# Human authority, and Sync as the only commit

Agents assert and propose; you decide. Nothing an agent does changes project
truth, and nothing you do changes canonical history until you press Sync.

No agent needs to run here. The browser holds human intent in
[`humanDraft.ts`](../../web/src/humanDraft.ts); backend preview validates structural
changes without committing, and Sync revalidates and commits the complete batch.

## Setup

A temporary project with a pending Hypothesis-status Proposal, a Decision
awaiting choice, and an asserted open Blocker.

## Drive

1. Open the project. Find the pending proposal in the judgment queue.
2. Approve it.
3. Open a node whose wording reads badly. Edit the prose **directly** — a
   literal text edit, not a chat asking an agent to rewrite it.
4. Set a node's standing.
5. Change the project truth scope — add or remove a repository.
6. Look at the graph before syncing. Reload the page.
7. Sync.
8. Create a node with **New node**, connect it to an existing node through
   **Connections**, and inspect the complete preview before Sync. In DAG view,
   dragging a node's connection handle selects endpoints for the same editor;
   keyboard activation and selectors provide an alternative. For an Evidence
   assessment relation, provide relevance and weight explicitly.
9. Undo a staged connection removal, then remove the connection and a node through
   Sync. Confirm that earlier Patch files remain byte-identical.

## Assert — browser

- `direct_prose_editor_opens` — a plain text editor, not a chat
- `no_chat_run_was_started` — rewording is a human edit, never an agent request
- `draft_holds_the_edit` before Sync
- `edited_node_standing_cleared_to_asserted` in the draft
- `canonical_unchanged_before_sync` — no new patch folder, revision has not moved
- `draft_survives_reload`
- `sync_control_reflects_pending_changes`
- `new_node_connectable_before_sync`
- `connection_remove_and_undo_preserve_intent`
- `node_removal_changes_current_graph_not_history`

### Bounded live receipt, 2026-09-05

Built and served this branch at `127.0.0.1:8437` against disposable acceptance
data; no real provider, personal research data, or production server was used.
In the in-app browser, created a ResearchQuestion (revision 2), restored a staged
Hypothesis after reload, and connected it before Sync. An independent graph read
still showed only the saved question at revision 2. Sync committed the Hypothesis
and its `has_hypothesis` edge together as revision 3. Removal/undo, connection
removal, direct Hypothesis wording editing, and confirmed node removal reached
revisions 4–6. The SHA-256 hashes of all five prior Patch files were unchanged
after removal. No chat was dispatched. Browser pixels and server responses were
inspected. The automated interaction regression additionally covers DAG dragging,
keyboard connection selection, Evidence assessments and read-only controls.

This is a focused receipt for graph authoring, not a claim that steps 1–5's
proposal, Decision, standing and truth-scope combination was redriven end-to-end.

## Assert — pytest, covered

- `patches_appended == 1` — one visible `batch-*` for the whole sync
- `prior_patches_byte_identical`
- `approval_recorded_in_log`
- `standing_recorded_in_log`
- `truth_scope_change_recorded_in_log`
- `prose_edit_preserves_standing_semantics`
- `non_prose_fields_refused_on_a_human_edit`
- `stale_draft_refused`
- `no_net_change_writes_no_patch`
- `graph_matches_log`

## Failure means

Either an agent path is writing something only a human may write, or edits are
reaching canonical files without going through Sync. Draft-loss regressions are
user-visible failures even when backend authority tests pass.
