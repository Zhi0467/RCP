---
id: S125-auto-research-graph-branch-merge
status: implemented
tier: live
driver: pytest + browser
covered_by:
  - tests/test_branch_history.py
  - tests/test_branch_merge.py
  - tests/test_branch_merge_proposals.py
  - tests/test_branch_merge_api.py
  - tests/test_paused_branch_merge.py
  - tests/test_branch_target_storage.py
  - tests/test_auto_research_experiments.py
  - tests/test_experiment_watcher_targets.py
  - web/tests/experimentBoard.test.mjs
  - web/tests/projectTabs.test.mjs
  - web/tests/campaigns.test.mjs
last_passed: 2026-08-18 — focused/full backend and Web suites plus a served live
  Codex browser drive kept branch-only Evidence off main, advanced main
  independently, merged one attributable transition, and kept console/server clean
last_checked: >-
  2026-09-07 — full backend and Web suites passed. Hermetic merge API workflows
  cover direct changes plus pending main Proposals, repeated human approval,
  three successive source value changes, stable retry identity, unchanged
  rejected reviews, and writer admission fences. A disposable served-browser
  drive verified ordinary branch editing and chats with a deterministic provider;
  no current live-provider or SSH qualification is claimed.
invariants: [1, 3, 6, 7b, 10g]
reported_by: confirmed design handoff, 2026-08-17
---

# Auto-research changes its branch before a human merges it to main

An Auto-research episode starts from one coherent main revision and writes every
graph change to its persistent episode branch while main remains independently
editable. Repository files and provider sessions remain ordinary project state;
this is a graph branch, not a reversible project sandbox.

After the episode completes or pauses for a human, Runs can dispatch a dedicated
orchestrator-authority merge agent. The agent rebases the branch's typed semantic
delta onto current main, resolves conflicts through its bounded correction loop,
and commits one attributable main transition or nothing. The branch remains as
replayable canonical history after any merge.

The human confirmed on 2026-09-05 that a merged paused episode cannot resume.
When the current orchestrator and all remaining turns owned by that episode are
paused, **End and merge to main** ends the episode before the merge starts.
Resume and Retry stay unavailable even if the merge fails. Saved stages and
branch history remain intact, and a failed merge can be retried.

## UI path

1. Open **Runs**, start an Auto-research episode, and open its episode detail.
2. Confirm the detail shows a compact branch identity with its main base and
   current branch head. Main graph views do not show branch-only changes.
3. While the episode runs, make an unrelated human edit on main. Confirm both
   heads advance independently.
4. Confirm **Merge to main** remains visible while the episode runs. Click it
   and confirm the current blocker appears beside the control without starting
   a merge. Let the episode reach a durable ending or human pause with no
   branch-writing task active.
5. Click **Merge to main**. Follow the ordinary merge task detail while the
   graph-only agent runs and, if needed, corrects its candidate.
6. Confirm one successful main transition appears with branch, episode, head,
   task, and human provenance. The branch detail reports the merged head and
   remains present; there is no discard or conflict viewer.
7. Repeat with a deterministic main/branch semantic conflict. The merge agent
   receives it, corrects or rebases against current main, and either commits one
   complete transition or leaves main unchanged.
8. Pause the current orchestrator with no running branch writers or unsettled
   child Work/Experiment. Confirm **End and merge to main** appears. Click it
   and confirm the episode ends, the merge completes, and Resume/Retry are
   unavailable for the old turn. Repeat with a failing merge: the episode stays
   ended and the branch can retry its merge.

## Assertions

- Episode creation pins one main base and durably binds the branch before any
  provider launch.
- Root, continuation, child Work, child Experiment, watcher, correction,
  settlement, and report graph reads and writes target the episode branch.
- Branch patches never advance or materialize main, and main remains writable.
- Existing beliefs retain human approval protection on the branch and during
  merge. Canonical human branch changes may become pending main Proposals;
  branch approval never grants the merge agent main approval authority.
- Branch watchers wake only branch-bound work. A successful merge reaches
  ordinary main watchers once through the committed main transition.
- Active or otherwise non-quiescent branches, already-merged heads, concurrent
  merges, and cross-project branch lookups fail closed.
- Ending a paused episode and admitting its merge is atomic. Concurrent Resume
  either admits first and blocks merge, or loses to the permanent ending fence.
- A paused child with its own lifecycle blocks merge; ending the parent never
  silently abandons separately authorized child work.
- A moving main head rebuilds the merge context and retries; it never overwrites
  the newer head or partially appends.
- A crash after the main commit but before its branch receipt reconciles to the
  existing commit and cannot merge the same branch head twice.
- Canonical local and remote state can create, replay, append, merge, and retain
  the branch without Git branches or repository worktrees.

## Failure means

Auto-research changes the project main graph before a human merge, a merge can
overwrite a newer main head or duplicate a committed branch head, repository
state is presented as branched or reversible, or the Runs detail requires a
manual conflict or branch-management interface.
