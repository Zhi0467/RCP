# Auto-research graph branches and agent-native merge implementation handoff

Date: 2026-08-17
Status: confirmed and ready to implement after typed operations and the transition manager

## Purpose

Run every Auto-research episode on a persistent graph-only branch so the orchestrator can exercise its full current authority without immediately changing the project's main graph. Main may continue changing while the episode runs. After the episode is quiescent, a human can dispatch an agent to semantically rebase and merge the branch onto current main.

This is branching for canonical research-graph state only. Repository files, experiments, external side effects, artifacts, and provider sessions remain in their ordinary locations and are not branched, reverted, or discarded with the graph branch.

## Confirmed authority

The branch does not reduce Auto-research authority. On its branch, the orchestrator retains the current profile exactly:

- it may create new ResearchQuestions and Hypotheses directly and attach them in the same Patch;
- any change to an existing ResearchQuestion or Hypothesis remains Proposal-only;
- it may directly create, update, relate, judge, supersede, merge, or remove Evidence, Decisions, Experiments, and Blockers within the existing operation rules;
- it may choose a governed Decision directly;
- no agent may approve a Proposal; and
- child workers retain ordinary-agent graph authority.

The global statement that only humans choose Decisions is wrong for Auto-research and must be corrected in the current specification. Human authority is retained through episode authorization, protected existing beliefs, Proposal resolution, and the explicit human dispatch of a branch merge.

## Branch identity and durability

Each Auto-research episode owns one persistent branch. Use the episode UUID as the stable branch identity unless the implementation needs a separate opaque id; do not add a user naming workflow.

Branch truth belongs in the canonical state repository under a branch namespace, not only in SQLite and not in a Git branch. The durable branch record must include at least:

- branch id and episode id;
- project identity;
- immutable main revision at branch creation;
- branch head identity;
- creation time and authorizing human snapshot;
- branch kind `auto_research`; and
- append-only merge receipts.

The branch's graph changes are an append-only Patch log. Do not rewrite its original base or Patch history when main advances or a merge occurs.

The exact on-disk layout is an implementation choice, but it must be mechanically replayable and remote-state compatible. A branch-aware history view may materialize the accepted main prefix at the immutable base revision and then apply the branch Patch log. Do not copy mutable main materializations and treat them as branch truth.

A branch revision is identified by branch id plus its branch head/revision. Do not assume an integer revision alone is globally unique across main and branches.

## Episode creation and binding

An Auto-research provider may not launch until the episode is durably bound to its branch and the branch base revision is known.

The branch is created from one coherent main revision. Root orchestrator input, branch graph materialization, and the stored base revision must all refer to that same main state.

Canonical state and SQLite cannot share one transaction. Use the existing reconciliation style so a crash may leave a recoverable orphan on one side, but never launch an unbranched episode or silently redirect an episode to main. Startup/task recovery must restore the episode-to-branch binding or fail explicitly.

## Branch-scoped graph behavior

Every graph-aware surface belonging to the episode uses the branch head:

- root orchestrator context and Apply;
- orchestrator continuations and Patch correction;
- child Work graph context and Patch apply;
- child Experiment-loop graph context, control projection, watchers, and Patch apply;
- graph-condition wakes owned by the episode;
- episode final settlement and report generation; and
- diagnostics and revision summaries shown for episode work.

A branch Patch changes only the branch. It must not alter main materialization, main revision, main control projections, or ordinary main watchers.

Main continues to accept human edits, ordinary Work Patches, other episode work, and transition-manager effects while the branch is active.

Branch-scoped watchers evaluate branch state and wake only branch-bound tasks. When a merge later changes main, ordinary main watchers observe the committed main transition through the normal mechanism.

## Graph-only boundary

Make the boundary explicit in contracts and UI without adding explanatory clutter:

- agent repository work occurs against the real project repositories;
- the branch does not create Git branches or worktrees;
- merge does not replay, copy, or revert repository files;
- there is no branch discard action; and
- a retained graph branch remains useful as an audit trail even if its repository work has already changed.

