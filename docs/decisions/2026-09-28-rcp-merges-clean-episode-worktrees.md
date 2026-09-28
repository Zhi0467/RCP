# RCP merges clean episode worktrees

Date: 2026-09-28. Status: active, design stage. Plan in the
[episode isolation handoff](../handoffs/handoff-2026-09-28-episode-isolation.md).

## Decision

When a human clicks Merge on an episode, RCP merges each clean episode worktree
into the branch the human picked, by itself, with no provider turn. It tests
the merge first with `git merge-tree`, which touches no checkout. A conflict or
a failed preflight goes to the merge agent as residue, next to the graph
residue, in one merge task.

This narrows the conversation rule that RCP never merges. Chat Integrate still
runs as an agent turn. RCP still never commits dirty files, resets, stashes, or
force-pushes.

## Why

- The graph merge already works this way: deterministic operations first, an
  agent only for residue. Code merges should match, so one Merge click behaves
  the same on both sides.
- A clean merge has one correct result. Spending a paid, multi-minute agent
  turn on it adds cost and a chance of error, and no judgment.
- `git merge-tree --write-tree` proves a clean result before any ref moves, so
  RCP never leaves a half-merged checkout.

## Consequences

- RCP now writes Git refs in the human's repository, but only on a human Merge
  click, and only to the target the human chose.
- Merge is local. Pushing stays an ordinary action.
- Execution hosts need Git 2.38 or later for `merge-tree --write-tree`. An
  older Git sends every code merge to the agent, and says why.
