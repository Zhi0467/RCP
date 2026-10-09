# Auto-research and branch merge

This specification owns Auto-research orchestration, project-wide budgets,
children, mail, the staged command client, persistent episode graph branches,
and human-dispatched semantic merge to main.

## Episode scope, budget, and authority

One human action starts one Auto-research episode for the whole project. Exactly
one project-owned orchestrator profile and one live Auto-research episode exist
per project. The optional human instruction guides the first paid invocation but
grants no authority.

Human start and reauthorization resolve the execution machine without gating
on compute readiness. The helper probes when invoked, and Settings shows the
stored probe. Setup and explicit human Cancel follow the
[compute jobs spec](compute-jobs.md).

The episode has two brakes:

- operational invocation budget **B**, set by the human and defaulted from
  Settings; and
- the protected-existing-belief rule.

There is no hidden subtree authority fence. Within the project and its graph
branch, the orchestrator may:

- create new ResearchQuestions and Hypotheses and attach them directly;
- change an existing ResearchQuestion or Hypothesis only through a Proposal;
- directly create, update, relate, judge, supersede, merge, or remove Evidence,
  Decisions, Experiments, and Blockers under the typed operation rules;
- queue and choose governed Decisions directly; and
- dispatch bounded operational children.

Every agent-produced Proposal waits for a human. Child workers retain the
ordinary profile and no Decision-choice exception. Neither the orchestrator nor
any child may approve a Proposal.

A Decision the orchestrator's own evidence settles is the orchestrator's to
decide, because a human reads the whole branch before any merge. It sets `ready`
instead only for a choice its authority cannot supply: human preference, cost or
risk the human carries, or a direction the starting instruction left open, and
`rationale` names what it is asking for. A `ready` Decision does not announce
itself and a wake armed on `decided` parks the episode until a human happens to
look, so handing a choice over never becomes the episode's only remaining path
forward.

Every orchestrator turn, worker turn, mail wake, graph-condition wake, and other
Auto-research operational continuation spends one unit of B. Exact recovery of
the same allocation spends none. Current turns finish at exhaustion; new work
does not start.

The same authorization derives a shared child-Experiment allowance **E = 5 ×
B**. Every actual child Experiment invocation spends one E unit and one unit of
that child's pinned ceiling; sleeping episodes reserve none and exact recovery
spends none. E is shared across all child Experiments and cannot be widened by
the orchestrator.

## Workers and child Experiments

The orchestrator may seat an ordinary Work worker on any existing graph node; its
instruction states what result ends the job. Seating selects context and
accountability, not a second graph-authority subtree. The
worker's repository scope is the exact child run scope and its graph target is
the parent Auto-research branch. Child Work follows the selected execution
route: direct Slurm submission or the generic `launch` helper. The helper binds
the current turn's machine, writable roots, operation, and episode. Both routes
use ordinary shell `watch.json` validation, settlement correction, and arming.
A still-running helper launched by the turn or retained from its recovery
lineage needs the returned shell watcher before the turn ends.

A child route is waiting when its current task succeeded and it has an armed,
undelivered watcher. Waiting is derived from the route, task, and watcher rows;
it is not a new persisted task state. The root's status distinguishes waiting
workers from running and settled workers.

At most one live Experiment-loop episode exists per Experiment per graph
target ([decision](../decisions/2026-10-06-experiment-loops-are-per-branch.md)).
An orchestrator kickoff reuses normal readiness on its own branch. It never
stops, adopts, or waits on a loop it did not start, on any target. A loop live
on another target does not block the kickoff; the result lists it as
`live_elsewhere`, and `status` lists every live loop off the orchestrator's
branch as `other_branch_loops`. Both are compact and capped: `{rows, omitted}`
with one row per loop (node, episode, target, starter, state, checkout). The
orchestrator and human-started loops are told to ask the human when their work
could interfere with another branch's episodes, for example the same node and
the same checkout. A child loop cannot `ask`; it is told to pause that work and
report the conflict in its answer, and the orchestrator asks the human. To restart its own child, the orchestrator stops it, waits for
settlement, and kicks off again. Routes left pending by the retired replacement
path were settled at upgrade as cancelled and never launched; their
`replaces_episode_id` remains history.

