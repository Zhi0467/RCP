---
id: S52-explicit-rejection-and-node-removal
status: implemented
tier: hermetic
driver: browser
covered_by: tests/test_agent_schema.py, tests/test_history.py, tests/test_api.py, tests/test_sync.py, web/tests/humanDraft.test.mjs, web/tests/runDialog.test.mjs, browser 2026-08-03
invariants: [1, 3]
reported_by: human, 2026-08-03
last_passed: 2026-08-03 — isolated local browser drive covered node and Proposal
  choose/withdraw persistence, both removal guards, confirmation/undo, Sync, and
  incident-relation removal; agent schema and admission races are covered by pytest
---

# Node judgments are explicit, and graph nodes can be removed

`asserted`, `accepted`, and `contested` are three distinct node states. The UI
does not try to encode them with one binary action: it presents the two human
judgments explicitly, with neither selected for asserted content.

A Proposal likewise keeps its unresolved `pending` state distinct from its two
terminal human decisions. An untouched Proposal is not rejected merely because
the human Syncs unrelated work.

RCP also has one `remove_nodes` graph operation. Removing a node removes its
incident edges from the current graph. It never edits or deletes earlier patch
files, so the operation and the state it removed remain reconstructable from
append-only history.

## UI path

1. In a node's detail window, **Contest** and **Agree** are simultaneously
   visible. Neither is selected for `asserted`; exactly one is selected for
   `contested` or `accepted`. Selecting the active judgment again clears it to
   `asserted`, and selecting the other replaces it.
2. In **Inbox**, every pending Proposal shows **Reject** and **Approve** beside
   each other. Neither is selected while the Proposal is pending. Selecting
   either stages that terminal decision; selecting it again clears the decision,
   and selecting the other replaces it.
3. A node's detail window also exposes **Remove node...**, visually separate
   from its judgment buttons. The confirmation names the node and reports how
   many connected relations will be removed from the current graph.
4. Confirming stages the removal in the same project-wide draft as edits and
   judgments. The node remains visible until the human presses **Sync**, and the
   staged removal can be undone before Sync.
5. The action is unavailable for an accepted node and explains that the human
   must first clear or contest the judgment and Sync. The backend rejects the
   operation even if a caller bypasses the UI.
6. RCP refuses to remove an Experiment while its bounded experiment loop is
   active. Removal never silently cancels external work.
7. Sync applies `remove_nodes` for an asserted or contested node and removes all
   incident edges from current state in the same revision. The patch log remains
   append-only.
8. If the same draft decides a pending Proposal whose related node is also
   removed, Sync withdraws that Proposal as stale instead of emitting operations
   against the removed node. The removal and withdrawal both commit atomically,
   and the history explains that the removal made the Proposal stale.
9. The same operation is present in the strict graph-agent schema and contract.
   Graph-capable agents may author it under their existing per-run authority;
   adding the operation does not widen Experiment-loop or other capability
   boundaries.

## Drive

1. Open asserted, accepted, and contested nodes. Stage and clear both judgments,
   navigate away, return, and reload.
2. Open Inbox with one pending Proposal. Reject it, clear that staged choice,
   then approve it and switch back to Reject. Navigate away and return.
3. Open an asserted or contested node with connected edges. Choose **Remove
   node...**, inspect the confirmation, confirm, then undo the staged removal.
   Stage it again and Sync.
4. With a pending Proposal related to an asserted or contested node, stage both
   a Proposal decision and removal of that node, then Sync. Repeat with Approve
   and Reject.
5. Attempt the same operation on an accepted node and on an Experiment whose
   bounded loop is active.
6. Submit `remove_nodes` through a graph-capable agent patch.

## Assert

- Contest and Agree are simultaneously visible and faithfully represent all
  three node standings.
- Reject and Approve are simultaneously visible for every pending Proposal.
- Exactly one staged Proposal decision is selected, it survives navigation and
  reload, and Reject applies none of the Proposal's semantic operations.
- A staged node removal is reversible before Sync and survives navigation and
  reload like every other graph-draft action.
- Sync appends history; it never deletes or rewrites an existing patch.
- The removed node and every incident edge disappear from the current graph and
  from `research.md`; replay still records what was removed and why.
- Accepted standing and an active bounded Experiment loop each prevent removal.
- A same-draft Proposal decision for a removed dependency is recorded as a stale
  withdrawal, with no semantic or standing operation targeting the removed node.
- Asserted and contested nodes can be removed by either the human UI or a valid
  graph-capable agent patch.
- Agent access to `remove_nodes` does not grant standing authority or broaden
  any run's existing node scope.

## Failure means

The UI cannot represent all three node standings, an untouched Proposal is
silently rejected, disagreement is confused with deletion, removal leaves
dangling edges, accepted truth disappears, or a live bounded loop is silently
abandoned.
