# Episodes run isolated, and a branch view shows the graph diff

Date: 2026-09-28
Status: scope confirmed by the human on 2026-09-28. Design only. Nothing is
implemented. The design gets one xhigh review before code starts.

Close this handoff when, on disposable data:

- an Experiment and an Auto-research episode each run with both toggles on;
- the branch view shows only their graph diff;
- one Merge click lands the code into a chosen branch and the graph into main;
- the chosen cleanup removes the worktree and the code branch, and archives the
  graph branch.

## What it does

Every episode has two independent isolation toggles, chosen at Run:

- **Code worktree:** the episode works in its own Git worktree for each
  run-scope repository, on a fresh branch.
- **Graph branch:** the episode writes to its own research-graph branch, not
  to main.

The toggles are independent. A code-only episode writes main's graph as it goes
and keeps its code in the worktree. A graph-only episode edits the shared
checkout and keeps its graph on the branch. Whatever is isolated reaches its
target only through a human **Merge**. With both toggles off, the episode runs
as an Experiment does today.

A **Branches** view is the research graph's Git UI. It lists main and every
episode branch, and shows one branch's diff against its base. One Merge click
merges both sides.

## Settled

### One isolation method on the episode parent

- Isolation belongs to the common episode parent
  (`docs/specs/conversations-episodes-and-watchers.md#common-episode-parent`),
  not to a mode adapter. A new episode mode gets it by calling the same
  parent method. Nothing is added per mode.
- The parent records one immutable `EpisodeIsolation` before the first
  provider launch:
  `episode_id`, `graph_branch_id | None`, and
  `worktrees: [{repo_alias, machine, shared_path, worktree_path, branch,
  start_commit}]`.
- The worktree binding reuses the conversation worktree binding and its proofs
  (`src/rcp/conversation_worktrees.py`). The owner becomes a chat or an
  episode. One binding code path serves both.
- **Auto-research children inherit the episode's isolation:** the same
  worktrees and the same graph branch. There is no worktree per child.
- Recovery, Resume, Retry, and restart keep the binding. A missing or moved
  worktree fails before launch. It never falls back to the shared checkout.
- Defaults at Run: Auto-research has both toggles on. An Experiment has both
  off, as today. The human may change either before Run. Neither can change
  after the first provider launch.

### Experiments on a graph branch

- An Experiment with the graph toggle on creates its own branch, exactly as
  Auto-research does today (immutable main base, own Patch log). The graph
  branch code stops being Auto-research-only.
- An Experiment started from an existing Auto-research branch keeps writing
  to that branch, as today.

### Merge

- One **Merge** dispatches both sides. There is no order between them.
- **Graph side:** unchanged. `build_deterministic_merge_ops`
  (`src/rcp/runs/branch_merge.py`) emits the clean operations and routes
  protected changes to Proposals. The rest is residue.
- **Code side:** a new pre-merge, per worktree. Test the merge without
  touching any checkout, using `git merge-tree --write-tree`
  (Git 2.38 or later). A clean result that meets the preflight below is
  merged by RCP itself. A conflict, or a failed preflight, becomes residue.
- If both sides have no residue, RCP commits both with no provider turn.
- Otherwise one merge task starts. Its prompt carries the graph residue and
  the code residue together: the conflicting files and the merge-tree conflict
  output. The agent resolves code conflicts in the worktree and graph
  conflicts in the stage Patch.
- **Code preflight** (the same facts Integrate checks today): a clean
  worktree, an existing local target branch, and, when the target is checked
  out in the shared checkout, a clean shared checkout.
- **Code target:** the human picks the branch in the Merge panel. It defaults
  to the worktree's starting branch. The graph target is always main.
- **Pushing:** Merge is local. Pushing the merged branch stays an ordinary
  human or agent action.
- Merge remains human-dispatched only (invariant 3). An agentless merge still
  needs the click.

### Cleanup after Merge

The Merge panel offers three checkboxes, all on by default:

- **Remove worktree** removes the checkout only.
- **Delete code branch** is offered only once the target contains the branch
  tip.
- **Archive graph branch** hides the branch from the default list. Its Patch
  log stays forever (invariant 1). An archived branch can be shown again.

**Keep branch open** merges and skips cleanup, so later work can merge again.
The same controls also appear on an unmerged branch, as **Discard worktree**
and **Archive**. They name what will be lost and need a confirmation.

### What the graph diff shows

The current branch view renders the whole graph on the branch target. The
history drawer and chat cards render revision summaries as blocks of prose.
Both are hard to read. The diff view replaces them for branches:

- **Only the changed nodes**, plus their one-hop neighbours drawn gray for
  context. Unchanged nodes are hidden.
- A mark on each changed node:
  - **added**, **changed**, or **removed**;
  - **→ Proposal**: a protected change that merges as a pending Proposal;
  - **conflict**: main changed the same field since the base.
- A **change list** beside the graph: one short row per entity, with type,
  title, change, and field names. No prose sentences.
- Selecting a row or node opens **field-level before → after**. For a
  conflict, it shows base, branch, and main.
- Header counts: added, changed, Proposals, and conflicts.

### Data

The diff is a projection. It is recomputed on read and never stored.

```text
ResearchDiff
  base_head, branch_head, main_head
  rows: [ResearchDiffRow]
  counts: {added, changed, removed, proposals, conflicts}

ResearchDiffRow
  entity: node | edge | proposal | ambiguity | glossary | global
  id, type, title
  change: created | updated | removed
  fields: [field_path]
  revision                      # branch revision that last touched it
  disposition: clean | proposal | conflict

MergePreview
  graph: {clean_ops: int, proposal_ops: int, conflicts: [BranchMergeConflict]}
  code:  [{repo_alias, source_branch, target_branch, commits_ahead,
           status: clean | conflict | target_dirty | worktree_dirty,
           conflict_files: [path]}]
```

`rows` come from the existing `GraphSemanticDelta` (base → branch).
`disposition` comes from comparing that delta with main's delta since the
base, through the same logic as `build_deterministic_merge_ops`. The diff and
the merge can therefore never disagree.

## Rules this changes

- The conversation spec says "No worktree belongs to an episode or worker."
  That line changes: an episode owns its worktrees, and its children share
  them.
- The same spec says "RCP never merges." That changes for this one case: RCP
  merges a clean, preflighted episode worktree into a human-chosen branch,
  after a human Merge click. RCP still never commits dirty files, resets,
  stashes, or force-pushes. Chat Integrate is unchanged.
- The Auto-research spec's "Graph-only boundary" section changes: a branch
  episode may now also have code isolation.
- A decision record captures the second change and why.

## Checks

- An isolated episode edits only its worktrees and its branch. The shared
  checkout and main stay unchanged until Merge.
- Children resolve the parent's binding, including after restart and Retry.
- Merge with no residue on either side runs no provider turn.
- Merge with only code residue, only graph residue, or both, starts one task.
  Its prompt carries exactly that residue.
- The diff rows' dispositions equal the merge builder's classification on the
  same inputs.
- Cleanup refuses to delete a code branch that the target does not contain.
- Tests assert ids, states, and counts, never wording.
- The served-app journey in the closing condition, with network, console, and
  server logs inspected.