Child task/episode admission, budget spend, parent registration, graph target,
and lifecycle routing commit atomically. Recovery dispatches an accepted queued
child without creating another id or spending again.

## Persistent graph branch

An Auto-research root or a main-target Experiment with graph isolation on owns
one persistent canonical graph branch. Both use the same branch creation path.
The owner episode id is its stable branch identity. Before any provider launch,
RCP:

1. reads one coherent main head;
2. creates or reconciles branch metadata in the canonical state repository;
3. stores the same immutable main base in SQLite episode binding; and
4. proves the episode-to-branch binding.

A crash may leave an orphan on one side of the canonical/SQLite boundary, but
startup reconciliation either restores the exact binding or fails explicitly.
An episode bound to a graph branch never launches without it or redirects to main.

The canonical branch record contains project, episode, kind `auto_research` or
`experiment_loop`, immutable main base head, authorizing human snapshot, creation
time, append-only branch Patch history, current head, and durable merge receipts.
Branch revisions are identified by branch id plus head; an integer alone is
insufficient.

The branch materializes the accepted main prefix through its base and then its
own log. It does not copy mutable main outputs and never rewrites its base when
main moves or a merge succeeds.

## Exact branch targeting

Every graph-aware path descended from the episode uses the branch target:

- root orchestrator context, validation, Apply, correction, and continuation;
- child Work and child Experiment context, control, Apply, watcher, and repair;
- branch graph conditions and lifecycle reconciliation;
- episode settlement, wrap-up inputs, and diagnostic summaries; and
- branch merge preparation.

Task, episode, watcher, stage, native-session, command, and control-plane rows
carry the exact graph target. Main and branch transition-event consumers keep
independent target watermarks. A conversation or native session already bound to
a branch cannot be resumed as main, and vice versa.

Agent graph and synthesis file pointers resolve inside that same branch,
including on a remote execution machine. Shared project inputs such as the
human paper introduction remain at their project-owned paths. A child
Experiment's Patch retains its own episode id; branch admission checks the
canonical task's exact graph target rather than equating child provenance with
the parent branch id.

A branch Patch advances only branch graph, control, guidance, and events. It
does not change main revision, main materialization, main control, or ordinary
main watchers. Human Sync, ordinary Work, and unrelated project work may keep
advancing main while the episode runs.

## Graph and code isolation

The graph branch covers canonical research state. Code isolation is a separate
Run choice. An omitted Auto-research code choice resolves on only with one
run-scope repository, Git 2.38 or later, and no writable-path grant overlapping
the shared checkout. Otherwise it resolves off. An explicit ineligible choice
still refuses. Graph isolation stays on for every Auto-research episode. Its
Decision exception remains branch-only. Experiment Work has no such exception.