Do not advertise a branch as a reversible whole-project sandbox.

## Branch lifecycle

Keep lifecycle small and derived from existing episode state plus merge records.

A branch is writable by its episode while that episode accepts graph work. It becomes merge-eligible only when the graph head is quiescent:

- the episode has a durable ending, including `completed`, `exhausted`, `stopped`, or `failed`; or
- the episode has a durable `human_pause`; and
- no episode task that can still append a branch Patch is active.

The branch is never discarded or deleted through normal product behavior. It remains inspectable in canonical history after pause, failure, stop, completion, or merge.

A branch may have more than one merge receipt. This permits a paused episode to advance later and merge a newer head, and permits a failed merge attempt to be retried. The merge action is offered only when the current branch head has changes not covered by a successful receipt.

## Agent-native semantic rebase and merge

There is no conflict viewer and no manual node-by-node merge UI. A human with project Patch authority dispatches a dedicated merge agent from the episode's Runs detail.

The merge task is graph-only. It reads branch/main material prepared by RCP and writes a candidate merge Patch in its own scratch stage. It receives no repository write roots.

### Merge inputs

RCP prepares a closed merge context containing:

- the immutable branch-base graph;
- the current branch-head graph;
- the current main graph and main revision;
- a typed semantic delta from base to branch;
- branch Patch summaries and provenance needed to understand intent;
- the current transition-manager schema and validation command; and
- any deterministic rebase conflicts found before launch.

Do not ask the agent to infer the base from prose or inspect raw state directories freely.

### Merge authority

The merge task runs with the orchestrator graph profile and the human merge dispatch as its `authorized_by` snapshot. This allows it to carry the branch's legal direct changes onto main while preserving the protected-existing-belief and Proposal rules.

The merge agent does not gain Proposal approval, project configuration, ontology, membership, or server authority.

### Merge result

The agent authors one semantic Patch against current main that incorporates the intended branch delta and resolves conflicts. RCP prepares it through the graph transition manager and commits it atomically to main or commits nothing.

The committed main merge must carry durable provenance sufficient to answer:

- which branch and episode it came from;
- which branch base and branch head were considered;
- which main revision the candidate was rebased onto;
- which merge task produced it; and
- which human dispatched that task.

Use an explicit branch-merge Patch kind or an equivalent strict provenance contract. Do not encode merge identity only in free-text summary.

### Conflict and moving-main behavior

A conflict is agent correction input, not a new UI workflow.

- Deterministic preparation reports typed conflicts with the affected operations/nodes and current-main cause.
- The same merge agent/native session may correct its candidate through the existing bounded Patch-correction mechanism.
- No partial main state is committed.
- If main advances after preparation but before append, expected-revision admission fails, RCP rebuilds the merge context on the new main head, and the merge task retries/rebases rather than overwriting main.
- A failed or paused merge task leaves both branch and main unchanged and can be retried by dispatching another merge task.

Do not rewrite the branch history to imitate Git rebase. The semantic rebase is the preparation of a main-target Patch against current main.

### Merge receipt

After a successful main commit, append or reconcile a durable branch merge receipt containing the source branch head, resulting main revision, merge Patch/task identity, and time. Branch truth remains intact.

The receipt write and main append must be crash-recoverable and idempotent. A crash after main commit must not cause the same branch head to be merged twice when reconciliation resumes.

The merge task is human-dispatched operational work and does not reopen or spend the concluded Auto-research invocation ceiling.

## Minimal UI

Use the existing Auto-research episode detail in Runs. Add only what is needed to make the native workflow operable:

- a compact branch identity/state on the episode;
- base and head information sufficient for audit, without a branch graph viewer;
- merge state such as unmerged, merge running, merged through a named head, or merge failed;
- a `Merge to main` action only when the branch is eligible and has unmerged changes; and
- the merge task in the ordinary task list/detail so its provider output, correction, pause, failure, retry, and success are visible through existing surfaces.

