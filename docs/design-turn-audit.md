# Design: the task inspector becomes a turn audit

Extend the task inspector from "every turn in a list" to "one turn's audit".
The agent panel already shows each agent's conversation and live state. The
inspector keeps the audit no other surface shows, plus the task controls it
already offers.

## Problem

The inspector lists one row per turn ("Node conversation · attempt 1").
With one id per agent since 0.4.49, that list repeats the agent panel.
It also mislabels turns, and on a phone it hides the turn being read.

## What the inspector shows

One turn and its attempts:

- the hero: state, error or status message, degradation, action retry
- Task contract: provider, runtime, execution, truth scope, node, guidance,
  native session, task id, what it continues, start time
- Exact prompts, staged task contracts, and the event history
- Pause, Resume, and Retry, unchanged

It has no left list. The header carries the turn's owner as an eyebrow and the
turn as its title: the first line of the first attempt's message, else the kind
label. Attempts are chips under the title, newest first, shown only when there
are two or more. A chip selects its attempt through the existing `onSelect`.
The inspector opens on the attempt it was asked for.

An attempt continues its parent when `parent_operation_id` names a task of the
same kind whose `attempt` is one less. Other parent links (orchestrator wakes,
Experiment kickoffs, chain continuations) are not attempts.

## Where it opens

- **Agent panel.** Each turn's last line that is not the member's (answer,
  deliverables, error, paused line) gets a Turn details icon in the answer's
  annotation row. It shows whenever the view has `onInspectTask` and the line's
  task is loaded, with or without Copy link. Hover names it. In the Agents
  workspace the icon appears with the line's time: on hover, on keyboard focus,
  or on tap at phone width.
- **Project history.** Its Agent tasks column lists owners, not turns.
  An agent row opens that conversation through the chat link, which reroutes
  to the conversation's graph. Any other row opens the audit of its newest turn.
- Every other existing `onInspectTask` caller (Experiment detail, episode
  timeline, consolidation inbox, node detail, Graph updated receipt) opens the
  audit unchanged.

## Owners in Project history

`taskOwnerRows(tasks, chatTitles)` in `web/src/agents/agentTasks.ts` groups
the project task list. `chatTitles` maps each conversation the client lists to
its title. The first matching rule wins:

1. **Agent.** `chatIdForTask` names a listed conversation. Whatever Agents
   lists opens there, including an Auto-research orchestrator across its chain.
2. **Episode.** The task has `episode_id`. One row per episode, labelled
   Auto-research when an orchestrator turn is in it, else Experiment.
3. **Unlisted chat.** One row per chat id, labelled by kind, opening the audit.
4. **Single run.** One row per attempt lineage, labelled by kind.

Rows sort by their newest turn's `updated_at`, then creation. Each row shows a
state dot, the newest turn's status label and time, and its degradation note.
A kind icon tells agents from runs; there are no group headers. At phone width
the column's eyebrow hides and the rows scroll sideways.

## Non-goals

- No server change. Names come from the conversations the client already loads.
- Space-thread turns stay out of the project task list.
- No new controls.

## Checks

- Web unit tests: owner rules and order, attempt lineage, turn title, turn end.
- Served-app drive: the audit from a failed turn, attempt chips, Project history
  rows opening an agent, desktop and 390px.
