# RCP merges clean episode worktrees

Date: 2026-09-28. Status: active. Plan in the
[episode isolation handoff](../handoffs/handoff-2026-09-28-episode-isolation.md).

## Decision

When a human clicks Merge on an episode, RCP runs a pre-merge. It commits the
episode worktree's own uncommitted leftovers, then tests the code merge with
`git merge-tree --write-tree`, which touches no checkout and moves no ref.

If neither the code nor the graph has residue, RCP lands the code itself, with
no provider turn:

- when the target is checked out in the clean shared checkout, by a real
  `git merge` there;
- otherwise by building the commit and moving the target with a
  compare-and-swap `git update-ref`, refusing if the target moved.

A target checked out in any other linked worktree, or equal to the episode
branch, refuses Merge.

If either side has residue, one merge task runs. Its agent lands the code with
a merge commit inside chat Integrate's local-merge write scope, RCP verifies
the landing, and RCP makes the single graph commit. Squash stays agentless.

This narrows the conversation rule that RCP never merges or commits dirty files.
Chat Integrate still runs as an agent turn. RCP still never commits the shared
checkout's changes, resets, stashes, or force-pushes. A dirty shared checkout
with the target checked out refuses Merge.

## Why

- The graph merge already works this way: deterministic operations first, an
  agent only for residue. Code merges should match, so one Merge click behaves
  the same on both sides.
- A clean merge has one correct result. Spending a paid, multi-minute agent
  turn on it adds cost and a chance of error, and no judgment.
- Leftovers in the episode worktree are the episode's own work. An episode that
  ends on Stop often leaves some, and refusing would make the human commit them
  by hand every time. The shared checkout belongs to the human, so it still
  refuses.
- The merge-tree test proves a clean result before any ref moves, and the two
  landing paths keep a checked-out target's files and index in sync.

## Consequences

- RCP now writes Git refs and commits in the human's repository, but only on a
  human Merge click, only to the local target the human chose, and only the
  episode's own leftovers.
- Merge is local. Pushing stays an ordinary action.
- Code isolation needs Git 2.38 or later on the execution host. With older Git
  the Run panel disables the code toggle. There is no agent fallback path.
