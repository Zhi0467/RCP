---
id: S138-edit-and-discuss-an-episode-graph
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_branch_graph_workspace_api.py
  - tests/test_branch_chats.py
  - tests/test_branch_target_storage.py
  - tests/test_branch_read_resolution.py
  - tests/test_branch_merge.py
  - tests/test_branch_merge_proposals.py
  - web/tests/branchGraph.test.mjs
  - web/tests/branchGraph.browser.test.mjs
last_passed: >-
  2026-09-07 — full backend and Web suites, production build, Ruff, and pre-commit
  passed. An isolated served-browser drive verified manual Sync, Discuss/Work,
  branch navigation, draft isolation, and clean console/network/server logs
  using a deterministic provider. Merge API regressions covered repeat main
  Inbox review, three successive value changes, retries, and writer fences.
invariants: [1, 3, 3b, 4, 4b, 5, 6, 6b, 7b, 10b, 10c, 10d, 10g]
reported_by: human-confirmed graph workspace and repeat main Inbox review, 2026-09-07
---

# Inspect, edit, and discuss Auto-research in its own graph

An episode opens the ordinary research graph interface on its persistent branch.
The initial changes view shows what the episode changed and nearby context.
Humans use the same node/relationship edits, Inbox, and node/project Discuss and
Work controls as main. There is no special central review conversation and no
reuse of the orchestrator's native session for ordinary chat.

The human confirmed that editing and ordinary chats remain available while the
episode runs and after it settles. They remain separately authorized work;
opening a chat neither spends the episode budget nor restarts an ended episode.
Merge waits for branch writers to settle and fences new writes while running.
These are graph branches; ordinary repository work retains its real effects.

## Drive

1. Open an Auto-research episode in Runs and choose Open graph. Inspect added,
   changed, and removed nodes and relationships, before/after content, and the
   task/Patch provenance. Expand surrounding context without leaving the branch.
2. Edit nodes and relationships with the ordinary controls and Sync. Reload,
   switch to main, and return: the edit persists only on the branch. Main and
   branch retain separate drafts even with identical node ids and revisions.
3. Ask about a branch node with Discuss, then use Work in that conversation.
   Discuss changes nothing; Work writes only to the branch with ordinary agent
   authority. Project chat works in the same way. Existing main chats remain
   separate. Pause, resume, repair, and watcher continuation retain the target.
4. Review a branch Proposal through the ordinary Inbox. Its human judgment
   remains attributable in the append-only branch history.
5. Merge a settled branch with legal direct changes and protected human edits.
   One validated main transition applies direct changes and creates pending
   main Proposals for protected changes. Review those in main's Inbox. Retry
   cannot duplicate either the merge or its main Proposals.
6. Keep a main request and a branch draft open across a target switch. A delayed
   snapshot, preview, or Sync response cannot replace the other target's state.
   Missing or foreign branches fail explicitly without restoring a main chat.
7. Confirm later human edits are distinguishable from the original episode's
   work. Existing reports remain immutable records of their ending.

## Verification boundary

Exercise this path on an isolated served app and inspect console, network, and
server errors. Local deterministic coverage does not claim remote/provider
qualification. Never write to the human's real data directory from this drive.
