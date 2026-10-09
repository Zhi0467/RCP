# Unified graph refs and branch continuation

Status: implementing in three slices on this PR. A design review was folded
in, and the three product questions are answered (see the end).

## Problems

1. **A project reopens after a backend check recovers.** In a desktop team
   space, a transient backend check failure (for example the team tunnel
   dropping after idle) sets `identityIssue`. While it is set, `App.tsx`
   replaces the whole rendered tree with the reconnect screen. On recovery,
   `useActorIdentity` reruns its identity read, which flips
   `actorIdentityChecked` false then true, and the project-open effect in
   `App.tsx` reruns because both values are in its dependencies. If the open
   project has no tab-cache entry, it takes the cold path and shows "Opening
   project" for seconds. Restored tabs do have a cache entry.
   Not proven: that a plain **Agents** click causes this. Healthy Agents
   navigation changes only selection and view; no rerun of project open was
   found from it. Selecting a chat on another graph does rerun it, by design
   today (this goes away with item 3). Reproduce both paths before fixing.
2. **Phone lands on the space page.** `initialProjectHash` drops the route on
   every `reload` navigation. Phone browsers may reload a discarded
   background tab on return, so the member loses their project. Also,
   `settleTeamSignIn` clears the route after a successful team sign-in. The
   reload rule was deliberate: the S107 open-project-tabs acceptance note
   (commit `1bffcaff`) says a reload or relaunch starts on the index with an
   empty dock, and `web/tests/projectTabs.test.mjs` holds it.
3. **Main and branches are different kinds of thing.** On the backend a branch
   exists only as an episode's attachment (`GraphBranchMetadata` requires
   `branch_id == episode_id`), branches are discovered through episodes, and
   main is the absence of `?branch_id=`. Explicit refs already exist in
   `transition_models.py`; the coupling is mostly in the browser. There the
   loaded unit is the pair (project, graph): a branch switch takes the same
   remember-and-reopen path as a project switch, URLs encode main as a missing
   parameter, and links are built per case. Symptoms: the banner speaks of
   episodes, **Main graph** always lands on Research, and branch-only UI hangs
   off the owning episode.
4. **Branch work has no human fallback.** Auto-research child chats are
   read-only (`auto_research_child_read_only` in `agent_tasks` admission), and
   the orchestrator accepts human mail only while the episode runs. After an
   episode ends, a child's artifact links to a Source chat nobody can write
   to. On main the fallback is "open the source chat, send a Work turn";
   branches need the same.

## Settled decisions

- One PR for all four. Items 1 and 2 are not branch issues; they ride along.
  Build it as three reviewable slices (commits) inside that PR:
  reverification and reload route; graph refs and picker; child unlock and
  orchestrator continuation.
- (1) Keep the project through a reverification that confirms the same
  backend. "Same" means every identity check `desktopRuntime.ts` already
  makes, including version and data-directory identity, plus the space.
  Reconcile with an ordinary reload, not a cold open. A changed backend still
  clears the project. Keeping state is not enough: the render gate that swaps
  in the reconnect or checking screen must also stop unmounting the project
  for a reverification (see answered question C).
- (2) A browser reload keeps the project route. Desktop keeps the S107 rule
  (reload and relaunch start on the index). A successful team sign-in keeps a
  pending route too. Restoring a route never skips identity admission; the
  project open still checks access.
- (3) Unify graph refs; keep episode ownership. Main is one ref among them,
  with its own rules made explicit rather than implied by absence:
  - One project API lists every graph ref: main plus each branch, with head,
    base, merge state, archive state, the chain-root owner episode, and the
    current (newest) chain member. It reuses the existing branch-summary code.
    It lists every unique branch, not the 50 newest episodes, and is not
    narrowed by the episode list's default archive filter. Branches are still
    created only by episodes; `branch_id == episode_id` stays. No persisted
    model changes.
  - In the browser the graph ref is a cursor inside the open project session.
    Separate project opening from ref activation. Keep the existing ref wire
    parameters, the `(project, ref)` snapshot caches, and the reducer's
    target fences (it holds one active target, snapshot, and draft, and drops
    mismatched responses); activation swaps these, it does not just move a
    pointer. The shell and view stay mounted while the new ref's snapshot
    loads. A first visit to a branch still reads it (branches have no display
    cache). Heartbeat single-flight and inactive refresh become ref-aware.
  - A switch keeps the view. It keeps the selected node only if it exists on
    the new ref, and clears a chat or episode selection that does not belong
    to it. A view with no meaning on the new ref falls back to Overview.
  - One helper builds every project URL from (project, ref, view), plus the
    optional chat or episode id the URL already carries.
  - The banner becomes a graph picker fed by that list. It hides archived
    branches by default, as the episode list does. **Episode & tasks** stays
    as a compact link to the current chain member when the ref has one.
  - Main-only rules stay explicit and owned where they are today: merges go
    into main, pairwise; Proposals and protected authority are unchanged.
  - Out of scope: branches without an episode, which would drop
    `branch_id == episode_id` and need a record migration.
- (4) Child chats unlock once their episode has ended. While it runs, the
  child composer is replaced by a pointer to **Message orchestrator**.
- (4b) Messaging an ended orchestrator starts an **Add N turns**
  continuation that carries the message. While the episode runs, a message is
  human mail, as today. The sender authorizes the continuation and sets N. The
  orchestrator keeps its branch authority; it does not become a plain chat.

## Child unlock (4): ownership and fence

Today, after **Add N turns**, only pending or running child Experiments move
to the continuation. Child Work routes stay on the source episode, and their
recovery (`create_auto_research_child_work_recovery`) requires a live parent.
The child prohibition in `_insert_agent_task` checks only
`trigger == "human"`; watcher wakes and generic Retry bypass it, and generic
Retry assigns no episode to a child Work request.

