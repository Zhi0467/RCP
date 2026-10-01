# Agents list: branch episodes and episode chats

Date: 2026-10-01
Status: design written; not implemented.

Implemented: nothing yet.
Remaining: both fixes below, their tests, and the served-app check.

Settled with the human on 2026-10-01:

- While main is viewed, an episode running on a graph branch is listed with
  its real status and opens read-only on main. Viewing it does not switch the
  viewed graph.
- Every human Run creates a new chat id, so each episode has its own row and
  transcript. It never joins an existing human chat on the node.

## Evidence

On a team server, an Experiment started with "Work on a graph branch" at
00:34:22 UTC. It finished at 00:38. A second Experiment started on main in an
existing node chat at 00:34:39 and ran until 04:21. While both ran:

- The branch episode's row sat under Done with no time. Opening it showed an
  empty chat and the error "This conversation belongs to another graph target
  and cannot continue here."
- The main episode's row correctly said Working. Its chat showed only the
  previous, finished human Work turn. Nothing showed that a turn was running.

## Cause

1. `App.tsx` keeps only tasks on the viewed graph target (`tasks` memo). A
   branch episode's tasks never reach `groupChatConversations`. The chat id
   the Run created stays as a local draft, and `conversationAgentStatus` files
   a draft under Done.
2. The chat summary page is scoped to the viewed target. After a reload, the
   branch chat is gone from main.
3. Selecting the draft runs the worktree check against main. The server
   refuses because the chat's runs are on the branch.
4. `startExperiment` takes its chat id from `ensureConversation`, which
   returns the node's newest chat. The Run already starts a fresh native
   session, but the Agents list groups by chat id, so the episode was filed
   in the human's finished chat and drawn into its transcript.

## Fix 1: branch episode rows on main

- The Agents list groups conversations from every project task, not only the
  viewed target's. A conversation whose tasks are all on another target is
  marked with that target. Its status comes from those tasks.
- A marked row shows a "Branch" tag. Opening it loads the transcript with
  that row's target (`loadChatTranscript` already takes one) and renders
  `NodeChat` with `readOnly`. No worktree check runs, and the composer is not
  offered.
- A chat with tasks on two targets cannot exist (the server refuses that
  admission), so a row has exactly one target.
- The local draft the Run created is dropped once a task names its chat id.
- Viewing the branch itself is unchanged.

## Fix 2: one chat per episode

- `startExperiment` always calls `startConversation` for a new chat id
  instead of `ensureConversation`. No server change: the Run already
  requires a client chat id, and watcher wakes and continuations use the
  chat id the episode recorded.
- Opening a node's chat still picks the node's newest conversation, which
  may now be an episode chat. That matches today's behavior for a node whose
  first chat was an episode.
- Out of scope: a running watcher wake in a human chat shows no line until
  it answers. That is a separate display gap.

## Checks

- Web unit tests: grouping with a branch-target task gives a Working row
  marked with that target; a Run on a node that already has a chat sends a
  chat id different from it.
- Served app on disposable data with a seeded branch episode: the row shows
  Working, then Done; opening it shows the branch transcript read-only, with
  no worktree error in the console or network log.
