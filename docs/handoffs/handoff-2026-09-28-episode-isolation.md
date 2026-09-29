# Episodes run isolated, and a branch view shows the graph diff

Date: 2026-09-28
Status: scope and design confirmed by the human on 2026-09-28, after a grilling
round that settled every open choice below. The xhigh design review ran the
same day; its eleven findings are folded in, and one choice it raised was
settled by the human (Auto-research keeps the graph toggle on). Slice 1, Binding,
is implemented. It includes persisted owner identity and operation state,
inherited bindings, Run admission, launch write roots, and fail-closed recovery.
Slice 2, Experiment graph branch, is implemented. Experiments can create graph
branches, and branch reads accept either episode kind. The API exposes the
toggles and resolves omitted Auto-research code isolation from eligibility.
Slice 3, Pre-merge and agentless merge, is implemented in the API. It includes
owner reservations, job gates, recoverable attempts, Git landing and verification,
MergePreview, and recorded cleanup. Slice 4, Merge task with code, is
implemented: a code conflict or graph residue starts one merge task with
Integrate's roots, and RCP verifies the landing before the graph commit. Diff
and web controls remain in slices 5–6, and the closing journey remains in
slice 7. The settled choices below are unchanged.

Close this handoff when, on disposable data:

- an Experiment and an Auto-research episode each run with both toggles on;
- the branch view shows only their graph diff;
- one Merge click lands the code into a chosen branch and the graph into main;
- the chosen cleanup removes the worktree and the code branch, and archives the
  graph branch.

## What it does

Every episode has two independent isolation toggles, chosen at Run:

- **Code worktree:** the episode works in its own Git worktree of its one
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

### Toggles at Run

- Defaults: Auto-research has graph isolation on and code isolation on when
  eligible, otherwise off. An Experiment has both off, as today. The Run panel
  shows both, and the human may change either before Run.
  Neither can change after the first provider launch.
- Auto-research's graph toggle is locked on. Its Decision exception stays
  branch-only (`docs/specs/authority-and-proposals.md`), so the orchestrator
  never chooses Decisions on main. Only its code toggle is free.
- The code toggle needs exactly one repository in the run scope, as chat
  worktrees do today. With more, the toggle is disabled and says why.
  Multi-repository isolation is later work, not this handoff.
- The code toggle needs Git 2.38 or later on the execution host
  (`git merge-tree --write-tree`). With older Git the toggle is disabled and
  names the version. There is no fallback merge path.
- The code toggle refuses when a machine writable-path grant covers the shared
  checkout, because that grant would make the shared checkout writable again.
  Every launch rechecks this.

### One binding, owned by the root episode

- Isolation belongs to the common episode parent
  (`docs/specs/conversations-episodes-and-watchers.md#common-episode-parent`),
  not to a mode adapter. A new episode mode gets it by calling the same
  parent method.
- The episode that first creates the isolation owns it. It records one
  immutable `EpisodeIsolation` before its first provider launch:
  `owner_episode_id`, `graph_branch_id | None`, and
  `worktree: {repo_alias, machine, execution_host, shared_path, worktree_path,
  git_common_dir, branch, starting_branch, starting_commit} | None`.
- Everything inside that episode uses the one binding. Nothing chooses its own:
  - Auto-research children;
  - **Add N turns** continuations (a new episode id, chained by
    `continues_episode_id`);
  - human-started Experiments on the owner's graph branch (they keep their own
    episode, as today).
  Each of these stores only `isolation_owner_episode_id` and resolves the
  binding through it.
- The worktree binding reuses the conversation worktree binding, its proofs,
  and its remote script (`src/rcp/conversation_worktrees.py`,
  `src/rcp/transport/conversation_worktree.py`). Its owner becomes a chat or an
  episode. One binding code path serves both.
- Recovery, Resume, Retry, and restart keep the binding. A missing or moved
  worktree fails before launch. It never falls back to the shared checkout.
- An episode with the code toggle on writes only its worktree. The shared
  checkout is outside its write roots.
- Concurrent turns inside one episode (Auto-research workers, child and branch
  Experiments) share the one worktree, just as they share the shared checkout
  today. Isolation adds no per-turn writer fence; only Merge fences the
  binding.
- The binding fixes the code side: the repository alias and the execution host.
  Graph truth membership stays separate and unchanged. A child or branch
  Experiment whose Work profile runs on another host refuses at admission.
  Binding-aware write-scope resolution, which today serves only `work_auto`
  chats, extends to episode `work_auto` and `orchestrate` launches.
