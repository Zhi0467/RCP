# Experiment loops are per graph branch

Date: 2026-10-06
Status: design settled with the human on 2026-10-06 in a grilling session and
reviewed once by an xhigh design pass the same day; its corrections are folded
in below and raised no human questions. Implemented on 2026-10-06 in this PR:
all four slices landed, each reviewed once with its fixes applied, and focused
Python and Web suites pass, including the broker-socket and browser cases the
Codex sandbox blocks. Overlap rows are compact and capped (`{rows, omitted}`,
`LOOP_OVERLAP_MAX_ROWS` and `LOOP_OVERLAP_MAX_BYTES` in `limits.py`). Still
open: the close criteria below on a served app with real providers, and a
migration rehearsal on a copy of real team-server data.

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
| 4 | The replaced loop's chat is never told | no per-turn loop status; resource discovery is already exact-target but omits stopped loops by design, so the chat keeps reading an old staged file |
| 5 | The chat watcher strip matches by node only | `visibleChatWatchers` (`web/src/experiments/runProjection.ts`) |
| 6 | Run cards show no branch, starter, or checkout | `ExperimentRunDetail.tsx`, `SpaceRuns.tsx` |
| 7 | A target's view hides its own loop once a newer loop lives elsewhere | the "globally newest" runtime, batch, and snapshot projections (`storage/experiments.py`), their node-keyed consumers (`projects.py`, `api/index.py`, `api/sync.py`), and the newest-root recovery and repair queries |

## Settled design

### Identity and authority

1. **A loop belongs to project + node + graph target.** At most one live loop per
   node per target. The unique index, admission, and every runtime lookup key on
   the target. One lookup replaces the "globally newest" and "for target" pair.
2. **The orchestrator keeps its existing authority, limited to its branch and
   its own children.** It cannot stop or adopt any loop it did not start, even a
   human loop on its own branch. It reads other branches and asks the human
   (`ask`). There is no new verb and no approve-then-RCP-acts path. The
   replacement path is removed: a kickoff never stops, adopts, or waits on
   another loop. To restart its own child it stops that child, waits for
   settlement, and kicks off again; recovery advice says so.
3. **Overlap is information, never a gate.** A start on a node that has a live
   loop on another target succeeds, for humans and the orchestrator alike. The
   start result and the human Run dialog list the other live loops. A human
   isolated start checks readiness against the branch it will create, so a live
   main loop no longer blocks it.
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
   state, and checkout. Checkout is `shared` or `worktree` plus execution host
   and repository path, so "same checkout" can be judged. The kickoff result,
   including a recovered kickoff, carries the same rows for its node as
   `live_elsewhere`.
7. **Node chats get a small loop-status block on every turn**, rendered from the
   same projection the run card uses: this target's loop (live, stopped, or
   completed; who started it; when and by whom it stopped), live loops on other
   targets for this node, and a watcher-state file refreshed for this target.
   It includes an explicit no-loop state, refreshes on resumed sessions and
   watcher-driven turns, and grants no watcher-maintenance authority: stopped
   loops stay out of the maintenance resources.
8. **Branch agents get read pointers only.** The prompt names the shared
   checkout path, says other code branches are readable through Git, and names
   main's `graph.json`, staged as an immutable task input with execution-host
   paths. No other branch's graph is staged. Write scopes, the graph target, and
   Patch collection do not change; the shared checkout is not added as a root.

### Web

9. **One card per loop, in a flat list.** Each card shows a branch badge,
   who started it (a member, or Auto-research with a link to the parent run),
   and its checkout. Selection, Stop, and busy state use the exact episode, not
   the node. The authorizing member stays recorded separately from the
   starting Auto-research run and from the isolation owner.
10. **The chat watcher strip shows only this chat's watchers and its own
    target's loop watchers.** Cancel can no longer reach another branch's loop.

### Out of scope

- Code collisions between loops on different nodes, and plain Work chats
  sharing the checkout, stay as today.
- An unexplained chat panel the human saw inside a run card. It needs a
  screenshot before it can be scoped.

## Migration

