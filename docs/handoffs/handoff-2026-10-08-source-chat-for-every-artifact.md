# Source chat for every artifact

Date: 2026-10-08
Status: design agreed with the human on 2026-10-08, revised after an astra
xhigh design review the same day. Slices 1 to 3 and the Runs folding are
implemented on branch `source-chat`, with specs updated.

Implemented: session-rule defects, Agents inventory, tags, filter, read-only
children, folded Archived column, readable refusals, one source link, viewer
button, Open in Agents, folded Runs cards. The conversation kind is named
`episode`, not `experiment`, to keep node types out of the kernel.
Remaining: the served-app journeys in slice 4, and the pre-push review.

Settled with the human on 2026-10-08:

- Every conversation, on main or any graph branch, is listed in Agents and
  tagged by graph and kind.
- Every artifact links to its conversation through one code path: its chat,
  else its Runs card, else no link.
- Edits stay as they are. Work that a comment cannot do goes through the
  source chat's ordinary composer.
- Auto-research child chats are listed and linked but read-only. Their chat
  shows one line pointing to the orchestrator. Humans keep messaging the
  orchestrator by mail in Runs.
- Agents' Archived column and Runs cards start folded. Agents gets a graph
  filter beside its search box.

## What the code already does

- Node and project chats, Experiment episodes, Auto-research child
  Experiments, and Auto-research child Work write canonical chat files stamped
  with their graph target (`_append_chat_records`, `src/rcp/runs/chat.py`).
  The orchestrator writes none.
- All chat files share one directory. The retained project service parses them
  in one cached scan; `chat_summaries` then keeps only the viewed target.
  Branch services are rebuilt per request with empty caches, so the
  cross-target inventory must use the retained main service.
- `#/projects/P?view=chats&chat=C&branch_id=B` switches the workspace to B,
  waits for that target, and opens C with a working composer. Storage refuses
  cross-target conversation and session reuse.
- A human post into an Experiment's chat with no live turn is admitted as an
  ordinary human turn on the same native session, unless the provider or
  machine changed or the binding is unusable, which starts fresh. It spends no
  episode budget. A live ordinary turn can be steered; an episode turn cannot.
- The only read-only Experiment chat is the Runs view of a branch Experiment.

## Design

1. **Session rules first.** Fix the three defects below before more sessions
   become reachable.
2. **Agents inventory across targets.** A project-wide inventory, separate
   from exact-target conversation selection, carries each conversation's graph
   target and kind (chat, Experiment, Auto-research child) through the web
   model, with project-wide task, activity, and unread state. Opening a
   conversation from another target switches the workspace first, through the
   existing deep link; node "Open chat" keeps selecting only within the
   viewed target. Task-only conversations without a transcript still appear.
   The filter applies across pages, not just the first 200 summaries.
   Delete the Experiment-index branch rows: `BranchEpisodeAgentRow`,
   `branchEpisodeAgentRows`, `agentGroupItems`, and their board rendering.
3. **Auto-research children are read-only.** Their chat shows the transcript
   and one line pointing to the orchestrator; the composer is not mounted, and
   the backend refuses a human turn on a child's chat. Child budgets, Stop
   fences, wakes, Finish, and recovery are unchanged.
4. **One validated link helper.** One backend function resolves the
   destination for every artifact inventory path, including stored artifacts
   that today get no link, and for the viewer state. It keeps today's checks:
   the transcript exists on its exact target, edit ancestry, project, target,
   and episode. Transcript lookup stays batched. `artifact_reply_origin` stays
   for edit admission. The Runs embedded transcript stays read-only and gains
   **Open in Agents**. The viewer icon becomes a labelled **Source chat**
   button, and the comment box points to the source chat for analysis or code.
5. **Folding and filter.** The Archived column starts folded. Runs stops
   expanding its first card by default; a card opened by a deep link still
   expands. The Agents filter offers all, main, or one branch.

## Session-rule defects (slice 1)

Each already happens on main today.

1. A human turn restores its old chat baseline by contract key alone, so after
   an intervening Experiment turn it is told the chat contract still holds
   (`_prepare_chat_prompt_state`, `src/rcp/runs/chat.py`). Recovery restores
   the same baseline. Fix: when the session's latest delivered master is not
   the baseline's, re-open the chat master with `replaces`, on fresh turns,
   Resume, Retry, and recovery, including a first human turn with no
   baseline.
2. Experiment graph repair without a renderer takes the session's latest
   master whatever its owner and stages it under the Experiment label
   (`experiment_loop.py`). Fix: recover the Experiment's own master and values
   from its lineage, and re-open it when another owner intervened.
3. After a report or artifact edit, a human turn ignores report revocation and
   may receive the unchanged-master pointer while report-only instructions
   still hold; its success then clears `episode_report_rebootstrap_pending`
   (`src/rcp/storage/episodes.py`). Fix: the first human turn after revocation
   re-opens its master; revocation clears only after a successful re-open.

Tests drive each transition through fresh, Resume, and Retry, on local and
remote stages.

## After a merge

Merge lands the branch's graph changes, and its code for code-isolated
episodes. Chats, sessions, artifacts, and reports stay on the branch, which is
never deleted; graph-branch archive only hides its episodes in Runs.

- Still works: reading the branch's chats, opening artifacts and reports, and
  comment edits.
- During a merge: new turns, Discuss included, are refused with
  `episode_merge_reserved`.
- Graph-only branch after the merge: new turns and Add N turns keep working.
- Branch whose code worktree cleanup removed: every new turn and Add N turns
  are refused with `episode_isolation_unavailable`.

The composer shows both refusals as readable reasons. Because archive hides
the episode in Runs but not its chat, the chat is the stable link.

Not in scope: artifact edits skip the merge fence during an active merge.

## Slices

1. Session-rule defects 1 to 3 with transition tests. About 1.5 days.
2. Agents inventory, tags, filter, read-only children, deep-link opening;
   delete the branch rows. About 1.5 days.
3. Link helper, viewer button, comment-box line, Open in Agents, readable
   refusals; delete `_episode_runs_query`. About 1 day.
4. Folding, spec updates, and served-app journeys: two members, a
   live-child chat, a reserved merge, an archived branch, a removed worktree,
   remote recovery. About 1 day.

Specs to update: `design.md` (episode sessions reachable from Agents),
`api-web-and-desktop-projections.md` (Agents),
`paper-artifacts-and-result-views.md` (source link and comments),
`conversations-episodes-and-watchers.md` (human turns in Experiment sessions),
`auto-research-and-branch-merge.md` (read-only child chats).