- The binding identity is immutable. Operation state (creating, merging,
  removing, removed) lives in a separate record with its own lock order.

### Experiments on a graph branch

- An Experiment with the graph toggle on creates its own branch, exactly as
  Auto-research does today (immutable main base, own Patch log). The graph
  branch code stops being Auto-research-only.
- An Experiment started on an existing branch keeps writing to that branch and
  uses that branch's isolation owner, as above.

### Fence

- A Merge click first persists an owner-keyed **merge reservation**, before any
  leftovers commit, also when no agent runs. Every admission on the binding
  (turns, continuations, Resume, Retry, recovery, Remove worktree) checks it.
  It lasts through landing, verification, and cleanup.
- Merge waits for live turns on the binding.
- Merge and Remove worktree also refuse while any compute job started by an
  episode on the binding is live or unobservable. A stopped watcher is not job
  completion. RCP still never cancels the job itself.

### Merge

Merge admission keys on the isolation owner. The code side and the graph side
each have their own delivered state. Merge is offered while either side has
undelivered work: a code-only owner, or a branch whose graph was already
delivered but whose code moved on, can merge.

One **Merge** click first persists the reservation and a **merge attempt**
record in phase `pre_merge`, before any Git write. A reservation whose attempt
never progressed past `pre_merge` is reclaimed by the next Merge or at startup,
after the attempt's recorded state is reconciled. Then it runs the
**pre-merge**, RCP's automatic first step:

1. **Leftovers.** If the episode worktree has uncommitted changes, RCP commits
   them as one leftovers commit on the episode branch. Gitignored files stay
   out.
   Before that, an unfinished Git operation (merge, rebase, cherry-pick),
   unmerged index entries, or dirty submodule contents refuse Merge and need
   repair. They are never leftovers.
2. **Refusals.** Merge refuses, and lists the files or the reason, when:
   - the local target branch is missing;
   - the target is the episode branch itself;
   - the target is checked out in any worktree other than the shared checkout
     (all registrations on the execution host are inspected);
   - the target is checked out in the shared checkout and that checkout is
     dirty.
   Refusals are validation errors, never residue.
3. **Graph.** Today's `build_deterministic_merge_ops`
   (`src/rcp/runs/branch_merge.py`) gives clean ops, Proposals, and residue.
4. **Code.** `git merge-tree --write-tree <target> <episode branch>` on the
   execution host gives clean, or a conflict with its files and output. This
   runs as a new operation of the shipped worktree script. It touches no
   checkout and moves no ref.

Then RCP advances the attempt to phase `landing` with the owner, the source
commit (after leftovers), the target ref and its commit, the graph head, the
history mode, and the cleanup choices. Just before landing it rechecks those
commits and the checkout's identity and cleanliness.

- **No residue on either side:** RCP lands the code, then commits the graph,
  with no provider turn.
  - Target checked out in the (clean) shared checkout: RCP runs a real
    `git merge` there, so the files and index stay in sync.
  - Target not checked out: RCP builds the commit from the tested tree and
    moves the target with a compare-and-swap `git update-ref`. If the target
    moved, Merge refuses rather than overwriting.
  - The graph commits as today's single main transition.
- **Residue on either side:** one merge task starts. The agent lands all the
  code, and RCP makes the single graph commit.
  - The task keeps `orchestrate` and gets the chat Integrate local-merge write
    scope: the episode worktree plus the shared checkout
    (`src/rcp/runs/chat.py`, the related write-scope variants). The prompt
    renders that same resolved scope.
  - Its prompt carries the full pre-merge output: the graph residue with
    reasons, the code status, the conflict files, and the merge-tree output.
  - The agent merges the code for the one repository, clean or conflicted, the
    way Integrate does, with a merge commit. Squash needs an agentless merge.
    It writes only the graph residue ops to `patch.json`. With no graph
    residue, the code lands in its own turn and the graph then merges with no
    provider turn; a code-only owner's task has no graph side.
    RCP prepends the clean ops, self-checks, runs the existing correction loop
    (`PATCH_CORRECTION_MAX_ROUNDS`), re-prepares if main moved, and commits
    one main transition or nothing. There is no in-turn Apply to main.
  - The merge launch runs on the binding's execution host, and its code alias
    comes from the binding.