- A versioned storage migration drops `episodes_one_live_experiment_control`
  and creates a unique index on `(project_id, control_node_id,
  graph_target_json)` for live `experiment_loop` rows. It also drops the
  pending-replacement unique index. `CREATE INDEX IF NOT EXISTS` would keep the
  old definition, so the drop is explicit, and a test asserts the old indexes are
  absent after fresh creation and after upgrade.
- `graph_target_json` is not canonical today: ordinary inserts and transfer
  imports serialize the same target with different key order. The migration
  rewrites existing values to one form, every writer uses one serializer, and
  raw-equality queries rely on it.
- A child route left `pending` with `replaces_episode_id` is settled in the
  migration transaction as cancelled and never launched, with a recorded
  diagnostic and its provenance kept. The orchestrator can kick off again
  normally. No provider launches during migration, and an already recorded
  predecessor Stop is never undone. Transfer export and import accept a
  cancelled never-launched route without an episode.
- `replaces_episode_id` and historical replacement notices stay as read-only
  history.
- Evidence: rehearsal and upgrade of a pre-change database, fresh-schema
  equivalence, reopen, retained live episodes, tasks, watchers and budgets,
  mixed JSON encodings, and a transfer round trip with a cancelled route.

## Implementation slices

Slice 1 owns every shared contract and lands first. Slices 2–4 then run in
parallel worktrees branched from slice 1, each owning only its files.

1. **Storage, migration, transfer, projections, APIs.** The migration and
   serializer above; a target-required runtime lookup plus an all-target
   snapshot that never collapses node identities; target-scoped admission,
   continuation, recovery, and graph-repair queries; Stop entry points that
   require a target or exact episode; one shared loop-status projection (live,
   stopped, completed, none; starter, Auto-research parent, stop actor and time,
   checkout identity); `other_branch_loops` and `live_elsewhere` row models; the
   human start response with its overlap list; the interference-rule renderer
   shared by slices 2 and 3; Episode response fields; `web/src/core/types.ts`.
   Removes the replacement path from storage and settles legacy pending routes.
   Owns `storage/*`, `transfer/records.py`, `projects.py`, `api/*` including
   `api/app.py` startup wiring, `runs/provider_login.py`, `runs/episodes/*`,
   `watchers.py`. Tests: two admitted live loops on one node across targets,
   started in either order, each recovered, stopped, and delivered a completed
   watcher group independently; same-target refusal; a human isolated start
   beside a live main loop; the migration evidence above.
2. **Auto-research.** Kickoff without replacement; `live_elsewhere` in normal
   and recovered kickoff results; `status.other_branch_loops`; recovery advice
   (stop own child, settle, kick off); orchestrator prompt rule and pointers.
   Owns `runs/auto_research*.py`, `runs/tasks/auto_research_stream.py`,
   `agents/auto_research_prompt.py`. Tests: kickoff beside a live main loop
   leaves it running; status lists it.
3. **Loop and chat prompts.** Status block for node chats on every turn kind;
   interference rule for Experiment-loop prompts; branch pointers and the staged
   main `graph.json`. Owns `agents/prompts.py`, `agents/experiment_loop_prompt.py`,
   `agents/context.py`, `runs/experiment_loop.py`, `runs/chat.py`,
   `runs/shared.py`, and `runs/tasks/*.py` except `auto_research_stream.py`.
   Tests check structure and staged files for live, stopped, completed, and
   no-loop states, never wording.
4. **Web.** Card labels, exact-episode selection and Stop, Run-dialog overlap
   list, watcher strip filtered by target and chat, WebMCP exact-target
   inspection. Owns `web/src/experiments/*`, `web/src/chat/NodeChat.tsx`,
   `web/src/App.tsx`, `web/src/graph/DetailDrawer.tsx`,
   `web/src/graph/GraphViews.tsx`, `web/src/webmcp/experiments.ts`,
   `web/src/core/api.ts`. Tests in `web/tests/`.

Test files follow their slice; slice 1 also owns
`test_auto_research_children_storage.py` and converts the replacement tests in
`test_auto_research_experiments.py` before slice 2 starts.

Docs (Claude): `auto-research-and-branch-merge.md` (the project-global rule,
replacement, recovery advice, and pending-route Finish text),
`conversations-episodes-and-watchers.md` (target-scoped continuation),
`api-web-and-desktop-projections.md` (one card per loop, "Started by"), and
this handoff's status.
