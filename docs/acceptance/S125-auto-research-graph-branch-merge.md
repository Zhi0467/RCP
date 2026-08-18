---
id: S125-auto-research-graph-branch-merge
status: implemented
tier: live
driver: pytest + browser
covered_by:
  - tests/test_branch_history.py
  - tests/test_branch_merge.py
  - tests/test_branch_merge_api.py
  - tests/test_branch_target_storage.py
  - tests/test_auto_research_experiments.py
  - tests/test_experiment_watcher_targets.py
  - web/tests/experimentBoard.test.mjs
  - web/tests/projectTabs.test.mjs
last_passed: 2026-08-18 — focused/full backend and Web suites plus a served live
  Codex browser drive kept branch-only Evidence off main, advanced main
  independently, merged one attributable transition, and kept console/server clean
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

## UI path

1. Open **Runs**, start an Auto-research episode, and open its episode detail.
2. Confirm the detail shows a compact branch identity with its main base and
   current branch head. Main graph views do not show branch-only changes.
3. While the episode runs, make an unrelated human edit on main. Confirm both
   heads advance independently.
4. Let the episode reach a durable ending or human pause with no branch-writing
   task active. Confirm **Merge to main** appears only then.
5. Click **Merge to main**. Follow the ordinary merge task detail while the
   graph-only agent runs and, if needed, corrects its candidate.
6. Confirm one successful main transition appears with branch, episode, head,
   task, and human provenance. The branch detail reports the merged head and
   remains present; there is no discard, branch switcher, or conflict viewer.
7. Repeat with a deterministic main/branch semantic conflict. The merge agent
   receives it, corrects or rebases against current main, and either commits one
   complete transition or leaves main unchanged.

## Assertions

- Episode creation pins one main base and durably binds the branch before any
  provider launch.
- Root, continuation, child Work, child Experiment, watcher, correction,
  settlement, and report graph reads and writes target the episode branch.
- Branch patches never advance or materialize main, and main remains writable.
- Existing-belief Proposal restrictions and orchestrator Decision authority are
  unchanged on the branch and during merge.
- Branch watchers wake only branch-bound work. A successful merge reaches
  ordinary main watchers once through the committed main transition.
- Active or otherwise non-quiescent branches, already-merged heads, concurrent
  merges, and cross-project branch lookups fail closed.
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
