# Branch node panel and orchestrator-child attention

Status: designed, not implemented. Settled decisions are below; nothing open.

Reported from an Auto-research branch: a Decision created on the branch shows
every raw field labelled "Branch", two revision-wide summaries under its
history, and the Agents dot lights on main with nothing listed to open.

## Changes

1. **A node created on a branch shows a tag, not a field diff.**
   `BranchChangeDetail` drops the field list when `change.before` is null and
   shows one "New on this branch" tag; the normal node body already shows each
   field once. An edited node keeps the diff, limited to fields the body
   renders: internal fields (`extension_type`, `extension_fields`,
   `source_refs`, `type`, revision fields) and empty-to-empty changes are not
   listed.
2. **Change history names what happened to this node.** Each
   `GraphChangeSource` entry in `graph_changes.branch_changes` carries a
   per-entity line — created, removed, or the edited field names — instead of
   the whole revision summary. View task stays.
3. **Orchestrator children never count as unread or "needs you".** A chat whose
   `conversation_kind` is `auto_research_child` is left out of unread ids, the
   Agents count, and the board's needs-you column. The orchestrator reads its
   children; a child that fails or stops reaches the human through the
   orchestrator's own attention (`needsHuman` from chain health).
4. **Main's Agents lists each branch's orchestrator.** The Agents filter keeps
   every `auto_research` conversation regardless of the viewed target, so the
   dot always has a row to open. Opening one from main switches to its branch.

## Checks

- Web unit tests: `chatWorkspace` (child excluded from unread, orchestrator
  listed off-target) and a `BranchChangeDetail` render case for created vs
  edited nodes.
- Python: `branch_changes` history line per entity.
- Served drive on a seeded Auto-research fixture: node panel on the branch,
  Agents on main with the orchestrator listed and children not in needs-you;
  390px phone view.
