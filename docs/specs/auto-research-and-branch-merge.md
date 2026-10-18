# Auto-research and branch merge

This spec owns project-wide orchestration, budgets, children, mail, staged
commands, episode graph branches, and human-dispatched merge to main.

## Episode scope, budget, and authority

One human action starts project-wide Auto-research. Each project has one
orchestrator profile; each graph branch has at most one live Auto-research
episode, so several may run at once on separate branches. Status lists the others
as `other_auto_research` (capped `{rows, omitted}` of episode, target, starter, and
checkout), and the start dialog shows them; neither gates a start. An optional
instruction guides the first invocation without granting authority. Human start
pins provider, model, and reasoning, defaulting to that profile. Child picks
use the orchestrator's explicit choice or the Settings node_chat profile, always
on the child's profile machine. Unsupported choices refuse before admission;
prompts list the same probed readiness choices.
**Why:** each episode's graph writes stay on its own branch; overlap in a shared
checkout is for humans to coordinate, as with Experiment loops below.

Start and reauthorization resolve the execution machine without gating on compute
readiness. The helper probes on use; Settings shows the stored probe. See
[Compute jobs](compute-jobs.md) for setup and human Cancel.

The brakes are the human's invocation ceiling **B** (defaulted from Settings)
and [protected-belief authority](authority-and-proposals.md#graph-action-vocabulary),
not a subtree fence. The root has the orchestrator profile on its branch;
children have the ordinary profile. Neither approves Proposals. The root decides
choices its evidence settles. It queues `ready` only for human preference, risk,
cost, or an unspecified direction, naming the needed judgment in `rationale`.
Waiting for an unannounced `ready` Decision must not be its only way forward.

Every root/worker turn, mail or graph-condition wake, and operational continuation
spends one B. Exact recovery spends none and restages durable claimed mail and
lifecycle input, replacing failed-attempt copies. It includes input harvested by
any attempt of that allocation; cleared input stays unread. Oversized recovery
input stages the oldest items that fit and logs the dropped count. At exhaustion,
current turns finish but new work cannot start.

The shared child-Experiment allowance is **E = 5 × B**. Each actual child
invocation spends one E and one unit of its pinned ceiling. Sleeping reserves
nothing; exact recovery spends nothing. The orchestrator cannot widen E.

## Workers and child Experiments

An ordinary Work worker may sit on any existing node. Its instruction defines
the result that ends its job; the seat gives context, not subtree authority.
It uses its exact child repository scope and parent branch. Direct scheduler
submission and generic `launch` both follow ordinary shell watcher validation,
correction, and arming. A helper still running from this turn or its recovery
lineage requires the returned shell watcher at settlement.

A route is waiting when its current task succeeded and has an armed undelivered
watcher. This derives from route/task/watcher rows, not a new task state. Root
status distinguishes waiting, running, and settled workers.

At most one live Experiment loop exists per node **per graph target**. Kickoff
uses normal readiness on the root's branch. The orchestrator never stops, adopts,
or waits on a loop it did not start. Other-target loops do not block kickoff:
`live_elsewhere` in its result and `other_branch_loops` in status use capped
`{rows, omitted}` lists of node, episode, target, starter, state, and checkout.
Agents ask the human when work could interfere; a child without `ask` pauses
that work and reports to its orchestrator. Restarting its own child requires
Stop, settlement, then kickoff.
**Why:** branch isolation forbids one branch stopping another's loop. Visibility
lets humans coordinate legitimate variants without a project-wide start gate.

Child admission, budget spend, parent registration, target, and lifecycle route
commit atomically. Recovery dispatches the same queued id without another spend.

## Branch names

Each root branch has one project-unique name: 2–32 lowercase letters, digits,
and hyphens, with alphanumeric ends, never `main`. Retired and archived names
stay reserved. Both episode kinds accept `branch_name`; an Experiment may name
one only when creating an isolated branch. Invalid/taken names return
`branch_name_invalid` (422) / `branch_name_taken` (409). Name and episode
reservation share one SQLite transaction.

Blank names become `research-<mon>-<day>` or `experiment-<mon>-<day>` using the
root's UTC date, lowercase English month, and two-digit day; clashes get `-2`,
`-3`, in creation order. Legacy read-time fallbacks need no migration and cannot
reuse reservations. Rename preserves other fallback names. One storage owner
resolves names; transfer carries current and retired reservations.

Any member may rename, archive, or unarchive through graph-ref routes; these
are operational labels, not Patches. Every surface uses the shared name. The
project-wide branch picker and tagged project-wide Runs/Artifacts follow the
[graph-ref projection](api-web-and-desktop-projections.md#project-graph-ref-inventory).
**Why:** one named object and selector prevent screens inventing incompatible
labels or showing different targets. Reserving old names keeps historical tags
unambiguous.

## Persistent graph branch

An Auto-research root or main-target Experiment with graph isolation owns one
persistent branch, through the same creation owner. Its episode id is the branch
id. Before provider launch, RCP captures a coherent main head, creates/reconciles
canonical metadata, stores that base in SQLite, and proves the binding. Startup
repairs a one-sided crash only by restoring that exact binding; it otherwise
fails explicitly, never redirects to main.

[History and graph targets](graph-history-and-transitions.md#append-only-history-and-graph-targets)
own metadata and replay. Main movement and successful merge never rewrite a
branch base or log; continuation episodes share the root's branch.

## Exact branch targeting

Every descendant graph path uses the exact branch: context, validation, Apply,
correction, continuation, child control and repair, watchers/conditions, lifecycle
reconciliation, settlement/report inputs, and merge preparation. Task, episode,
watcher, stage, session, command, and control rows carry it. Main/branch event
watermarks stay independent; sessions cannot switch targets on resume.

Graph/synthesis pointers resolve in that branch even remotely. Shared inputs such
as the paper introduction retain project-owned paths. Child Experiment Patches
keep the child's episode id; admission checks the task target, not equality with
the branch owner. Branch writes advance only branch graph/control/guidance/events;
ordinary human and agent work may keep advancing main.

## Forks

A fork is a graph branch with its own UUID4 id, unlike an episode branch whose id
is its episode id, and an exact parent head (`main@R` or `branch:<id>@R`) fixed
at creation. Its owner is an episode lineage (the chain root) or, for a member's
fork, no episode. A lineage owns its persistent branch and every fork it created.
[History and graph targets](graph-history-and-transitions.md#append-only-history-and-graph-targets)
own metadata and replay.

The orchestrator places children with `spawn` and `episode --kick-off-experiment`.
`--at <ref@R>` forks any project ref, resolved once to an exact head; a bare `@R`
names the root's branch. `--target <branch>` (id or name) uses an open
lineage-owned branch; archived, foreign, and main targets refuse with
`child_target_refused`. Omitting both keeps the root's branch. `spawn --project`
and `--seat-node` choose the seat, never the target. `--seed-file <file>` requires
`--at` and must hold at least one operation valid under the orchestrator profile.
Fork reservation, seed snapshot, child admission, and budget spend commit in one
command start. RCP then publishes the fork and commits the seed as its first
Patch before the child launches; retry re-drives that recorded identity, never
a new fork.

A run whose target is a fork reads graph history only on the fork and its
ancestors, each through the revision it was forked at; other runs read main and
every branch. The prompt renders that same scope, and reading grants no writes.
The loop-overlap builder takes the same scope, so such a run is shown no live
loops on other branches; human views stay unfiltered.

An orchestrator fork archives automatically when its head still equals its parent
head and no writer can reach it: no unsettled task using it as graph target or
bound merge destination, no publication of unknown outcome, no undelivered watcher
or worker mail, no live child Experiment, and no accepted admission targeting it.
The child poll rechecks this under the canonical write lock. `branch --archive <id>`
archives an owned fork, never the root's own branch. Member forks never auto-archive.

`merge --key K <fork>` delivers an owned fork into its parent when the lineage owns
both and the parent is open. A fork of main or of a foreign branch has no owned
destination (`merge_main_requires_human`, `merge_branch_not_owned`). The
[semantic merge](#semantic-rebase-and-merge) runs against the parent: empty residue
commits without a provider; residue spends one lineage invocation, reserved at
admission or when the parent's advance first creates residue, and refuses with
`merge_budget_exhausted`. Protected beliefs arrive as pending Proposals on the
parent. The root must be running without ending or Stop; a delivered head, a live
source writer, or another merge into that parent refuses. Merge does not archive.
**Why:** invariant 3 lets the orchestrator merge only within its own lineage; main
truth changes only by human dispatch.

Any member may fork an explicit `main@R` or `branch:<id>@R` with
`POST /api/projects/{project_id}/graph-refs` (`{request_id, at, name?}`; the
UUID4 `request_id` becomes the branch id) and deliver any fork, member or
lineage, into its parent with `POST .../graph-refs/{branch_id}/merge`
(`{request_id}`), without an episode or budget; the branch picker offers Merge on
each open, merge-eligible fork. An episode branch refuses
(`merge_source_is_episode_branch`) and delivers through episode Merge. A member
merge carries no episode attribution, even out of a lineage fork. **Why:** a
lineage fork of main, or of a branch the lineage does not own, has no owned
destination, so only a member can deliver it. A member fork has no owner episode,
so the first Experiment on it creates the isolation binding that later
Experiments there share.

## Graph and code isolation

Auto-research always isolates its graph. An omitted code-isolation choice resolves
on only with one repository, Git 2.38+, and no writable grant overlapping the shared
checkout; otherwise off. Explicit ineligible choices refuse. The root owns the
optional worktree. Children, continuations, and human-started Experiments on that
branch share its immutable
[isolation binding](conversations-episodes-and-watchers.md#episode-isolation).
With code isolation off they retain ordinary repository scope. Neither choice
changes truth membership or rolls back repository effects.

Graph merge does not copy repository files. Human Merge coordinates separate code
and graph delivery through the isolation owner; graph history persists afterward.

## Mail and lifecycle notices

Mail is Markdown hearsay in a star: root to its spawned worker's stable id, worker
to root. It grants no graph authority. Live steering remains the separate
[ordinary human chat channel](providers-and-containment.md#live-human-steering),
never episode-worker or agent-to-agent live injection.

While any continuation-chain member runs, including sleep/wrap-up, child composers
point to **Message orchestrator** (`auto_research_child_read_only`). After the
lineage ends (`completed`, `failed`, `needs_action`, `stopped`), ordinary human turns
are admitted only if any child Experiment also ended and isolation remains
available. `removing`/`removed` isolation keeps `episode_isolation_unavailable`.
The sender authorizes these Discuss/Work turns outside episode budgets. Wakes,
question follow-ups, Resume, Retry, and Repair use the same ownership fence;
artifact comments remain allowed.

Episode turns use episode master keys; human turns use ordinary chat masters.
Switching owner reopens that owner's master, including retained Patch/recovery
checks. Episode recovery never converts to human-owned work. The store's
`episode_child_recovery_refusal` blocks generic child Work Retry/Resume and child
Experiment recovery after the parent ended, except already-paid recovery behind
the child's pending Stop. Projections and transport retry use that same rule.
**Message orchestrator** appears only for a running-lineage lock, not isolation
or merge refusal. Add N turns refuses while a human turn is active in a lineage
child, naming the chat; admission locks composers again. Session/stage exclusion
still prevents overlap.

Lifecycle notices are RCP facts: child settlement/recovery, child Experiment
attention/ending, and graph readiness. Source transition and deduplicated notice
commit together. Busy actors receive input after their turn. Sleeping delivery
atomically claims a bounded batch and one B allocation; a root graph wake also
claims pending root mail/notices. A short grace window batches lifecycle wakes.
A running root may harvest both mail and notices transactionally, attributing
consumption to its turn. Other command responses include nonzero `inbox_pending`
counts. Large inboxes need multiple harvests; clear refuses if its complete compact
acknowledgment would exceed bounds. Exhaustion retains input without paid wakes.

Stop records `human:<member>`, `orchestrator:<operation>`, or `system:<reason>`
with its fence. Root-requested Stop notices carry `wake_suppressed=self_caused`;
revoked-login child failures use `provider_auth`. Suppressed notices never buy a
wake, hold ordinary mail, or block quiescence/ending; harvest and ending receipts
still include them.

A completed worker watcher group wakes the same route/session, not the root.
Its atomic claim binds one continuation and spends one B, requiring a running
parent, an unfenced route, and a succeeded task. Exhaustion leaves completion
pending, even after the exhaustion fence. The wake retains pinned scope and
watcher model/reasoning/skills, with job identity, result, times, duration, logs,
backend, and shell observations. Child settlement then notifies the root.

Experiment agents may retire their own staged graph conditions through the same
atomic `external` stop list as external observers; see
[watcher resources](conversations-episodes-and-watchers.md#watcher-resources).
**Why:** an obsolete condition otherwise holds work open and spends a wake nobody
needs. Retirement cancels no job and does not widen the separate human retire
control, which excludes Experiment graph conditions.

## Staged command client

The exact RCP-authored prefix exposes validation/keyed Apply, status, keyed worker
spawn and pause/resume/stop, star mail, graph-condition registration, child
Experiment kickoff/stop/exact-resume, keyed fork merge and fork archive (see
[Forks](#forks)), inbox harvest/clear, ask, and guarded finish.
Spawn/kickoff may select provider/model/effort. Root commands expose no compute
helper verbs; child Work uses `launch` on the generic route or direct scheduler
submission. Validation/reply policy remains surface-specific.

Continuations refresh prefix, vocabulary, graph inputs, schema, validator, and
write boundary while retaining assignment/work. Correction gets a fresh
validate-only credential, not earlier Apply/coordination commands. Keyed Apply
returns the revision and refreshed pointers within the turn; a final unconsumed
`patch.json` applies at settlement. Resume uses the exact saved session/stage;
there is no agent Retry. An unusable child must be stopped, settled, and replaced.

Mutations require an idempotency key. RCP persists intent, input file bytes/digest,
and identity before effects. Completed `ok`/`invalid` results replay exactly;
interrupted/`unavailable` effects may only prove or resume the same intent, never
reread changed bytes or invent another child. Starts/exits enter task events.

The per-invocation broker authenticates the provider process or live descendants
on its host without reusable bearer credentials in prompt, environment, stage,
or arguments. It guards provenance within the cooperative execution-account
model, not hostile same-UID processes. Episode id/signature remain bound even
though ordinary Work and Experiment turns share the broker mechanism.

Apply uses the branch transition manager with idempotent effect identity.
Guarded finish is a pure transition: unfinished children, undelivered notices,
and accepted-but-unreflected admissions yield a complete immutable blocker
receipt. A waiting worker yields `waiting_work` and `stop --key <key> <worker_id>`.
Finish never silently cleans up blockers.

## Orchestrator questions

Only the root may keyed `ask`; workers mail it. RCP captures episode, operation,
session, stage, scope, and target. Repeating the same owner/key reads current
state. `ask` returns `parked`; the root ends its turn while workers continue.
Open questions add `needs_action` health without changing episode admission/status.

An answer creates one hearsay-only human mail per question/revision, with full
answer and selected choices. Normal mail wakes spend B or defer while busy;
harvest can consume it in-turn. Reconciliation repairs interrupted insertion.
Answers grant no approval, scope, authority, or budget.

Each root launch stages a bounded snapshot of open questions, undelivered
dismissals, and claimed answers, never transcripts. Successful turns acknowledge
dismissals; failed launches retain them. Dismissal never wakes or mails. Endings
withdraw cards read-only; continuations reopen ancestor cards with their original
bindings. Root and session-start Retry expose `ask`; workers do not.

## Completion, Stop, and report

Completion requires explicit idempotent `finish`. A settled child, open Blocker,
resource contention, or human-started downstream Experiment is not completion if
the root can still act, delegate, or arrange an observable continuation. Pausing
for a human must name the missing judgment, credential, privileged action,
approval, or coordination. Use the actual scheduler/process route; never invent
a scheduler or repeat uncertain submissions.

Exhaustion/non-Stop ending fences new work, settles admitted children, and runs
the common visual report in the exact branch session using one immutable bounded
receipt. Human Stop skips the report. Report reconciliation records failures:
permanent admission defects settle with a nonblocking report error; sign-in
blockages park pending without spending an attempt; transient failures retry with
a diagnostic receipt. Unclassified admission or non-transient launch failures
settle after three repeats per process. Warnings are deduplicated per episode and
failure kind. Failed reports do not auto-retry; a later continuation has its own
ending/report.
**Why:** logging a deterministic failure forever can leave an episode stuck in
wrap-up. Durable phase-specific outcomes keep its controls usable.

**Add N turns** creates a new episode linked by `continues_episode_id`, using the
same branch and exact native session. N is the new ceiling; the new member grant
is recorded without changing the source authorizer, ending, receipt, or report.
The root id remains `branch_id`; current-writer lookups resolve the newest chain
member. Pending/running child Experiment routes move to it; child Work routes,
ordinary mail/notices, and watchers stay on the source. Undelivered question
answers follow reopened questions, without redelivering old answers. Its opening
`lifecycle_wake` claims a `reauthorized` notice naming source and ceiling.
**Why:** a new record preserves one ending per episode and prevents delayed old
callbacks mutating a reopened run, while retaining branch work and session memory.

`POST .../episodes/{id}/continue` takes `invocation_ceiling` and `request_id` for
both episode kinds, including stopped sources. `can_continue` requires bound
session/stage. Admission refuses nonterminal/already-continued sources, live turns,
active merge, a competing live branch/node-target episode, removed isolation, or
an active human-owned lineage-child turn. Exact request replay returns the same
continuation. Stopped watchers stay stopped; missing bindings require a new run.

**Message orchestrator** targets the newest member. Running members get ordinary
mail; ended members require explicit N to authorize continuation with that message.
The composer defaults N to 3 and sends it; the API has no N default. Ordinary mail
racing with ending refuses rather than silently granting turns. Continuation and
sender mail are created in one transaction and claimed with the opening notice.
Replay requires matching message, N, and member ids, not display name. Concurrent
losing sends become mail to the still-running winner; their N is unused. Refusals
carry codes and disable the composer until admission is available.

Parent settlement/report launch waits for unfinished child Experiment turns and
exact paid recovery without another E spend. Ended parents or exhausted E prevent
new invocations and leave watcher completion unconsumed. A report allocation whose
snapshot predates child settlement ends with a visible nonblocking error once the
child settles, preserving its receipt/attempts. Reconciliation scans every episode,
independent of recent-history display limits.

Episode Stop atomically retires root/child watchers and fences live child
Experiments, including late handoffs. Child `stop` fences its route and retires
watchers. Neither can wake again or claim to cancel compute jobs.

## Branch lifecycle and merge eligibility

Graph delivery requires an exact head newer than base, no successful receipt for
that head, and no queued/running/pausing graph writer (`branch_merge` is not a
writer). Episode status, ending, wrap-up, quiescence, and root pause are irrelevant.
Stop ends work; Merge delivers it. A newer head may merge again; recovered or
explicitly abandoned attempts are not live writers. Code delivery is independent,
so code-only owners or delivered graphs with newer code can still Merge.
**Why:** a report failure must not trap valid branch work. The human chooses when
a head is ready; merge does not end the episode.

Only a human member dispatches Merge, from any owner-chain card, without episode
budget spend. Fully delivered, foreign, concurrent, and live-writer requests refuse,
naming the writer when applicable. An active reservation fences new work on the
entire isolation binding through cleanup; branch history is never deleted.

## Episode code merge and cleanup

Merge reserves the owner and persists an attempt before committing leftovers.
Phases are `pre_merge`, `landing`, `agent_merging`, `verified`, `graph_committed`,
`cleanup`, and `done`; next Merge/startup reconciles interruption against recorded
commits. The immutable binding and mutable attempts are separate.

The human chooses a local target, defaulting to the starting branch, with Git
2.38+. Missing/self targets, targets checked out outside the shared checkout,
a dirty shared checkout holding the target, unfinished merge/rebase/cherry-pick,
unmerged index, and dirty submodules refuse. RCP commits only ordinary episode
leftovers with `git add -A`, excluding ignored files, under a fixed RCP identity.
It never commits shared-checkout changes, resets, stashes, force-pushes, or pushes.

The graph builder and `git merge-tree --write-tree` classify residue. With none,
RCP builds the landing commit from the tested tree: a clean checked-out target
fast-forwards in one step; an unchecked-out target moves by compare-and-swap.
Source/target commits and checkout identity are rechecked before landing. Code
lands and is verified before the graph transition.

Either code conflict or graph residue starts one merge task in chat Integrate's
local-merge scope. RCP verifies its landing contains both recorded source and
target commits before graph commit. With no graph residue, the code turn is
followed by agentless graph delivery. Code-only tasks target main with no graph
side. Missing landing fails before graph commit; next Merge starts a fresh
attempt. A later graph failure never undoes landed code.

Default history is a merge commit (`--no-ff`). Squash requires agentless delivery,
records its resulting commit, and forbids Keep branch open. Verification proves
source ancestry or recorded squash. Code and graph have separate delivery records.
**Why:** clean merges need no paid judgment. Testing the tree before moving refs
and preserving checked-out files/index make deterministic landing safe; only the
episode owns its leftovers.

Cleanup records independently retryable steps: remove worktree, delete code branch,
archive graph branch. Deletion requires worktree removal and verified delivery or
recorded squash. Archive preserves Patches; Keep branch open skips cleanup.
Discard/archive before delivery needs confirmation. Live turns block removal;
live or unobservable jobs pause it until human confirmation. Cleanup failure does
not change delivery into failure.

## Semantic rebase and merge

Merge context contains immutable base, exact branch/main heads and graphs, typed
base-to-branch delta, bounded Patch summaries/provenance, canonical human actions,
delivered heads, schema/validator, and deterministic conflicts. Dispatch pins main's
current truth membership; `repositories_read` is empty. Changed membership requires
new dispatch; out-of-membership provenance refuses.

The deterministic builder emits authorable non-conflicting node creations/ordinary
updates and legal edge creates/removes/replacements. Replacement overlays changed
branch fields on main, preserving compatible main edits; validation checks net
changes and both operations' authority. It cannot leave an edge missing or smuggle
unrelated edits. Already-present values are omitted. Protected changes, conflicting
nodes, Decision outcomes, removals, and source Proposals remain in agent residue,
keeping coupled fields together.

Ordinary edits reset standing as on the branch. A mere main acceptance does not
defer them; agreed stronger branch/main standing is restored. A human standing
change on main since fork is deferred for explicit judgment. Each residue path's
reason comes from the same mapping rendered in the contract. Built operations are
staged as a file. Guidance text merges, but backend freshness flags do not: main's
transition recomputes validity from merged dependencies.

Empty residue commits without a provider. Otherwise the agent writes residue only;
RCP prepends built operations for validation and commit. Unsupported configuration,
invalid fixed operations, and mandatory source Proposals with out-of-scope provenance
fail before launch, outside an unrepairable correction loop.
**Why:** deterministic paths already have a specified result; the agent is needed
only for unresolved judgment. Agentless still requires human dispatch. Merges are
pairwise against current main, never a batch of branches.

The merge agent has the orchestrator graph profile under the dispatching human.
It gets scratch and, only for code landing, the episode worktree/shared-checkout
roots rendered from the resolved scope. It gains no membership, ontology,
configuration, approval, server, or general branch authority. The combined Patch
commits one main transition or nothing; conflicts use bounded same-session
correction, not a manual node-conflict viewer.

Legal direct changes may accompany protected main Proposals. Branch human judgment
does not let the agent approve on main: its Inbox reviews again. Exact canonical
human initiating operations prove special status/standing causes, never prose or
inferred intent. The [Proposal rules](authority-and-proposals.md#protected-belief-proposals)
allow coupled content/status/standing review and accepted ordinary-node removal.
Every effect must match branch delta without unrelated changes.

Live pending source Proposals retain identity/content. Resolved/stale ones remain
branch history; their resulting graph changes are represented separately. Proposal
identity is deterministic from branch, semantic operations, and last delivered
head (base before first merge). Validated receipts and snapshots suppress unchanged
re-proposals even after rejection on main; genuinely new source changes still
require review. Unsupported ontology, human-only ordinary extension edits, and
mutually invalidating review combinations fail explicitly without partial commit.

Append compares prepared main head. If it advanced, RCP rebuilds/re-prepares;
it never overwrites or concatenates history. Paused/failed attempts leave histories
unchanged and allow fresh human dispatch. Commit provenance and reconciled receipt
bind source branch/episode/base/head, prepared main, resulting revision/transition,
task, dispatcher, and time. Crash recovery cannot merge the same head twice.
Only main watchers see the successful main transition, exactly once.

## Runs projection

The parent has one health/recommendation; child/task states support it. Detail
shows branch name/base/head, merge state (`unmerged`, `merging`,
`merged-through-head`, `needs-action`, `failed`), task/correction history, and
report/error. Paused/interrupted merge tasks retain diagnostics and offer fresh
Merge when eligible.

Merge remains visible when ineligible. Preview counts graph changes, commits,
conflicts, and Proposals. Its pickers choose target, merge/squash when eligible,
worktree/code-branch removal, and Keep branch open (not with squash). Submission
rechecks server state and presents the specific refusal using the
[visual rules](interface-and-visual-design.md). Only an in-flight action or missing
preview disables it.

**Open graph** opens the persistent branch. Changes compares base to head with
neighboring context, create/update/remove markers, before/after content, provenance,
and a task filter. Context/full-graph switches change only the lens; editing and
pickers always use the full graph. Removed objects have history, not node controls.

Branches expose ordinary edits, Sync, Inbox, and Discuss/Work during and after
an episode. These human-authorized conversations and standalone Experiments own
their sessions/stages, not the root's budget or route. Only episode-owned work
gets episode provenance. Recovery, steering, and wakes preserve exact target;
main/branch chats are separate, and merge fences writers.

Drafts, selection, requests, viewport, and chat state key by project and target,
even at equal revisions. Missing/foreign branches fail without substituting main.
The [project branch picker](api-web-and-desktop-projections.md#project-graph-ref-inventory)
controls the shared view target. Reports retain original lifecycle versions;
artifact edits create later versions without changing graph/lifecycle provenance.
Root artifact comments stay in its thread without budget spend.

Branches are episode-created and merge only on human dispatch. General branch
creation, cherry-pick, conflict editing, and repository rollback are not current
features. A version-control model remains a possible direction, not a permanent
exclusion or authority exception.

### Browser preference in Auto-research

Human launch sets `browser_requested`. Continue, retry, wakes, and children
preserve it; command payloads cannot grant browser access. Each child retains its
own stable stage across recovery/wakes. API projects launch preference and each
turn's durable browser status.
