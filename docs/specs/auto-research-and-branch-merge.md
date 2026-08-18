# Auto-research and branch merge

This specification owns Auto-research orchestration, project-wide budgets,
children, mail, the staged command client, persistent episode graph branches,
and human-dispatched semantic merge to main.

## Episode scope, budget, and authority

One human action starts one Auto-research episode for the whole project. Exactly
one project-owned orchestrator profile and one live Auto-research episode exist
per project. The optional human instruction guides the first paid invocation but
grants no authority.

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

The orchestrator may seat an ordinary Work worker only on an Experiment or
Blocker, both of which have mechanically recognizable operational exits. Seating
selects context and accountability, not a second graph-authority subtree. The
worker's repository scope is the exact child run scope and its graph target is
the parent Auto-research branch.

One project-global live Experiment-loop episode may exist per Experiment. An
orchestrator kickoff reuses normal readiness and, if a loop already exists,
requests a graceful replacement through the same durable Stop path. It never
overlaps loops or hard-kills the current turn. A pending replacement snapshots
its goal and admission and starts only after settlement and fresh readiness.

Child task/episode admission, budget spend, parent registration, graph target,
and lifecycle routing commit atomically. Recovery dispatches an accepted queued
child without creating another id or spending again.

## Persistent graph branch

Every Auto-research episode owns one persistent canonical graph branch. The
episode id is its stable branch identity. Before any provider launch, RCP:

1. reads one coherent main head;
2. creates or reconciles branch metadata in the canonical state repository;
3. stores the same immutable main base in SQLite episode binding; and
4. proves the episode-to-branch binding.

A crash may leave an orphan on one side of the canonical/SQLite boundary, but
startup reconciliation either restores the exact binding or fails explicitly.
RCP never launches an unbranched episode and never redirects it to main.

The canonical branch record contains project, episode, kind `auto_research`,
immutable main base head, authorizing human snapshot, creation time, append-only
branch Patch history, current head, and durable merge receipts. Branch revisions
are identified by branch id plus head; an integer alone is insufficient.

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

A branch Patch advances only branch graph, control, guidance, and events. It
does not change main revision, main materialization, main control, or ordinary
main watchers. Human Sync, ordinary Work, and unrelated project work may keep
advancing main while the episode runs.

## Graph-only boundary

The branch covers canonical research-graph state only. Auto-research repository
work uses the real project repositories under exact provider-native write
containment. Provider sessions, external jobs, artifacts, and files remain in
their ordinary locations.

There is no Git branch, worktree, repository rollback, branch discard, or
whole-project sandbox. Merge neither copies nor replays repository files. A
failed or merged graph branch persists as an audit trail even when its operational
work already changed a repository.

## Mail and lifecycle notices

Agent mail is star topology: the orchestrator may address workers it spawned,
and those workers may reply. The human messages the orchestrator, not a child.
Mail is Markdown hearsay and carries no graph authority; `patch.json` remains the
only graph channel.

RCP-authored lifecycle notices are separate authority facts: child settlement
or recovery, child Experiment attention/ending, graph-condition readiness, and
replacement progression. Source transition and deduplicated notice commit
together. A busy actor receives the notice after its current turn; nothing is
injected into a live provider process.

Sleeping-actor delivery claims a bounded notice batch atomically with one B
allocation. A running orchestrator may harvest or clear its inbox without a
separate wake. Budget exhaustion retains notices but cannot create an
unauthorized turn. Clear refuses before acknowledgment if even its compact full
response exceeds the bound.

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

There is no agent Retry verb. Resume means the exact saved session and stage;
an unusable binding tells the orchestrator to create the explicit replacement.

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

Apply uses the ordinary transition-manager path on the branch target, with
idempotent source effect identity and refreshed graph pointers. Guarded finish
is a pure state transition: it refuses with a complete immutable blocker receipt
while child work, replacements, undelivered notices, or accepted-unreflected
admissions remain. It never performs cleanup as a side effect of saying finish.

## Completion, Stop, and report

Normal completion requires an explicit idempotent `finish`. A settled child, an
open Blocker, temporary resource contention, or a downstream human-started
Experiment is not completion while existing agent authority and tools can still
resolve the prerequisite. The orchestrator must act, delegate, or arrange an
observable continuation. It may pause for a human only after naming the exact
new judgment, credential, privileged action, approval, or coordination needed.

At budget exhaustion or non-Stop ending, admitted children settle, the parent
fences new work, and the common visual report resumes the exact branch-bound
session with one immutable receipt. Human Stop uses the common graceful fence
and skips the report. Reauthorization always creates a new episode, native
session, and branch; it never reopens an exhausted parent.

## Branch lifecycle and merge eligibility

A branch remains writable while its episode accepts graph work. It is eligible
to merge when:

- the episode has a durable completed, exhausted, stopped, failed, or
  human-pause ending; and
- no task that can append a branch Patch is active.

Eligibility and merge state derive from canonical branch head, episode state,
task state, and successful receipts. The branch is never deleted. A newer branch
head after a prior receipt may be merged again; a head already covered by a
successful receipt cannot.

Only a human project member can dispatch **Merge to main**, from the exact
Auto-research episode detail. Active/nonquiescent, already-merged,
cross-project/cross-episode, or concurrently merging requests fail closed. The
merge task does not spend the concluded episode budget.

## Semantic rebase and merge

RCP prepares a closed graph-only context containing:

- immutable branch-base graph;
- current branch-head graph and exact head;
- current main graph and exact head;
- a typed semantic base-to-branch delta;
- bounded branch Patch summaries and provenance;
- transition schema and validation command; and
- deterministic conflicts found before provider launch.

The merge agent receives the orchestrator graph profile under the human merge
dispatcher's authorization. It receives scratch but no repository write roots,
membership, ontology, project configuration, Proposal approval, server command,
or general branch authority.

The agent authors one typed semantic Patch against current main. The transition
manager validates it and commits one attributable main transition or nothing.
Conflict diagnostics enter the same bounded native-session correction loop;
there is no manual node conflict viewer.

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
  diagnostic, and offer a fresh **Merge to main** dispatch when eligible;
- **Merge to main** only while eligible and changed;
- the ordinary merge task/output/correction/recovery history; and
- the episode report or final report error.

Main project graph views always show main. Selecting the exact branch Experiment
route may show its branch-bound Runs history and transcript, but branch chat is
read-only: there is no generic fresh composer or repair action that could create
a main-target conversation from a branch-bound session. A malformed or partial
branch route cannot restore a cached main Experiment selection.

There is no branch graph viewer, branch switcher, conflict editor, cherry-pick,
discard, repository-branch control, or automatic merge.

## Verification contract

[S115](../acceptance/S115-beliefs-change-only-through-you.md) owns the
protected-belief boundary, [S78](../acceptance/S78-one-budget-one-stop.md) owns
bounded orchestrator budget/Stop behavior,
[S113](../acceptance/S113-campaign-attribution.md) owns episode lineage, and
[S125](../acceptance/S125-auto-research-graph-branch-merge.md) owns graph-branch
isolation and semantic merge. Single-turn orchestration and child harvesting
remain focused implementation contracts under this specification.