- **Verification.** Before the graph commits, RCP checks the code landed: the
  target contains the source commit, or the attempt records the squash commit
  RCP made. A graph Patch without landed code fails the task.
- **Interrupted attempts.** A new Merge first reconciles any unfinished
  attempt from its record, squash included, before it runs a fresh pre-merge.
  An uncertain SSH result is reconciled the same way, never guessed.
  - An episode with no code toggle launches its merge with no repository roots,
    as today.
- **Retries.** Inside one merge task, the correction rounds and main
  re-preparation are automatic. After they run out, the task ends
  `retryable`. There is no automatic re-dispatch: the human clicks Merge
  again. That pre-merge sees a target that already contains the episode tip as
  a code no-op, so only what is left goes to the agent.
- **History.** The Merge panel offers **Merge commit** (default, `--no-ff`) or
  **Squash**. With Squash, **Keep branch open** is disabled, and RCP records the
  squash commit so the code branch can be deleted.
- **Target.** A local branch only. The human picks it in the Merge panel; it
  defaults to the worktree's starting branch. There is no pull-request option.
  The graph target is always main.
- **Pushing** stays an ordinary human or agent action.
- Merge remains human-dispatched only (invariant 3). An agentless merge still
  needs the click.

### Cleanup after Merge

The Merge panel offers three checkboxes, all on by default:

- **Remove worktree** removes the checkout only. It refuses while any episode
  or known compute job on the binding is live. Later episodes that point at a
  removed worktree refuse before launch.
- **Delete code branch** is offered only once the target contains the branch
  tip, or RCP recorded the squash commit for it. It needs **Remove worktree**
  too, because the retained checkout holds the branch.
- **Archive graph branch** hides the branch from the default list. Its Patch
  log stays forever (invariant 1). An archived branch can be shown again.

**Keep branch open** merges and skips cleanup, so later work can merge again.
The same controls also appear on an unmerged branch, as **Discard worktree**
and **Archive**. They name what will be lost and need a confirmation.

Each cleanup step is recorded, idempotent, and retryable on its own. A cleanup
failure shows as a cleanup failure, never as a failed code or graph delivery.

### What the graph diff shows

The current branch view renders the whole graph on the branch target. The
history drawer and chat cards render revision summaries as blocks of prose.
Both are hard to read. The diff view replaces them for branches:

- **Only the changed nodes**, plus their one-hop neighbours drawn gray for
  context. Unchanged nodes are hidden.
- A mark on each changed node:
  - **added**, **changed**, or **removed**;
  - **→ Proposal**: a protected change that merges as a pending Proposal;
  - **conflict**: main changed the same field since the base;
  - **needs agent**: main did not conflict, but the merge builder still sends
    it to the agent (for example a Decision outcome). The row names the
    builder's reason.
- A **change list** beside the graph: one short row per entity, with type,
  title, change, and field names. No prose sentences.
- Selecting a row or node opens **field-level before → after**. For a
  conflict, it shows base, branch, and main.
- Header counts: added, changed, removed, Proposals, conflicts, needs agent.

### Data

The diff and the preview are projections. They are recomputed on read and
never stored.

```text
ResearchDiff
  base_head, branch_head, main_head
  rows: [ResearchDiffRow]
  counts: {added, changed, removed, proposals, conflicts, needs_agent}

ResearchDiffRow
  entity: node | edge | proposal | ambiguity | glossary | global
  id, type, title
  change: created | updated | removed
  neighbours: [id]              # one-hop context
  revision                      # branch revision that last touched it
  paths: [ResearchDiffPath]

ResearchDiffPath
  field_path
  base, branch, main            # values, as the change views keep today
  delivered: bool               # covered by an earlier merge receipt
  conflict: bool
  needs_agent: bool
  residue_reason: <MERGE_RESIDUE_REASONS key> | None
  needs_proposal: bool          # a requirement, not a built operation

MergePreview
  delivered_baseline            # the builder's previous delivered snapshot
  graph: {ops: int, residue: [{path, reason}]}
  code:  {repo_alias, source_branch, target_branch, commits_ahead,
          leftover_files: [path],
          status: clean | conflict | already_merged | target_dirty
                  | target_missing,
          conflict_files: [path]} | None
  needs_agent: bool
```

