# A branch merges on branch facts

Confirmed by the human 2026-09-14. This record reverses the eligibility rule in
`docs/specs/auto-research-and-branch-merge.md` ("Branch lifecycle and merge
eligibility") that tied merging an Auto-research branch to its episode's
lifecycle.

## What happened

An Auto-research branch carried twenty revisions of graph work. Its episode was
stuck in `wrapping_up` because its report could not start. The human's merge
attempt was refused with an episode-lifecycle reason (the exact diagnostic was
not captured), and the same stuck episode counted as the project's one live
Auto-research episode, so no new episode could start either. Merge eligibility
read the episode's ending, quiescence, and paused orchestrator, and the merge
request model required a non-null ending. The branch itself was in perfect
order: an exact head, no writer touching it.

## The decision

A graph branch is merge eligible when its head is exact, newer than its base,
not already covered by a successful merge receipt, and no queued, running, or
pausing graph-capable task is writing to that branch. Nothing about the episode
is a condition: not its status, ending, wrap-up state, quiescence, or whether
its orchestrator is paused. Merging does not end the episode. An orchestrator
that later writes a newer head produces a branch that may be merged again, as
the spec already allows. The separate "End and merge to main" action goes away;
ending an episode is Stop, merging is Merge, and a human may do either.

## What this costs, stated plainly

A merge can land while an episode is still running, so the human, not the
lifecycle, decides when a head is worth merging. Human review of the merge
(Proposals for protected changes) is unchanged and still gates what reaches
main.