The root episode owns the optional Git worktree. Children, continuations, and
human-started Experiments on its graph branch resolve the same immutable
[isolation binding](conversations-episodes-and-watchers.md#episode-isolation).
They write the worktree instead of the shared checkout. Turning code isolation
off keeps ordinary repository write scope. Neither choice changes graph truth
membership or provides repository rollback.

Graph merge still neither copies nor replays repository files. A failed or merged
graph branch persists as an audit trail. Human-dispatched Merge coordinates code
delivery and graph delivery through the isolation owner.

## Mail and lifecycle notices

Agent mail is star topology: the orchestrator may address workers it spawned,
and those workers may reply. The orchestrator addresses a spawned worker by its
stable child worker id. While any member of the episode's continuation chain
runs, a child's composer points to **Message orchestrator** and explains the
ownership fence (`auto_research_child_read_only`). Sleeping and wrapping-up
episodes still hold that fence. After the lineage ends (`completed`, `failed`,
`needs_action`, or `stopped`), the child chat admits ordinary human turns; a
child Experiment must also have ended, and the episode's isolation must remain
available. Isolation cleanup in `removing` or `removed` keeps the composer locked
with `episode_isolation_unavailable`, matching task admission. The sender
authorizes that turn outside the episode, and Discuss or Work determines its
authority. Its watcher wakes,
question follow-ups, Resume, Retry, and Repair pass the same lineage fence.
The native-session master key of an episode-owned turn names its episode; a
human-owned turn keeps the ordinary chat key, so existing chats keep their
masters. The first human turn replaces the child's master with an ordinary chat
master; a later episode turn on that session reopens its episode-owned master. Retained Patch
values and recovery masters use the same owner check.

An orchestrator-started child turn retains its episode ownership. Recovery uses
the child Work or child Experiment episode route and its ending fence; generic
Retry or Resume never converts it to human-owned work. One store rule
(`episode_child_recovery_refusal`) refuses generic Retry and Resume of child
Work turns, and of child Experiment turns once their orchestrator has ended
unless the child Experiment's own Stop is still pending; that recovery settles
an already-paid turn behind the ending fence. Task projections clear Retry and
Resume by the same rule, the chat points to **Message orchestrator** instead,
and transport auto-retry skips such turns. The locked composer offers
**Message orchestrator** only while a running lineage holds the lock, not for
isolation or merge refusals. A new Send is
the ordinary human path. Add N turns refuses while a human-owned turn is active
on any child of the lineage and names the chat; an admitted continuation locks
those composers again. The native-session and stage exclusion still prevents
overlapping launches. Artifact-comment edits remain allowed.
Mail is Markdown hearsay and carries no graph authority; `patch.json` remains the
only graph channel.

RCP-authored lifecycle notices are separate authority facts: child settlement
or recovery, child Experiment attention/ending, and graph-condition readiness. Source transition and deduplicated notice commit
together. A busy actor receives the notice after its current turn; neither mail
nor lifecycle notices are injected into a live provider process. The separate
[live human steering](providers-and-containment.md#live-human-steering) channel
addresses only an ordinary human-triggered Discuss or Work turn. It does not
address episode workers or give agents a live messaging channel.

Sleeping-actor delivery claims a bounded notice batch atomically with one B
allocation. A graph-condition wake of the root orchestrator also claims pending
lifecycle notices and root-addressed mail within the delivery bounds. Lifecycle
wakes wait a short grace window so notices arriving together share one allocation.
A running orchestrator may harvest or clear its inbox without a separate wake;
the harvest returns and consumes, in one transaction, the pending lifecycle
notices and the pending mail addressed to the orchestrator, attributing the
consumption to the running turn, so mail that arrives before a turn's last
harvest never costs a wake. Budget exhaustion retains notices but cannot create
an unauthorized turn. Clear refuses before acknowledgment if even its compact
full response exceeds the bound.

Every Stop records who initiated it (`human:<member>`,
`orchestrator:<operation>`, or `system:<reason>`) in the same transaction as
the fence. A notice born from a stop the orchestrator itself requested carries
`wake_suppressed=self_caused`; a
child task failure classified as a revoked login carries
`wake_suppressed=provider_auth`. A suppressed notice never admits a paid wake
and never holds ordinary mail; the running orchestrator still learns of it
through the harvest, it still counts in the ending receipt, and it never blocks
quiescence or an ending.

A completed child watcher group wakes the same child route and native session,
never the root. Watchers retain the episode id and route worker id. One atomic
claim creates the continuation, binds it to the route, and spends one B unit;
the same group cannot create another wake. Admission requires a running parent,
a route without a Stop fence, and a succeeded current child task. Exhausted B
leaves the completion pending and visible, including after the exhaustion ending
fence. The wake retains the watcher's resolved model, reasoning, and skill policy
within the child's pinned execution scope. It carries job id, exit status,
timestamps, duration, log path, and backend identity, plus shell observer results;
the child's eventual settlement reaches the root through the existing lifecycle.

## Staged command client

The orchestrator receives one exact RCP-authored command prefix. The closed
surface supports:

- Patch validation and keyed Apply;
- status inspection;
- keyed ordinary-worker spawn and child pause/resume/stop;
- keyed star mail and graph-condition registration;
- keyed child-Experiment kickoff/stop/exact-resume;
- keyed inbox harvest or clear; and
- keyed guarded finish.

Native continuations retain the assignment and completed work while refreshing
the exact command prefix and callable vocabulary, graph inputs, Patch schema,
validator, and resolved write boundary. Current instructions supersede earlier
ones on those subjects. Graph correction supplies a fresh validate-only command
credential; earlier coordination or Apply commands do not remain available to
that correction.

Keyed Apply is the orchestrator's preferred graph channel. Applying inside the
turn returns the new revision and refreshed canonical paths while that turn can
still act on them. An unconsumed final `patch.json` still applies at turn
settlement, but the episode then spends another invocation to observe the result.

There is no agent Retry verb. Resume means the exact saved session and stage;
an unusable binding tells the orchestrator to stop that child, wait for
settlement, and start a fresh one.

Mutating commands require a caller-supplied idempotency key. RCP records the
exact admitted intent and any instruction/goal/Patch file bytes and digest before
the effect. Completed `ok` and `invalid` outcomes replay exactly. An
`unavailable` or interrupted effect may only prove or resume the same
deterministic intent and identity; it cannot reread new bytes or invent another
child. Every command start and exit is recorded in the task event stream.

The per-invocation broker authenticates a command as the fresh provider process
or one of its live descendants on the execution host. It stores no reusable
bearer credential in prompt, environment, stage, or command arguments. This
guards command provenance within the cooperative execution-account model; it
does not defend against an arbitrary hostile same-UID process.
Broker authority is separate from episode identity: ordinary Work and
Experiment-loop use the same turn binding, while every Auto-research request
retains its episode id and signature. Generalizing that binding does not widen
the root's command policy: it has no compute helper verbs. Child Work follows
the same execution route as ordinary Work: the generic process route exposes
only `launch`, while the scheduler route submits directly and exposes no helper
verbs. Its existing validation and reply commands retain their policy.

Apply uses the ordinary transition-manager path on the branch target, with
idempotent source effect identity and refreshed graph pointers. Guarded finish
is a pure state transition: it refuses with a complete immutable blocker receipt
while child work, undelivered notices, or accepted-unreflected
admissions remain. A waiting child contributes a `waiting_work` blocker with
action `stop --key <key> <worker_id>`. Finish never performs cleanup as a side
effect of saying finish.

## Orchestrator questions

Only the orchestrator may call keyed `ask`; workers mail the orchestrator.
The server captures the question's episode, operation, native session, stage,
write scope, and graph target. Repeating an identical owner/key reads its current
state. `ask` returns `parked` immediately and the orchestrator ends its turn;
the episode remains running and workers continue. Open questions add a
`needs_action` health overlay without changing admission or persisted status.

A human answer creates one hearsay-only human mail item per question/revision.
Normal mail admission wakes the orchestrator and spends one invocation, deferring
when its session is busy. The mail identifies the question and carries its full
answer and selected choices, including when the orchestrator harvests it during
an existing turn. Reconciliation repairs a resolution interrupted before mail
insertion. No answer grants approval,
graph authority, write scope, or additional budget.

Every orchestrator launch stages a fresh bounded question snapshot from the
question store, including open questions, undelivered dismissals, and answers
claimed by that wake. It never reads chat transcripts. A successful provider turn
acknowledges the staged dismissals; a failed launch leaves them pending. Dismissal
never sends mail or wakes an agent. Episode endings withdraw cards read-only;
continuations reopen ancestor cards while preserving their original binding.
Root and session-start Retry contracts share the resolved `ask` command surface;
workers remain excluded.

## Completion, Stop, and report

Normal completion requires an explicit idempotent `finish`. A settled child, an
open Blocker, temporary resource contention, or a downstream human-started
Experiment is not completion while existing agent authority and tools can still
resolve the prerequisite. The orchestrator must act, delegate, or arrange an
observable continuation. It may pause for a human only after naming the exact
new judgment, credential, privileged action, approval, or coordination needed.
Capacity handling follows the actual execution route: a scheduler can accept
queued work, while a direct process host may require diagnosis and an observable
continuation before launch. Neither route justifies inventing a scheduler or
repeating an uncertain submission.

At budget exhaustion or non-Stop ending, admitted children settle, the parent
fences new work, and the common visual report resumes the exact branch-bound
session with one immutable receipt. The receipt compacts to its storage bound
and never blocks settlement; a permanent admission defect settles the episode
with a visible nonblocking report error instead of leaving it in `wrapping_up`.
Human Stop uses the common graceful fence and skips the report.

Reauthorization is a **continuation episode**: a new episode record chained to
the ended one by `continues_episode_id`, on the same graph branch, with the
orchestrator resumed in its exact native session and told how many turns it now
has (the human's number is the continuation's ceiling). The branch keeps the
chain root's id as its `branch_id`; every chain member's graph target names that
branch, and a branch resolves to the newest member of its chain wherever the
current writer matters. Child Experiment routes still pending or running move to
the continuation, so their endings reach the resumed orchestrator; child Work
routes, notices, ordinary mail, and watchers stay on the source. Undelivered
question-answer mail follows the reopened question to the continuation
orchestrator; already delivered answers are not sent again. The continuation's first
turn is a `lifecycle_wake` that claims a `reauthorized` notice naming the source
and the new ceiling. The source episode is otherwise untouched: its ending,
receipt, report, attempts, watchers, and notices stay as they were. A
continuation is offered (`can_continue`) only where the source's orchestrator
session and stage are still bound; otherwise the human starts a new episode. `POST .../episodes/{id}/continue` with
`invocation_ceiling` and a client `request_id` serves both modes; it refuses
while the source has a live turn, is not terminal, is already continued, a merge
runs on its branch, or a newer live episode occupies the project, or the node
on the same target. The
same `request_id` replays the same continuation. A continuation records the
member who made it; the source keeps its own authorizer. Stopped watchers stay
stopped. See
[reauthorization continues on the same branch](../decisions/2026-09-14-reauthorization-continues-on-the-same-branch.md).

**Message orchestrator** addresses the newest member of the chain. A running
member receives ordinary human mail. Messaging an ended member authorizes an
Add N turns continuation carrying that message. The composer defaults N to 3,
editable before sending, and always sends it; the API has no default N, and a
send without N is ordinary mail only; the composer shows N operational turns (budget B) and the derived
E = 5N child-Experiment invocations. This remains an orchestrator with branch
authority, not an ordinary chat.
An ordinary-mail-only send that races with the episode ending refuses and asks
for a continuation budget; it cannot silently authorize the default turns.

One storage transaction creates the continuation and sender-attributed mail
addressed to its new root, then claims both that message and the `reauthorized`
notice for the opening turn. Replaying the same request id returns the same
continuation and message only when both the message and N match; changing either
under that id is refused.
Concurrent sends cannot create two continuations of the same source: a sender
that loses the race delivers its message as ordinary mail to the winning
continuation while it runs, and its N is unused. Every refusal carries a code. Messaging
does not bypass continuation admission: a missing session or stage, another live
Auto-research episode, an active merge, removed isolation, or an active
human-owned child turn refuses continuation. The composer shows the refusal
reason and remains disabled until admission is available.

Parent settlement and report launch, including restart of an allocated report,
wait for unfinished child Experiment turns and their exact recovery. A parent
ending does not revoke an already-paid child recovery or spend E again. It still
refuses new invocations; pending watcher completion remains unconsumed when the
parent has ended or the shared E allowance is exhausted.
An older report allocation whose snapshot predates child settlement cannot
produce an accurate final report. Once the child settles, that allocation ends
with a visible nonblocking report error; its immutable receipt and attempt
history are preserved. It does not generate a report from stale child facts.
Startup and runtime reconciliation inspect every episode, independently of the
recent-history display limit, so older parents continue settling after child work.

Episode Stop retires child watchers in the same admission fence as the root's
watchers and persists Stop on its live child Experiments, including their late
watcher handoffs. The root's child `stop` verb likewise fences that route and retires its
armed watchers. A completion on a stopping or stopped route cannot create a
wake. These fences do not cancel compute jobs.

## Branch lifecycle and merge eligibility

A branch remains writable while any episode of its chain accepts graph work
and no merge reservation fences its owner. Graph delivery uses branch facts:
its head is exact and newer than
its base, no successful receipt covers that head, and no queued, running, or
pausing graph-capable task is writing to the branch (a `branch_merge` task is
not a writer). Nothing about the episode is a condition: not its status, ending,
wrap-up state, quiescence, or whether its orchestrator is paused. Merging ends
nothing; there is no **End and merge to main**. Ending an episode is Stop,
merging is Merge, and a human may do either. See
[a branch merges on branch facts](../decisions/2026-09-14-a-branch-merges-on-branch-facts.md).

Eligibility and merge state derive from the canonical branch head, the branch's
task state, and successful receipts. Recovered or explicitly abandoned historical
attempts do not count as active writers. The branch is never deleted. A newer
branch head after a prior receipt may be merged again; a head already covered by
a successful receipt needs no graph delivery. Code delivery is independent: a
code-only owner or a delivered graph whose code moved on may still Merge.
An active merge fences new work on the entire binding through cleanup.

Only a human project member can dispatch **Merge**, from any card of the
owner's episode chain. Fully delivered, cross-project, cross-branch, or
concurrently merging requests fail closed, as does a branch with a live writer,
which is named. The merge task does not spend any episode budget.

## Episode code merge and cleanup

Merge persists an owner reservation and an attempt before committing leftovers.
Mutable attempts are separate from the immutable binding. Their phases are
`pre_merge`, `landing`, `agent_merging`, `verified`, `graph_committed`,
`cleanup`, and `done`.
The next Merge or startup reconciles an interrupted attempt before new work.
An uncertain transport result is checked against its recorded commits.

The target is a human-chosen local branch, defaulting to the starting branch.
Git 2.38 or later is required. Missing targets, the episode branch itself,
targets checked out outside the shared checkout, and a dirty shared checkout
holding the target are validation errors. An unfinished merge, rebase, or
cherry-pick, unmerged index entries, and dirty submodules also refuse Merge.
RCP commits ordinary episode leftovers once with `git add -A`. Ignored files
stay out. It never commits the shared checkout's changes. RCP authors its
leftovers and landing commits under a fixed RCP identity, so an account with
no Git identity can still merge.

The existing graph builder and `git merge-tree --write-tree` classify residue.
With neither kind of residue, RCP lands code first and verifies it before one
main graph transition. Both paths build the landing commit from the tested tree. A checked-out
target fast-forwards to it in the clean shared checkout, in one step. An
unchecked-out target moves by a compare-and-swap ref update. Source and target commits and checkout identity
are rechecked before landing. RCP never resets, stashes, or force-pushes.
A code conflict or graph residue starts one merge task. Its agent lands the
code with a merge commit inside chat Integrate's local-merge scope, and RCP
verifies the landing before the graph commit: the target must contain both the
source and the target commit the attempt recorded. With no graph residue, the code
lands in its own turn and the graph then merges with no provider turn. A
code-only owner's task targets main and has no graph side. Squash needs an
agentless merge. A task that ends without the code landed fails before any
graph commit; the next Merge starts a fresh attempt.

History defaults to a merge commit (`--no-ff`). Squash records its resulting
commit and disallows Keep branch open. Verification proves source ancestry or
the recorded squash commit. Graph and code delivery have separate records.
A later graph failure does not undo landed code.

Cleanup records each step independently. The defaults remove the worktree,
delete its code branch, and archive its graph branch. Branch deletion requires
worktree removal and verified delivery or a recorded squash. Archive hides the
graph branch from the default list and preserves every Patch. Keep branch open
skips cleanup. Discard worktree or Archive before delivery requires explicit
confirmation. Live turns block worktree removal; live or unobservable jobs
pause it until the human confirms them.
Cleanup failures remain cleanup failures and can be retried individually.

## Semantic rebase and merge

RCP prepares a closed graph-only context containing:

- immutable branch-base graph;
- current branch-head graph and exact head;
- current main graph and exact head;
- a typed semantic base-to-branch delta;
- bounded branch Patch summaries and provenance;
- relevant canonical human actions and previously delivered source heads;
- transition schema and validation command; and
- deterministic conflicts found before provider launch.

Merge dispatch pins current main project truth membership as its run truth
scope, independent of the default repository selection for ordinary runs.
`repositories_read` remains empty. A membership change after dispatch requires
a new merge; provenance outside current membership is rejected.

RCP builds authorable non-conflicting node creations, ordinary node updates, and
legal edge creations, removals, and replacements from the same semantic paths
checked by merge validation. An existing edge edit removes and recreates that
edge with the branch's changed fields overlaid on current main, preserving
compatible main edits. Validation checks the replacement's net field changes;
the temporary removal does not grant permission to change unrelated fields or
leave the edge missing. Both operations retain ordinary authority checks.
Already-present values are omitted. Protected changes, conflicting nodes, Decision
outcomes, removals, and source Proposals remain together in the agent's residue.
This keeps coupled fields such as Decision options and selection in one update.

An ordinary agent update resets standing, on main exactly as it did on the branch,
so an edit to a node main merely accepts is built rather than deferred. Where
branch and main still agree on a stronger standing, the build restores it so the
update cannot quietly drop it. What is deferred is a human standing move on main
after the fork: carrying the branch edit would discard that judgment, so the merge
decides it explicitly.

Every residue path carries the reason it needs judgment, and the merge contract
renders that same resolved mapping. The built plan reaches the agent as a staged
input file rather than inlined prompt text.

Experiment guidance text is merged; its backend-owned freshness flags are not
source changes. Main's transition recomputes validity from the merged dependencies,
so guidance that was fresh on the branch may become stale on main.

An empty residue commits without a provider turn. Otherwise, the agent writes
only the residue operations; RCP prepends the built operations for both self-check
and commit. Unsupported configuration changes, invalid fixed operations, and
mandatory source Proposals with out-of-scope provenance fail before provider
launch. They never enter an agent correction loop that cannot repair them.

When needed, the merge agent receives the orchestrator graph profile under the
human merge dispatcher's authorization. Without code isolation it receives
scratch but no repository write roots. When it lands code, its roots are exactly
chat Integrate's local-merge scope: the episode worktree and the shared
checkout, and its prompt renders that same resolved scope. It never receives
membership, ontology, project configuration, Proposal approval, server command,
or general branch authority.

The combined candidate is one typed semantic Patch against current main. The
transition manager validates it and commits one attributable main transition
or nothing.
Conflict diagnostics enter the same bounded native-session correction loop;
there is no manual node conflict viewer.

The same transition may apply legal direct changes and create pending main
Proposals for protected changes. A human judgment on the branch is not approval
authority for the merge agent: the human reviews the resulting Proposal again in
main's ordinary Inbox. Related content, status, and standing changes to one
protected node may form one atomic review so approving one cannot invalidate the
rest. The merge can also propose removal of an accepted ordinary node, retaining
the explicit human removal gate on main.

Every proposed effect must represent the exact branch delta without unrelated
changes. Special human status/standing facts come from canonical human Patch
authorship and initiating operations, never report wording or inferred intent.
Live pending source Proposals retain their identity and content. Resolved or
stale source Proposals stay in branch history; their resulting node/relationship
changes are represented independently. The source Proposal history is unchanged.

Proposal identity is deterministic for its branch, semantic operations, and the
last delivered source head (the immutable base before the first merge).
Validated prior merge receipts and their exact branch snapshots identify already
delivered source changes. An unchanged change is not proposed again on a later
branch head, including after a human rejects it on main. A genuinely new source
change still requires the normal merge and review path.

Merge remains limited to main's supported semantic actions and review forms.
Ontology changes, human-only extension-field edits on ordinary nodes, and
structural/content combinations whose separate reviews invalidate one another
produce an explicit merge validation failure. They are not silently omitted or
partially committed. Both histories remain available for inspection; graph
branching does not grant the merge agent project-configuration authority.

The append path compares the exact main head used for preparation. If main
advances, RCP rebuilds the context and semantically re-prepares against current
main; it never overwrites or concatenates raw history. A paused or failed merge
leaves both histories unchanged, and a later human may dispatch another merge
task.

The committed merge Patch names source branch/episode/base/head, main head,
merge task, and human dispatcher. After commit, RCP appends or reconciles a
durable receipt containing source head, resulting main revision and transition,
task, and time. Main commit and receipt are crash-idempotent: a process failure
after main append cannot merge that same branch head twice.

A successful main transition reaches ordinary main watchers exactly once. It
does not notify branch watchers as though branch truth changed.

## Runs projection

The Auto-research parent owns one episode health and one recommendation. Worker
and task states remain supporting history, not peer parent states. The detail
shows:

- compact branch id, immutable main base, and current branch head;
- unmerged, merging, merged-through-head, needs-action, or failed merge state;
- paused and interrupted merge tasks project as needs action, retain their
  diagnostic, and offer a fresh **Merge** dispatch when eligible;
- a persistent **Merge** control, including when currently ineligible. It
  summarizes the preview (graph changes, commits ahead) and opens pickers on
  click: the target branch, Merge commit or Squash (Squash only when no agent
  is needed), removing the Git worktree together with its code branch, and
  keeping the graph branch for more runs (not with Squash). The pickers count
  every change the merge agent must resolve, conflicts and Proposals included.
  The pickers' Merge checks current server state and either admits the merge or
  displays the specific blocker beside the control. Ineligibility does not hide
  or disable the control; an in-flight UI action or a missing preview disables
  it;
- the ordinary merge task/output/correction/recovery history; and
- the episode report or final report error.

**Open graph** opens the episode's persistent branch in the ordinary Research
workspace. The initial changes view shows the semantic difference from its
immutable base, with neighboring context, created/updated/removed markers,
before/after content, and canonical Patch/task provenance. A task filter focuses
the changes attributed to one task. Expanding context or showing the full graph
changes the display lens; editing and node pickers always use the full branch.
Removed objects remain inspectable history without current-node controls.

The branch has the same manual node/relationship edits, Sync, Inbox, and ordinary
node/project Discuss and Work controls as main. Those actions remain available
while Auto-research runs and after it settles. Each ordinary conversation owns
its own native session, stage, and human authorization. It neither reuses the
orchestrator session nor acquires an Auto-research budget or parent route.
Standalone human-started Experiment loops likewise keep their own episodes.

Human Sync and ordinary Work preserve their actual authorship in branch history.
Only episode-owned work receives that episode id; graph target alone does not
imply operational ownership. Resume, Retry, repair, steering, and watcher wakes
retain the original exact graph target. Main and branch chats are separate.
Merge admission waits for graph writers, and an active merge fences new writes.

The selected target persists through Research, Inbox, and Chats. **Main graph**
returns to main. Drafts, selections, request fences, viewport, and chat state are
scoped by project and graph target, even at equal numerical revisions. A missing
or foreign branch fails explicitly; it never substitutes main state or a main
conversation. A report retains its original lifecycle version; file edits
produce later artifact versions without changing that lifecycle or graph
provenance. Orchestrator artifact comments and replies stay in the episode
thread and spend no operational budget.

Today's surface is one branch per episode and one human-dispatched merge. There
is no general branch manager, conflict editor, cherry-pick, or repository control.
No merge runs without a human dispatching it.
The API supports the bounded episode landing and cleanup above. That
is current scope rather than a permanent exclusion; a version-control model for
the graph is admitted in
[the graph-branch scope decision](../decisions/2026-09-08-graph-branch-scope-is-reopened.md).

### Browser preference in Auto-research

The human's launch request sets `browser_requested` on the generic episode.
The root request, human Continue, retries, actor wakes, and child Work and
Experiment admission preserve that preference. Children inherit it from their
parent episode; an agent's command payload cannot grant browser access.
Each child uses its own stable stage across retries and wakes. The episode API
shows the launch preference and each turn's durable browser status.