Clicking the action dispatches the merge agent. Do not add a conflict viewer, branch switcher, manual cherry-pick UI, discard button, or repository-branch UI.

Main project graph views continue to show main only.

## API and projection contract

Episode responses must expose a strict branch summary rather than making the browser reconstruct branch state from tasks. Include branch id, base, head, merge eligibility, latest successful merge head/main revision, and active merge task where applicable.

Every branch endpoint must verify the branch belongs to the requested project and episode. Do not permit cross-project lookup by branch id.

The merge endpoint requires the same named human/project membership checks as other consequential dispatches and must reject an active/non-quiescent branch, a head already merged, or a concurrent merge task.

## Non-goals

Do not implement:

- Git branches or repository worktrees;
- repository rollback or merge;
- branch discard or deletion;
- a general user-created branch system;
- arbitrary branch-to-branch merges;
- a branch graph viewer or conflict editor;
- orchestrator self-merge;
- direct raw-history concatenation; or
- automatic merge on episode completion.

## Important seams

Shared branch contracts should land serially before consumers. Likely seams include:

- `src/rcp/core/models.py` and typed Patch provenance;
- new branch metadata/history models;
- `src/rcp/history/manager.py` and history delta/materialization;
- `src/rcp/transport/state.py` and remote state workspaces;
- `src/rcp/storage/models.py`, episode/Auto-research storage, and reconciliation;
- `src/rcp/background.py` task creation and recovery;
- `src/rcp/runs/auto_research*.py`;
- child Work/Experiment-loop graph binding and watchers;
- a dedicated merge run/prompt module;
- `src/rcp/api/episodes.py` and API composition;
- `web/src/types.ts`, `web/src/campaigns.ts`, `web/src/components/CampaignRuns.tsx`, episode hooks/client calls, and focused styles/tests.

Do not let separate workers independently invent branch identity, revision references, or merge provenance.

## Acceptance contract

This is a new cross-module user journey and merits one active acceptance scenario, rather than one scenario per internal branch behavior. Create the next available scenario with the following promise:

> An Auto-research episode starts from one coherent main revision and writes every graph change to its persistent episode branch while main remains independently editable. After the episode completes or pauses for a human, the Runs detail can dispatch an orchestrator-authority merge agent. The agent rebases the branch's semantic delta onto current main, resolves conflicts through its normal correction loop, and commits one attributable main Patch or nothing. The branch remains after merge, repository files are not branched, and main never shows branch graph changes before a successful merge.

The scenario should cover one clean merge and one main/branch conflict handled without a conflict viewer. Reference existing authority, episode, and revision-coherence scenarios rather than restating them.

## Verification

At minimum prove:

1. Episode creation pins one base main revision and launches no provider before branch binding exists.
2. Root, continuation, child Work, child Experiment, watcher, correction, settlement, and report paths all read and write the branch.
3. A branch Patch leaves main graph/revision/control unchanged.
4. Main may advance repeatedly while the branch remains stable.
5. Existing-belief Proposal restrictions and direct Decision authority are unchanged on branch and merge.
6. Branch watchers do not wake main tasks; a successful merge does wake eligible main watchers once.
7. Merge is refused while a branch-writing task is active and allowed after durable completion or human pause.
8. A clean merge commits one main revision with complete branch provenance.
9. A deterministic conflict reaches the merge agent, correction succeeds without partial append, and no conflict viewer is required.
10. A moving main head causes re-preparation/retry, never overwrite.
11. Crash recovery deduplicates a main commit whose branch receipt was not yet reconciled.
12. The branch remains replayable and visible after merge; no discard route exists.
13. Remote canonical state supports branch creation, replay, append, merge, and recovery.
14. Served-browser verification shows only the compact branch/merge controls and ordinary merge-task detail.

## Completion

Update the Auto-research, graph/history, authority, episode, watcher, API, and Runs specifications. Archive this handoff only after the branch and merge paths pass focused, full-suite, remote-state, and served-browser verification.
