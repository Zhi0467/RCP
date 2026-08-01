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
invariants: [1, 2, 3]
---

# Human authority, and Sync as the only commit

Agents assert and propose; you decide. Nothing an agent does changes project
truth, and nothing you do changes canonical history until you press Sync.

No agent runs here at all. The split is sharp: the **commit** side is thoroughly
tested, the **pre-Sync** side is entirely untested because the draft lives in
[`humanDraft.ts`](../../web/src/humanDraft.ts) — in the browser, where the
backend cannot see it.

## Setup

A temporary copy of the demo project — it ships with a pending proposal and an
ambiguity waiting on judgment.

## Drive

1. Open the project. Find the pending proposal in the judgment queue.
2. Approve it.
3. Open a node whose wording reads badly. Edit the prose **directly** — a
   literal text edit, not a chat asking an agent to rewrite it.
4. Set a node's standing.
5. Change the project truth scope — add or remove a repository.
6. Look at the graph before syncing. Reload the page.
7. Sync.

## Assert — browser, not covered

- `direct_prose_editor_opens` — a plain text editor, not a chat
- `no_chat_run_was_started` — rewording is a human edit, never an agent request
- `draft_holds_the_edit` before Sync
- `edited_node_standing_cleared_to_asserted` in the draft
- `canonical_unchanged_before_sync` — no new patch folder, revision has not moved
- `draft_survives_reload`
- `sync_control_reflects_pending_changes`

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
reaching canonical files without going through Sync. The untested half is the
one where you lose work you already typed.
