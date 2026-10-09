# Branch navigation and branch continuation

Status: design, not started. One PR. Nothing is implemented yet.

## Problems

1. **Agents click reopens the project.** In a desktop team space, a transient
   backend check failure (for example the team tunnel dropping after idle) sets
   `identityIssue`. On recovery, `useActorIdentity` reruns its identity read,
   which flips `actorIdentityChecked` false then true. `backendSessionReady`
   flickers, and the project-open effect in `App.tsx` reruns. The active
   project is not in the tab cache (only projects you leave are), so it takes
   the cold path and shows "Opening project" for seconds.
2. **Phone lands on the space page.** `initialProjectHash` drops the route on
   every `reload` navigation. Phone browsers reload a discarded background tab
   on return, so the member loses their project.
3. **Branch banner is not a picker.** It shows "Episode branch · id · Revision
   · merge state" with **Episode & tasks** and **Main graph**. **Main graph**
   links to the Research view, so leaving a branch always lands there.
4. **Branch work has no human fallback.** Auto-research child chats are
   read-only (`auto_research_child_read_only`), and the orchestrator accepts
   human mail only while the episode runs. After an episode ends, a child's
   artifact links to a Source chat nobody can write to. On main the fallback
   is "open the source chat, send a Work turn"; branches need the same.

## Settled decisions

- One PR for all four.
- (1) Keep the rendered project through a reverification that confirms the
  same backend (same `instance_id` and space). Reconcile it with an ordinary
  reload, not a cold open. A changed backend still clears the project.
- (2) A reload keeps the project route. Desktop keeps its current behaviour
  unless a reason for it turns up while implementing (report it, do not guess).
- (3) The banner becomes a graph picker: main plus every branch the Agents
  inventory already lists. Switching keeps the current view (Agents stays
  Agents). **Episode & tasks** stays as a compact link beside the picker.
  A view that has no meaning on the target falls back to Overview.
- (4) Child chats unlock once their episode has ended. While it runs, the
  child composer is replaced by a pointer to **Message orchestrator**.

## Proposed fence for (4)

An ended Auto-research episode can still get an **Add N turns** continuation
that resumes the orchestrator, which may continue child sessions. So:

- Admission allows a human turn on a child only when the child's episode
  lineage has no running continuation.
- Admission of an Add N turns continuation refuses while a human turn is
  active on any of that episode's children, naming the chat.
- A running continuation re-locks the children; the composer says why.
- Artifact-comment edits on a child's output stay allowed, as today.

The invariant that a human turn and an orchestrator continuation never share a
child session at the same time is owned by `agent_tasks` admission and gets a
test that attempts the violation.

## Checks

- Web unit tests for the picker's target list and the view kept across a
  switch; a test that a same-backend reverification keeps the project mounted.
- `tests/` admission tests: child human turn refused while running, allowed
  after ending, refused again under a running continuation; continuation
  refused while a child human turn runs.
- Served-app drive: branch picker switch from Agents on a throwaway server;
  forced reverification while a project is open (no "Opening project").
- Spec updates: `auto-research-and-branch-merge.md` (child chat rule),
  `api-web-and-desktop-projections.md` (reload route, banner).
- Live check left open: phone reload on a real device in a team space.
