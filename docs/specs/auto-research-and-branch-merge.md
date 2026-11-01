# Auto-research and branch merge

This spec owns project-wide orchestration, budgets, children, mail, staged
commands, graph branches, and merge. Source owners are
[Auto-research](../../src/rcp/runs/auto_research.py),
[admission](../../src/rcp/runs/auto_research_admission.py), and
[episode merge](../../src/rcp/runs/episodes/merge.py).

## Episode scope, budget, and authority

One human action starts project-wide Auto-research. Each project has one
orchestrator profile. Each graph branch has at most one live Auto-research episode.
Other episodes appear in capped lists in status and the start dialog without
gating admission. An optional opening instruction grants no authority. Start pins
the provider, model, and reasoning from the human's choices or the orchestrator
profile. Children use the orchestrator's choices or node-chat defaults. All
profiles run on the project's base machine, as enforced by
`config.require_base_machine`. Unsupported choices are refused before admission.
Prompts show the same probed choices.
**Why:** branch writes are isolated. Humans coordinate overlapping checkout work.

Start and reauthorization resolve execution without gating on compute readiness.
The helper probes when used. Settings shows stored readiness. See
[Machines and compute](machines-and-compute.md).

The brakes are the human's invocation ceiling **B** and
[protected-belief authority](authority-and-proposals.md#graph-action-vocabulary),
not a subtree fence. The root has the orchestrator profile. Children have the
ordinary profile. Neither approves Proposals. The root decides choices that the
evidence settles. It queues `ready` only for human preference, risk, cost, or an
unspecified direction, and names the needed judgment in the rationale. An
unannounced ready Decision cannot be its only plan.

Each root turn, wake, and operational continuation spends one invocation from B.
Ordinary Work workers are unmetered, including their turns and wakes. Exact
recovery spends nothing. It restages durably claimed mail and lifecycle input
from the allocation's attempts, replacing failed copies. Cleared input stays
unread. If recovery input is oversized, it stages the oldest items that fit and
logs omissions. Exhaustion lets current turns finish but admits no new work.
Child Experiments share **E = 5 × B**. Each invocation spends one invocation from E
and the child's pinned ceiling. Sleep reserves nothing. Exact recovery spends
nothing, and the orchestrator cannot widen E.

## Workers and child Experiments

The root chooses children by job. Short fixes stay in its turn or native
subagents. Compute that outlives the turn goes to a node worker. Research-state
upkeep goes to a project worker. Before starting a child Experiment, audit that
Experiment and every one-edge neighbour. The root decides whether findings
warrant graph changes. Results bearing on a hypothesis or decision are Evidence.
Plumbing checks are not.
**Why:** preparation and smoke runs must not consume the episode as research results.

A Work worker may sit on any existing node. Its instruction defines completion,
while its seat supplies only context. It uses the exact child repository scope
and parent branch. Direct scheduler submission and generic launch follow ordinary
shell watcher validation, correction, and arming. A live helper from this turn or
recovery lineage requires the returned shell watcher at settlement. A succeeded
route with an armed, undelivered watcher is waiting. This is derived from existing
rows. Root status distinguishes waiting, running, and settled work without a new
task state.

At most one live Experiment loop exists per node and target. Kickoff uses normal
branch readiness. The root never stops, adopts, or waits on a loop it did not
start. Loops on other targets appear in capped results and status without blocking
admission. Interference with another branch's RCP episode requires human
coordination. Shared scheduler queueing alone is not interference. A child without
ask pauses and reports to the root. Restarting its own child requires Stop,
settlement, and then kickoff. Child admission, spend, parent registration, target,
and lifecycle route commit atomically. Recovery dispatches the same queued id
without another spend.
**Why:** visibility permits variants without cross-branch control authority.

## Branch names

Root branches have project-unique names governed by
[branch-name storage](../../src/rcp/storage/branch_names.py). Retired and archived
names remain reserved. Both episode kinds accept names. An Experiment may name
only a new isolated branch. The name and episode reservation share a transaction.
The Web field lowercases text and collapses spaces and hyphens while typing.
It trims edges only on submission. Invalid and taken names produce distinct refusals.

Blank names derive from the root's UTC date as `research-<mon>-<day>` or
`experiment-<mon>-<day>`. Collisions use numeric suffixes in creation order.
Migration backfills legacy names and their reservations. Renames preserve other names.
Transfer carries current and retired reservations.

Discuss, Work, Seed, Refresh, Experiment, Auto-research, and merge launches carry
the same inventory of retained readable branches in their resolved read scope
and history usage. Archived readable targets remain listed. Names or ids resolve
through [History reads](graph-history-and-transitions.md#history-reads).
Any member may rename, archive, or unarchive through graph-ref routes. Labels are
operational and never Patches. All surfaces use shared names and the
[graph-ref projection](api-web-and-desktop-projections.md#project-graph-ref-inventory).
**Why:** one inventory prevents inconsistent labels and targets. Reserved old
names keep historical tags unambiguous.

## Persistent graph branch

An Auto-research root or isolated main-target Experiment owns one persistent
branch whose id is its episode id. Before launch, RCP captures a coherent main
head, creates or reconciles canonical metadata, stores the base in SQLite, and
proves the binding. Startup repairs one-sided crashes only by restoring that
exact binding. Otherwise it fails and never redirects to main.
[History and targets](graph-history-and-transitions.md#append-only-history-and-graph-targets)
own replay and metadata. Main movement and merge never rewrite the branch's base
or history. Continuation episodes share the root's branch.

## Exact branch targeting

All descendant context retains the exact target. Validation, Apply, correction,
continuation, control, repair, watchers, lifecycle, settlement, reports, and merge
also retain it. Task, episode, watcher, stage, session, command, and control rows
bind the target. Main and branch watermarks are independent. Resumed sessions
cannot switch targets. Remote graph and synthesis pointers resolve there. Shared
paper retains project paths. Child Patches retain child episode ids. Admission
checks the task target rather than branch-owner equality. Branch writes affect
only that branch's projections. Ordinary work can continue on main.

## Forks

A fork has its own UUID4 id and an immutable exact parent head. It has either an
episode lineage owner or, for member forks, no episode. A lineage owns its
persistent branch and the forks it created. Metadata and replay follow the
history spec.

Spawn and Experiment kickoff accept `--at <ref@R>` to fork an exact head. A bare
`@R` means the root branch. They accept `--target <branch>` for an open branch
owned by the lineage. Main, foreign, and archived targets are refused. Omitting
the target keeps the root's branch. Seat options never choose the target.
`--seed-file` requires `--at` and nonempty operations valid under the orchestrator
profile. Fork reservation, the seed snapshot, admission, and spend commit together.
Publication and the first seed Patch precede child launch. Retry re-drives the
same identity and never creates another fork.

Runs targeting a fork read only that fork and its ancestors through their fork
points. Other runs read main and every branch. Prompt and overlap visibility
share that scope, which grants no writes. Human views remain unfiltered.

An owned fork archives automatically only while its own head remains at its
immutable parent-at-fork head.
It must have no reachable writer, nested live fork, unknown publication,
undelivered watcher or mail, live child Experiment, or accepted admission.
Resumable sessions and stages, along with bound merge destinations, count as
writers. Child polling checks under the canonical lock. Explicit archive targets
owned forks, never the root's branch. Member forks do not archive automatically.

Keyed merge delivers an owned fork into its open, owned parent. Main or foreign
parents require human dispatch. A deterministic plan commits without a provider.
Conflicts or review cards spend one lineage invocation. That invocation is reserved
at admission or when parent movement first requires it. An exhausted budget
refuses the merge. Protected changes become pending parent Proposals. The root
must be running without ending or Stop. Delivered heads, live source writers,
and concurrent parent merges are refused. Merge does not archive.
[Invariant 3](../../AGENTS.md#stable-invariants) bounds lineage authority. Only a
human dispatches delivery into main.

Members may fork an exact head and merge any fork into its parent without an
episode budget. [Graph-ref requests](../../src/rcp/api/episode_routes.py) own the
request fields. Episode branches use episode Merge. Member merges carry no
episode attribution, even from lineage forks. A member fork's first Experiment
reserves its isolation ownership at admission. Later Experiments there share it.

## Graph and code isolation

Auto-research always isolates its graph. Code isolation defaults on only with
one repository, Git 2.38+, and no overlapping writable shared-checkout grant.
When several repositories are writable on the orchestrator's machine, the start
dialog's Code worktree option picks one, and the run writes only that one. With
none, the option is off and disabled. Explicit ineligible choices are refused. The root owns the optional worktree.
Children, continuations, and human-started Experiments there share the immutable
[isolation binding](conversations-episodes-and-watchers.md#episode-isolation).
Without code isolation, they keep ordinary repository scope. Neither choice
changes truth membership or undoes repository effects. Graph merge never copies
files. Human Merge coordinates code and graph delivery through the isolation owner.

## Mail and lifecycle notices

Mail is Markdown hearsay in a star connecting the root to workers. It grants no
graph authority and is not live injection.
[Human steering](providers-and-containment.md#live-human-steering) remains a
separate ordinary-chat channel.

While any continuation member runs, including during sleep or wrap-up, child
composers offer Message orchestrator. After the lineage ends, ordinary human
turns require ended child Experiments and available isolation. Removed isolation
or isolation being removed refuses those turns. Questions, wakes, Resume, Retry,
and Repair share the ownership fence. Artifact comments remain allowed. Human
turns use ordinary chat masters. Episode turns use episode masters. Switching
reopens that master's retained Patch and recovery checks. Recovery never converts
ownership. Generic child recovery after the parent ends is refused, except for
already-paid recovery behind a pending child Stop. Message orchestrator appears
only for a running-lineage lock, not a merge or isolation refusal. Add turns
refuses while a lineage child has a human turn. It then relocks composers.
Session and stage exclusion still prevents overlap.

The chain's orchestrator chat has the same lock. Its chat id equals the root
episode id and branch id. After ending, project-chat turns on that branch resume the newest
orchestrator session and stage. If they start fresh, they give an explicit reason:
unavailability, a provider or machine change, or a dropped session. Add turns
refuses during such a turn. Viewer Send continues it as Work.

Lifecycle notices are deduplicated RCP facts committed with source transitions.
They cover child settlement and recovery, Experiment attention and ending, and
graph readiness. Busy actors receive them after their turn. Sleeping delivery
atomically claims a bounded batch and one invocation from B. Root graph wakes
also claim pending input, with a short grace window to batch notices. Running
roots harvest mail and notices transactionally with turn attribution. Responses
expose nonzero pending counts. Large inboxes require repeated harvests. Clear
refuses if its complete acknowledgment exceeds the bounds. Exhaustion retains
input without paid wakes.

Stop receipts record attribution to a human, orchestrator operation, or system.
Self-caused Stops and child failures from revoked login suppress wakes. Suppressed
notices neither hold ordinary mail nor block quiescence or ending. They remain
in harvest and ending receipts. A completed worker watcher group wakes that same
route and session with its pinned scope, profile, and recorded job observations.
This requires a running parent, an unfenced route, and a succeeded task. Child
settlement then notifies the root. Experiment agents may retire their own staged
conditions through the shared
[watcher stop contract](conversations-episodes-and-watchers.md#watcher-resources).
This neither cancels jobs nor widens human retire controls.

## Staged command client

The RCP-authored prefix exposes the resolved closed vocabulary from
[Auto-research commands](../../src/rcp/runs/auto_research.py). It includes validation,
keyed Apply, status, worker and Experiment lifecycle, star mail, conditions, fork
merge and archive, inbox harvest and clear, Bulletin while enabled, root ask,
guarded finish, a one-time self-name, and the graph history reads. Workers get
validation, status, message, and the same Bulletin and history verbs. Spawn and kickoff may choose the provider, model, and effort.
The root has no helper verbs. Child Work uses ordinary generic launch or direct
scheduler submission.

Continuations refresh the prefix, vocabulary, graph inputs, schema, validator,
and write boundary while retaining the assignment and work. Correction receives
fresh validate-only credentials. Keyed Apply returns the revision and refreshed
pointers. An unconsumed final Patch applies at settlement. Resume keeps the exact
session and stage. There is no agent Retry. Unusable children must stop, settle,
and be replaced.

Mutations persist keyed intent, file bytes and digest, and identity before effects.
Completed ok or invalid results replay exactly. Interrupted or unavailable effects
may only prove or resume the same intent. They never reread changed files or
invent a child. Starts and exits become task events. The broker for each invocation
authenticates the provider process and descendants on the host. It uses no
reusable credentials in prompts, the environment, stages, or arguments. This
establishes provenance within the cooperative account, not isolation from hostile
UIDs. Episode and signature binding remain despite shared broker mechanics.

Apply uses idempotent branch transitions. Finish is a pure guard. It returns an
immutable, complete blocker receipt for unfinished children, undelivered notices,
or accepted admissions not yet reflected in state. Waiting workers return
waiting-work plus a stop command. Finish never silently cleans up blockers.

## Orchestrator questions

Only the root may use keyed ask. Workers mail the root. The question captures the
exact episode, operation, session, stage, scope, and target. The same owner and
key read the current state. Ask parks the root's turn while workers continue.
Open cards add needs-action health without changing admission or status.

Each answer revision creates one hearsay-only mail with the full answer and
choices. Mail wakes spend B or defer and harvest normally. Reconciliation repairs
insertion. Answers grant no approval, scope, or authority. They grant no budget
except explicitly named turns on an ended episode's last open card.

Launch stages bounded open questions, undelivered dismissals, and claimed answers,
never transcripts. Successful turns acknowledge dismissals. Failures retain them.
Dismissal neither wakes nor mails. Stop withdraws read-only cards. Other endings
hold answers. The last open card authorizes continuation turns. Any continuation
atomically claims held or undelivered answers up to the cap with its opening
notice. A failed start reopens answered cards. Ancestor cards keep their original
bindings. The root and session-start Retry expose ask. Workers do not.

## Completion, Stop, and report

Completion requires keyed finish. A settled child, Blocker, contention, or
human-started downstream Experiment is not completion while the root can act,
delegate, or arrange an observable continuation. Human pauses name the missing
judgment, credential, privilege, approval, or coordination. Use actual compute
routes. Never invent schedulers or repeat uncertain submissions.

Exhaustion or an ending other than Stop fences new work, settles admitted children,
and enters [visual wrap-up](conversations-episodes-and-watchers.md#visual-wrap-up)
on the bound branch from an immutable bounded receipt. That owner defines session
continuation and the no-session exception. Stop skips it.
Permanent admission defects settle with a nonblocking report error. Sign-in blocks
park without spend. Transient failures retry with the receipt. Unclassified or
non-transient failures settle after three repeats per process, with deduplicated
warnings. Failed reports do not retry automatically. A continuation gets its own
ending and report.
**Why:** durable phase outcomes prevent deterministic failures from trapping wrap-up.

Add N turns creates a new linked episode with a new ceiling and grant. It uses
the same branch and exact native session. The source authorizer, ending, receipt,
and report remain immutable. Current-writer lookup follows the newest chain
member. Pending and running child Experiments move to it. Work routes, ordinary
input, and watchers stay on the source. Questions reopen without redelivery.
The opening wake claims a reauthorization notice.
**Why:** delayed old callbacks cannot mutate a reopened run.

Continuation requires a terminal source that has not yet continued and a bound
session and stage. It requires available isolation, no live turn or merge, no
competing branch-node episode, and no human-owned turn in a lineage child. Exact
request replay returns the same continuation. Stopped watchers stay stopped.
Absent bindings require a new run. Message orchestrator targets the newest member.
A running member gets mail. An ended member requires explicit N; the composer
defaults to 3, while the API has no default. Ending races refuse implicit turns.
Continuation and mail commit together. Replay matches the message, N, and member
ids. Concurrent losing sends become mail to the running winner without spending
their N.

Parent settlement and reporting wait for unfinished child turns and paid recovery
without extra E. Ended parents and exhausted E block new invocations. They retain
unconsumed watcher completion. A report snapshot predating child settlement gets
a visible, nonblocking error after settlement. Its receipt and attempts remain.
Reconciliation covers all episodes independently of display limits. Root Stop
atomically retires root and child watchers and fences child Experiments, including
late handoffs. Child Stop fences its route and watchers. Neither control cancels
compute jobs.

## Branch lifecycle and merge eligibility

Graph delivery requires a head newer than the base and no successful receipt for
that head. It also requires no queued, running, or pausing graph writer. Merge
itself is not a graph writer. Episode status, ending, wrap-up, and pause do not
gate delivery. Recovered and abandoned attempts are not live writers. Newer heads
may merge again. Code delivery is independent.
**Why:** report failure must not trap valid work. Stop ends work, and Merge delivers it.

Human members dispatch episode Merge from any owner-chain card without a budget.
Fully delivered, foreign, concurrent, or live-writer requests are refused with
specific reasons. Reservations fence work across the isolation binding through
cleanup. History is retained.

## Episode code merge and cleanup

Merge reserves the owner and persists an attempt before committing leftovers.
[Merge implementation](../../src/rcp/runs/episodes/merge.py) owns persisted phases.
Startup or the next Merge reconciles recorded commits separately from the immutable
binding. The human chooses a local target, defaulting to the starting branch,
with Git 2.38+. Missing targets and self-targets are refused. So are targets
checked out elsewhere, dirty target checkouts, unfinished Git operations, an
unmerged index, and dirty submodules. RCP commits ordinary episode leftovers with
`git add -A` under its fixed identity. It ignores ignored files. It never commits
shared changes, resets, stashes, force-pushes, or pushes.

The plan and `git merge-tree --write-tree` decide whether judgment is needed.
Deterministic landing builds from the tested tree. It fast-forwards a clean,
checked-out target or moves an unchecked-out target by compare-and-swap. Both
paths first recheck the source, target, and checkout identity. Code lands and
verifies before graph commit. A code conflict, graph answers, or confirmed
unfinished jobs start one task in chat Integrate's local-merge scope; the task
stops the confirmed jobs first. The landing must contain both recorded
commits. Code-only delivery targets main without a graph side. If there are no
graph answers, subsequent graph delivery runs without an agent. A missing landing
fails, and the next Merge starts fresh. Later graph failure never undoes landed code.

Default history is a merge commit. Squash is available only without an agent.
It records its commit and forbids Keep branch open. Verification proves source
ancestry or the recorded squash. Code and graph delivery records are separate.
**Why:** deterministic tested trees need no paid judgment. Only the episode owns
its leftovers, and shared files and the index must remain intact.

Cleanup independently retries worktree removal, code-branch deletion, and graph
archive. Merge keeps the worktree while a confirmed job still reads unfinished,
skips branch deletion while the worktree is kept, and skips graph archive after
any kept worktree or failed step, so a later Merge finishes them. Branch deletion requires a removed worktree and verified delivery or
squash. Keep branch open skips cleanup. Discard or archive before delivery needs
confirmation. Live turns block removal. Live or unobservable jobs need human
confirmation. Cleanup failure does not turn delivery into failure.

## Semantic rebase and merge

RCP builds the Patch. Agents never write merge operations. Loading captures the
exact base, source, and destination, canonical human facts, earlier scoped reviews,
and live dispatch authority. Truth membership is pinned. A change requires new
dispatch. Repository read scope is empty.

[The planner](../../src/rcp/runs/merge_plan.py) compares whole fields with a declared
content role. Engine and identity fields never diff. One-sided changes win.
Identical changes agree. Different changes to the same field conflict. Removal
conflicts with edits, new references, or new or edited incident edges. These are
presence conflicts. Re-merge uses the last delivered source. Still-pending review
fields compare against the current destination, replacing outstanding and new
effects together. The loader derives outstanding changes from canonical filing
snapshots without replaying historical operations on live state. Stale reviews
contribute nothing. Source reversion can withdraw without re-filing. Rejection
never resurrects work. Approved work yields to later destination judgment.
**Why:** declared model roles keep merge semantics aligned with core.

Core classifies changes as direct, reviewable, or refused. One Proposal per group
connected by approval dependencies or effects prevents approvals from making each
other stale. Planning, answered splitting, and filing share connectivity until
the footprints stabilize. [Patch construction](../../src/rcp/runs/merge_patch.py)
uses core-accepted sequences for Decisions, retirement, and edge replacement.
Carried status retains its recorded cause. Only canonical human facts keep a
human-edit cause. If the source cause changed or was removed, the merge uses the
newest carried Evidence edge into that Hypothesis. If no such edge is available,
it refuses for branch repair. Creation retains all writable content, defaults, and the selected
Decision lifecycle. Overlapping source Proposals join review groups. Independent
ones retain their identity and content with new destination filing stamps.

Agents write only conflict answers and review prose in `answers.json`. Answers
choose the branch value, destination value, merged value, or presence. Status and
standing answers must pick a side. `merge-answers check` runs commit preparation
and binds the receipt to bytes, plan, and heads. Settlement requires a passing
receipt for the collected bytes. That receipt overrides diagnostics from other
attempted files. Coherence clashes become node questions once per group. Repeated
clashes return through check. Construction or footprint errors that depend on
answers name conflict ids. Hard authority errors and refusals remain terminal.
Without conflicts or cards, there is no provider. Pairwise merges always use the
current destination.

The merge task has the orchestrator profile under dispatch authority and scratch.
Only for code landing does it also receive the resolved worktree or shared-checkout
roots. It gains no approval, membership, ontology, configuration, server, or general
branch authority. One merge commits one destination transition or none. A changed
head rebuilds and prepares again. It never overwrites or concatenates history.
Failed and paused attempts allow fresh dispatch. Canonical provenance and the
reconciled receipt bind the source, base, head, prepared destination, result, task,
dispatcher, and time. Crash recovery cannot deliver the same head twice. Main
watchers see a successful main delivery once.

[Merge effects](../../src/rcp/runs/merge_effects.py) verifies the canonical receipt
before carrying eligible Evidence findings and rebuilding the main display
snapshot, including attention. Agentless and no-change deliveries use the same
settlement. Durable, uncapped effect receipts keep each step independently owed
until it succeeds; task success alone is not publication. Startup and background
reconciliation retry unfinished effects without redelivering the graph or changing
the task verdict. A terminal task without graph delivery closes its effect
obligation only after locked canonical non-delivery proof. Legacy recovery considers the latest committed delivery per
source/destination and only findings created before it. Superseded legacy deliveries
cannot publish old concerns. Finding eligibility and dismissal semantics belong to
[Evidence to check](graph-consolidation-and-lessons.md#evidence-to-check).

## Runs projection

Child and task state support one parent health and recommendation. Detail shows
the branch base and head, merge state, tasks, report, and error. Backend `can_merge`
covers graph delivery, code delivery, cleanup, and settled-attempt reconciliation.
Delivered exact graph heads alone offer no Merge. Newer heads return to unmerged.
Zero semantic preview counts still require a no-change receipt. Graph-only
requests for already-delivered or unchanged heads are refused.

Preview counts changes, commits, conflicts, and Proposals. It selects the target,
eligible merge or squash, cleanup, and Keep branch open. Squash cannot keep the
branch open. Submission rechecks state. Failures mark the same Merge action red
with the reason in a tooltip. Clicking it gets a fresh preview. There is no
separate Retry. An in-flight request or missing preview disables submission.
[Picker behavior](api-web-and-desktop-projections.md#project-graph-ref-inventory)
uses the shared human dispatch path. [The API contract](api-web-and-desktop-projections.md#episode-merge-api) owns
request identity and accepted-task replay.
Open graph shows the persistent branch. Changes compares the base and head with
neighbouring context, creations, updates, removals, before and after values,
provenance, and task filtering. Lens switches never change edit or picker targets.
Removed objects have only history.

Branch edits, Sync, Inbox, Discuss, Work, and standalone Experiments remain
available during and after episodes under their own human authority, session,
and budget. Only episode work gets episode provenance. Recovery and wakes preserve
targets, and merge fences writers. Drafts, selection, viewport, requests, and chats
are keyed by project and target even at equal revisions. Missing or foreign
targets never fall back to main. The
[branch picker](api-web-and-desktop-projections.md#project-graph-ref-inventory)
owns the shared view target. Report lifecycle versions remain fixed while artifact
edits add versions. Root artifact comments remain in the thread without budget
spend. Current exclusions belong to [design](../design.md).

### Browser preference in Auto-research

Human launch sets `browser_requested`. Continuation, retry, wakes, and children
retain it. Commands cannot grant access. Each child keeps a stable stage for
recovery and wakes. Projections show the launch preference and durable per-turn status.
