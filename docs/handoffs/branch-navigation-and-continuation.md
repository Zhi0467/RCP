# Unified graph refs and branch continuation

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
3. **Main and branches are different kinds of thing.** On the backend a branch
   exists only as an episode's attachment (`GraphBranchMetadata` requires
   `branch_id == episode_id`), branches are discovered through episodes, and
   main is the absence of `?branch_id=`. In the browser the loaded unit is the
   pair (project, graph): a branch switch takes the same remember-and-reopen
   path as a project switch, URLs encode main as a missing parameter, and links
   are built per case. Symptoms: the banner speaks of episodes, **Main graph**
   always lands on Research, and branch-only UI hangs off the owning episode.
4. **Branch work has no human fallback.** Auto-research child chats are
   read-only (`auto_research_child_read_only`), and the orchestrator accepts
   human mail only while the episode runs. After an episode ends, a child's
   artifact links to a Source chat nobody can write to. On main the fallback
   is "open the source chat, send a Work turn"; branches need the same.

## Settled decisions

- One PR for all four. Items 1 and 2 are not branch issues; they ride along.
- (1) Keep the rendered project through a reverification that confirms the
  same backend (same `instance_id` and space). Reconcile it with an ordinary
  reload, not a cold open. A changed backend still clears the project.
- (2) A reload keeps the project route. Desktop keeps its current behaviour
  unless a reason for it turns up while implementing (report it, do not guess).
- (3) Unify graph refs; keep episode ownership. Main is one ref among them,
  with its own rules made explicit rather than implied by absence:
  - One project API lists every graph ref: main plus each branch, with head,
    base, merge state, and an optional owner episode. Branch summaries come
    from this list, not from episodes. Branches are still created only by
    episodes; `branch_id == episode_id` stays.
  - In the browser the graph ref is a cursor inside the open project session.
    Switching it keeps the view and selection, reuses per-ref caches inside
    the session, and never takes the project open path. One helper builds
    every project URL from (project, ref, view).
  - The banner becomes a graph picker fed by that list. **Episode & tasks**
    stays as a compact link when the ref has an owner episode. A view with no
    meaning on the target falls back to Overview.
  - Main-only rules stay explicit and owned where they are today: merges go
    into main, pairwise; Proposals and protected authority are unchanged.
  - Out of scope: branches without an episode, which would drop
    `branch_id == episode_id` and need a record migration.
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

- Python tests for the graph-ref list (main present, every branch present,
  merge state carried, no episode read required by the client).
- Web unit tests for the URL helper, the picker's ref list, and the view kept
  across a switch without a project reopen; a test that a same-backend reverification keeps the project mounted.
- `tests/` admission tests: child human turn refused while running, allowed
  after ending, refused again under a running continuation; continuation
  refused while a child human turn runs.
- Served-app drive: branch picker switch from Agents on a throwaway server
  (network shows no project reopen);
  forced reverification while a project is open (no "Opening project").
- Spec updates: `auto-research-and-branch-merge.md` (child chat rule),
  `api-web-and-desktop-projections.md` (reload route, graph refs, picker),
  `graph-history-and-transitions.md` if the ref list changes its contract.
- Live check left open: phone reload on a real device in a team space.