`rows` come from the existing `GraphSemanticDelta` (base → branch) and keep
the values, provenance, and neighbour ids that `src/rcp/api/graph_changes.py`
already carries. Classification is per path, not per entity: one node can need
a Proposal and hold a conflict at once. The flags come from the same call to
`build_deterministic_merge_ops` that Merge uses, against the same delivered
baseline, so the diff and the merge cannot disagree. Rows an earlier merge
already delivered stay in the diff, with those paths marked `delivered`. A node's mark in the
graph is its most severe path.

## Rules this changes

- The conversation spec says "No worktree belongs to an episode or worker."
  That changes: a root episode owns its worktree, and every episode inside it
  shares it.
- The same spec says "RCP never merges, commits dirty files, resets, stashes,
  or force-pushes." Two parts change, for episode Merge only: RCP merges a
  clean, preflighted episode worktree into a human-chosen local branch, and it
  commits the episode worktree's own leftovers first. RCP still never commits
  the shared checkout's changes, resets, stashes, or force-pushes. Chat
  Integrate is unchanged.
- The Auto-research spec's "Graph-only boundary" section changes: a branch
  episode may also have code isolation.
- The branch-merge spec says the merge agent "receives scratch but no
  repository write roots." With code isolation, it receives the Integrate
  local-merge scope.
- The decision record
  [RCP merges clean episode worktrees](../decisions/2026-09-28-rcp-merges-clean-episode-worktrees.md)
  captures the second and fourth changes and why.

## Slices

Each slice is one Codex implementation pass, reviewed once as it lands.

1. **Binding — implemented.** `EpisodeIsolation` storage and its separate operation state,
   the chat-or-episode worktree owner, `isolation_owner_episode_id` resolution
   for children, continuations, and branch Experiments, Run admission (one
   repository, Git version, grant overlap, host compatibility, Auto-research
   graph toggle locked on), binding-aware write roots for `work_auto` and
   `orchestrate`, and recovery fail-closed. Specs for these rules change in
   the same slice.
2. **Experiment graph branch — implemented.** The graph toggle for Experiments
   reuses episode branch creation. Branch reads accept Experiment owners.
   Omitted Auto-research code isolation resolves from eligibility; an omitted
   Experiment choice stays off. Current-behavior specs are updated.
3. **Pre-merge and agentless merge — implemented.** Owner-keyed merge admission with
   separate code and graph delivery, the merge reservation and job gate, the
   merge attempt record and its reconciliation, interrupted-Git detection, the
   leftovers commit, the refusals, the merge-tree operation in the shipped
   script, `MergePreview`, landing (checkout merge or compare-and-swap),
   verification, Squash, and recorded cleanup steps.
4. **Merge task with code — implemented.** The Integrate write scope on the merge launch, the
   combined prompt, and an **audit of the branch-merge prompt and its
   correction prompt**: what each round says and how it is built, now that it
   also carries code state.
5. **Diff projection.** `ResearchDiff` and its API.
6. **Web.** Run toggles, the Branches diff view, and the Merge panel.
7. **Specs and journey.** Current-behavior spec updates, then the served-app
   journey in the closing condition.

## Checks

- An isolated episode edits only its worktree and its branch. The shared
  checkout and main stay unchanged until Merge.
- Children, continuations, and branch Experiments resolve the owner's binding,
  including after restart and Retry. None creates a second one.
- Run refuses the code toggle for two run-scope repositories, for old Git, and
  for a grant covering the shared checkout. Auto-research cannot turn the
  graph toggle off.
- A code-only owner can Merge. So can a branch whose graph was delivered but
  whose code moved on.
- Merge refuses a target checked out in another worktree, a target equal to
  the source, an unfinished Git operation, and a live or unobservable job.
- A merge task whose Patch is valid but whose code did not land fails before
  the graph commit.
- A crash after landing and before the graph commit, squash included, is
  reconciled by the next Merge from the attempt record.
- Merge refuses a dirty shared checkout that has the target checked out, and a
  missing target. It commits worktree leftovers.
- Merge with no residue on either side runs no provider turn.
- Merge with only code residue, only graph residue, or both, starts one task.
  Its prompt carries exactly that pre-merge output, and its write scope is the
  Integrate local-merge scope only when code isolation is on.
- A second Merge after a code-landed, graph-failed task sees the code as
  already merged.
- Squash disables Keep branch open and still allows branch deletion.
- The diff rows' dispositions equal the merge builder's classification on the
  same inputs.
- Cleanup refuses to delete a code branch the target does not contain and RCP
  did not squash.
- Tests assert ids, states, and counts, never wording.
- The served-app journey in the closing condition, with network, console, and
  server logs inspected.
