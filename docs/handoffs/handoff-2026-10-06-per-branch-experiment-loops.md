# Experiment loops are per graph branch

Date: 2026-10-06
Status: design settled with the human on 2026-10-06 in a grilling session.
Not yet implemented. Implementation runs in this PR as parallel slices after
one xhigh design review.

Close this handoff when all of these hold:

- a main loop and an Auto-research child loop run on the same Experiment node
  at the same time, and neither stops, hides, or adopts the other's watchers;
- the orchestrator's `status` and kickoff result list the other branch's live
  loop, and nothing it runs can stop that loop;
- a node chat whose loop was stopped says so on its next turn, from a fresh
  status block, never from an old watcher-state file;
- every loop card in Runs shows its branch, who started it, and its checkout;
- a chat's watcher strip lists only watchers that chat armed or its own
  branch's loop owns;
- a database from before this change upgrades with no live row lost.

Why loops became per branch: [decision](../decisions/2026-10-06-experiment-loops-are-per-branch.md).

## What went wrong

On 2026-10-06 an Auto-research run on a graph branch kicked off a child loop on
an Experiment node. A human had started a loop on the same node, on main, on
2026-10-01. RCP stopped the human's loop as a "predecessor", stopped its 8
watchers, and started the child. Nobody was told: the orchestrator's kickoff
result said `created`, its replacement notice was `self_caused`, and the human's
node chat kept believing its loop and plan were live.

Root cause: a loop's identity is *project + node*. The branch is a field on the
episode, and each reader filters by it differently.

| # | Defect | Owner today |
|---|---|---|
| 1 | One live loop per node, across all branches | index `episodes_one_live_experiment_control` (`storage/base.py`), `_live_predecessor_id` (`runs/auto_research_experiments.py`), spec |
| 2 | The orchestrator cannot see loops on other branches | `status` lists only its own children |
| 3 | The kickoff hides the replacement | `kick_off` replacement path, `wake_suppressed=self_caused` |
| 4 | The replaced loop's chat is never told | no per-turn loop status; the node-attached resource drops the node once a newer loop lives on another branch, so the chat reads a stale staged file |
| 5 | The chat watcher strip matches by node only | `visibleChatWatchers` (`web/src/experiments/runProjection.ts`) |
| 6 | Run cards show no branch, starter, or checkout | `ExperimentRunDetail.tsx`, `SpaceRuns.tsx` |
| 7 | A branch's view hides its own loop once a newer loop lives elsewhere | `experiment_loop_runtime` "globally newest" display contract and `experiment_watcher_resources` (`storage/experiments.py`) |

## Settled design

### Identity and authority

1. **A loop belongs to project + node + graph target.** At most one live loop per
   node per target. The unique index, admission, and every runtime lookup key on
   the target. One lookup replaces the "globally newest" and "for target" pair.
2. **The orchestrator has full authority on its own branch and none elsewhere.**
   It reads other branches and asks the human (`ask`). There is no new verb and
   no approve-then-RCP-acts path. The replacement path is removed: a kickoff
   never stops, adopts, or waits on another target's loop.
3. **Overlap is information, never a gate.** A start on a node that has a live
   loop on another target succeeds, for humans and the orchestrator alike. The
   start result and the human Run dialog list the other live loops.
4. **Code isolation is unchanged.** Worktrees stay optional as today, and
   Auto-research resolves its code choice as today.

### What agents see

5. **One interference rule for every loop agent.** Main loops, branch children,
   and the orchestrator receive the same line next to the same list: ask the
   human if your work could interfere with episodes on another branch, for
   example the same node and the same checkout. It is prompt guidance, not
   enforcement.
6. **Orchestrator `status` gains `other_branch_loops`**, one compact row per live
   loop off its branch across the project: node, episode, target, started by,
   state, checkout (`shared` or `worktree`). The kickoff result carries the same
   rows for its node as `live_elsewhere`.
7. **Node chats get a small loop-status block on every turn**, rendered from the
   same projection the run card uses: this target's loop (live, stopped, or
   completed; who started it; when and by whom it stopped), live loops on other
   targets for this node, and a watcher-state file refreshed for this target.
   It replaces nothing the human sees; the run cards already show it.
8. **Branch agents get read pointers only.** The prompt names the shared
   checkout path, says other code branches are readable through Git, and names
   main's `graph.json`, staged read-only beside the branch graph. No other
   branch's graph is staged.

### Web

9. **One card per loop, in a flat list.** Each card shows a branch badge,
   who started it (a member, or Auto-research with a link to the parent run),
   and its checkout.
10. **The chat watcher strip shows only this chat's watchers and its own
    target's loop watchers.** Cancel can no longer reach another branch's loop.

### Out of scope

- Code collisions between loops on different nodes, and plain Work chats
  sharing the checkout, stay as today.
- An unexplained chat panel the human saw inside a run card. It needs a
  screenshot before it can be scoped.

## Migration

- A new storage migration replaces `episodes_one_live_experiment_control` with
  a unique index on `(project_id, control_node_id, graph_target_json)` for live
  `experiment_loop` rows. Old rows satisfy the wider key, so the rebuild cannot
  fail on existing data. `graph_target_json` is written in one canonical form;
  the slice verifies that before relying on it.
- A child route left in `pending` with `replaces_episode_id` was waiting for a
  predecessor that the new rule no longer stops. On upgrade it starts through
  ordinary per-target admission, or fails with a recorded diagnostic. It never
  waits forever.
- `replaces_episode_id` stays as a read-only historical column. Transfer
  records keep carrying it.

## Implementation slices

Slice 1 is the shared contract and lands first. Slices 2–4 then run in
parallel worktrees branched from slice 1, each owning only its files.

1. **Storage, admission, API projection.** The migration; one target-keyed
   runtime lookup and its callers (`api/experiment_controls.py`,
   `api/experiments.py`, `watchers.py`, `runs/provider_login.py`);
   `experiment_watcher_resources` per target; the `other_branch_loops` row
   model; Episode response fields for starter and checkout; matching
   `web/src/core/types.ts`. Tests: two live loops on one node across targets;
   same-target refusal; upgrade of a pre-change database.
2. **Auto-research.** Remove the replacement path and its pending advance;
   kickoff `live_elsewhere`; `status.other_branch_loops`; orchestrator prompt
   rule and pointers. Owns `runs/auto_research*.py`,
   `storage/auto_research_children.py`, `agents/auto_research_prompt.py`.
   Tests: kickoff beside a live main loop leaves it running; status lists it.
3. **Loop and chat prompts.** Status block for node chats; interference rule
   for Experiment-loop prompts; branch pointers and the staged main
   `graph.json`. Owns `agents/prompts.py`, `agents/experiment_loop_prompt.py`,
   `agents/context.py`, `runs/experiment_loop.py`, `runs/tasks/*.py` staging.
   Tests check structure and staged files, never wording.
4. **Web.** Card labels, Run-dialog overlap list, watcher strip filter. Owns
   `web/src/experiments/*`, `web/src/chat/NodeChat.tsx`. Tests in
   `web/tests/`.

Docs (Claude): `auto-research-and-branch-merge.md` (the project-global rule and
replacement text), `conversations-episodes-and-watchers.md`,
`api-web-and-desktop-projections.md`, and this handoff's status.
