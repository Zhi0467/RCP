# Research Control Panel blueprint v0.10 amendment

This amendment supersedes the v0.5 prohibition on removing graph nodes and the
older operation tables that omit `remove_nodes`. It does not weaken append-only
history, materialized-file ownership, human standing authority, Proposal
authority, or the bounded Experiment-loop capability.

## D30 — Explicit judgments and non-rewriting node removal

Node standing has three UI states and two human judgments. **Contest** and
**Agree** are simultaneously visible toggle controls. Neither selected means
`asserted`; Contest selected means `contested`; Agree selected means `accepted`.
Selecting the active judgment again clears it to `asserted`, and selecting the
other replaces it. All changes remain in the project draft until **Sync**.

A pending Proposal likewise shows **Reject** and **Approve** simultaneously.
Neither selected means the Proposal is still pending. A staged decision may be
cleared before Sync. After Sync, an approved or rejected Proposal is terminal;
later graph work does not rewrite its historical resolution.

`remove_nodes` is a strict graph operation containing one or more `node_ids`.
For every target, admission requires:

- the node exists in the current graph;
- its standing is not `accepted`; and
- if it is an Experiment, no bounded Experiment loop is active for it. Both a
  durable queued/running loop task and a nonterminal attempt count as active.

The whole patch is rejected if any target fails. A valid operation removes each
target and every incident edge from the current materialized graph in the same
revision. A pending Proposal that depended on the removed node becomes stale
under the existing Proposal dependency rule; removal does not let an agent
resolve or withdraw it.

The operation is available in the strict graph-agent schema and as a standalone
human approval operation emitted by project-wide Sync. Existing per-run
authority still applies: in particular, a bounded Experiment-loop patch cannot
use `remove_nodes`, and no agent gains standing authority. The human UI refuses
an accepted target even when the same local draft also clears or contests it;
the standing change must be Synced first so accepted research never disappears
inside one compound gesture.

Removal changes current materialized state, not history. RCP appends the removal
patch and never edits or deletes the earlier creation, update, edge, Proposal,
ambiguity, or patch records. Replay therefore retains what existed, what was
removed, and the revision that removed it even though the current graph no
longer contains the node or its incident edges.

[`acceptance/S52-explicit-rejection-and-node-removal.md`](acceptance/S52-explicit-rejection-and-node-removal.md)
is the executable contract for this amendment.