"Ended" means a status that Continue accepts: `completed`, `failed`,
`needs_action`, or `stopped`. `wrapping_up` and sleeping episodes are still
running.

Admission, owned by `agent_tasks`, sorts every task on a child chat by who
owns it:

- **Human turns.** A fresh human Send is allowed only when the child's
  episode lineage (its chain) has no running member, and for a child
  Experiment chat only when that child Experiment has itself ended. The turn
  runs outside the episode as an ordinary chat turn, authorized by the
  sender; its mode decides its authority, as on any chat. Its own watcher
  wakes, question follow-ups, and Resume, Retry, or Repair are human-owned
  and pass the same check.
- **Chat master on an owner change.** A child's native session carries a
  Work chat master rendered as an Auto-research child (with
  `auto_research_child_boundary`), and the generic `chat_master_contract_key`
  does not tell the two apart. The first human turn on an unlocked child
  therefore replaces the master with an ordinary human chat master, and a
  later episode turn on that session replaces it back, as an Experiment turn
  already reopens its own master. The master's key includes its owner.
- **Episode turns.** A child turn the orchestrator started keeps its existing
  owner. Its recovery goes through the episode route
  (`auto_research_child_work_for_operation`, child Experiment recovery behind
  the parent ending fence) or is refused. It never becomes a human turn by
  being retried from an unlocked composer (see answered question A).
- **Continuations.** Admission of an Add N turns continuation refuses while a
  human-owned turn is active on any child of that lineage, naming the chat.
  A running continuation re-locks the children; the composer says why.
- Artifact-comment edits on a child's output stay allowed, as today.
- The atomic session and stage exclusion in
  `_require_session_launch_available` stays; it already blocks two runs on one
  session at once. The fence above is about ownership, not just overlap.

Invariant check: unlock does not touch 3, 4, or 10b, because chat authority
stays mode-dependent. 10g holds only if episode recovery, target, stage, and
Stop keep their current owner, which the episode-turn rule above preserves.
`docs/design.md` says children "are read-only to humans"; update it.

## Ended-orchestrator message (4b)

Today Continue takes only a ceiling and a request id, claims zero mail, and
mail rejects an ended episode. Calling one then the other can miss the
opening turn or cost a second paid wake. So this is one operation:

- One storage transaction creates the continuation, inserts the
  sender-attributed mail addressed to its new root, and claims that mail
  together with the `reauthorized` notice before launch.
- A retry with the same request id returns the same continuation and the
  same message only when the message and N both match; a different message
  or N under that id is refused, because N sets the authorized ceiling.
- The message goes to the newest member of the chain. If that member is
  running, it is ordinary mail; if it has ended, it is continued.
- Keep the existing authorizer check (`require_patch_capable_identity`) and
  the orchestrator dispatch resolution in `auto_research_admission`.
- The composer states the cost: N turns of budget B, which also derives a
  child-Experiment allowance E = 5N.
- It is not "messageable at any time". Continue refuses when the
  orchestrator's session or stage is gone, another episode is live on the
  project, a merge is in progress, or the branch's isolation is removed. The
  composer shows the reason and stays disabled.

## Checks

- Reproduce problem 1 on both paths (healthy Agents navigation; failure then
  recovery) before and after the fix, with network and console logs.
- Python tests for the graph-ref list: main present; every branch present,
  including archived ones and ones past the 50-episode window; merge state,
  owner, and current chain member carried; no episode read needed by the
  client.
- Web unit tests: the URL helper; the picker's ref list; the view kept
  across a switch without a project reopen; a delayed response for the old
  ref is dropped after a switch; a draft on one ref never shows on another; a
  same-backend reverification keeps the project mounted; a changed backend
  clears it.
- Web unit test for the reload rule: browser reload keeps the route, desktop
  reload does not; team sign-in keeps a pending route. Update
  `projectTabs.test.mjs` and the S107 acceptance note.
- `tests/` admission tests that attempt each violation: child human turn
  refused while the lineage runs, allowed after it ends, refused again under
  a running continuation; continuation refused while a human-owned child turn
  is active; a child Experiment chat refused while that Experiment runs;
  watcher wake and Retry of an orchestrator-started child turn never admitted
  as human-owned.
- Chat master test: child episode turn, then a human turn, then a later
  episode turn on the same session each run under their own owner's master.
- Ended-orchestrator message tests: one transaction claims the mail and the
  notice; a lost response retried with the same id returns the same records,
  and the same id with a different message or N is refused;
  two concurrent sends make one continuation; N = 1 works; each refusal
  reason surfaces.
- Served-app drive on a throwaway server: branch picker switch from Agents
  (network shows no project reopen); forced reverification while a project
  is open (no "Opening project"); message an ended orchestrator and see one
  continuation whose first turn reads the message.
- Spec updates: `docs/design.md` (child read-only line),
  `auto-research-and-branch-merge.md` (child chat rule, message
  continuation), `api-web-and-desktop-projections.md` (reload route, graph
  refs, picker), `graph-history-and-transitions.md` if the ref list changes
  its contract.
- Live check left open: phone return to a discarded tab on a real device in a
  team space. Record the navigation type the browser reports; the code fix
  covers `reload` only.

## Answered product questions

- A. **Retry on an ended episode's child turn** started by the orchestrator is
  refused, pointing to messaging the orchestrator or **Add N turns**. Send
  stays the human path in that chat.
- B. **Messaging an ended orchestrator defaults to N = 3**, editable before
  sending, with the cost shown as N turns and E = 5N child Experiments.
- C. **A backend check failure keeps the project mounted** under a blocking
  reconnect overlay, so scroll, open panels, and unsent text survive. A
  changed backend still clears the project.
