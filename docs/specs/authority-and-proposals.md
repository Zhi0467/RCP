# Authority and Proposals

This spec owns graph and orchestration permissions, protected-belief Proposals,
and authorization lineage. [Providers and containment](providers-and-containment.md)
owns runtime filesystem capabilities.

## Product authority and machine authority

Product authority governs graph actions, conversations, episodes, and membership.
Authenticated members, code-owned profiles, and task contracts define that authority.
Members are equal. There is no administrator product role.

Machine authority covers installation, updates, restore, credentials, central
checkouts, and member removal.
[Server and machine operations](server-and-machine-operations.md) owns its
commands, service-account boundary, and cooperative team trust model. Provider
sign-ins are member actions
([owner](providers-and-containment.md#member-sign-in-settings-and-selection)).
Machine authority never grants human product judgment.
Member terminal access follows
[its runtime boundary](providers-and-containment.md#member-terminals).

## Four separate boundaries

Every effect must pass four independent boundaries:

1. The authenticated human authorizes the root action.
2. A constant agent profile permits semantic and orchestration actions.
3. The task contract exposes tools and RCP channels.
4. Membership, graph target, scope, current state, and budget admit the targets.

Prompts, providers, manifests, skills, paths, sessions, and parent-task ids cannot
widen these boundaries. Execution profiles grant no graph permission.
[Providers and containment](providers-and-containment.md) owns provider and model
resolution.

## Human identity and project membership

Humans have an immutable random `user_id` within a space and a mutable single-line
display name. Personal spaces have one durable local owner. Team requests derive
identity from server authentication, never from an actor id supplied in the body.

Project creation seats its creator. Project members may invite enrolled space
members. Nonmembers receive the same not-found response as for an unknown project.
Membership is checked on each project request and again under the Apply lock.

Leaving or losing membership durably fences new continuations and watcher claims.
Already-authorized turns finish. The last member cannot leave. Credential rotation
or revocation alone does not stop already-authorized work.

### Who-may rules

Membership and exceptional member-specific rules use shared pure `rcp.policies`
predicates across routes, storage, and offers. Listing queries filter their own
SQL. Owners load the facts and retain the check order, refusal status, lifecycle,
and captured authorization. An offered control may still refuse an action.
A repeated action may remain idempotent even when its control is hidden.
**Why:** separate permission rules for buttons and the server drift apart.

## Human authority

The [invariant registry](../../AGENTS.md#stable-invariants) owns protected human
authority. The profile and action rules below implement its bounded exceptions.

The member's authenticated page and an ordinary Work chat controlling a requested
Experiment or Auto-research episode count as human authorization only under the
[member-page](conversations-episodes-and-watchers.md#native-chat-context) and
[Work-started episode](conversations-episodes-and-watchers.md#work-started-episodes)
contracts. Neither a voice confirmation preference nor a provider session widens
those contracts.

Contest and Agree are independent staged controls. Clearing or replacing one
updates the corresponding staged standing. Sync commits it. A Blocker's standing
is independent of its `open`, `resolved`, or `superseded` lifecycle.
Authorized graph agents may change that lifecycle. Any non-approval update or
retirement, lifecycle-only changes included, returns accepted or contested
standing to asserted.

## Decision authority

Agents may queue Decisions with status `open`, `ready`, or `revisit`. The `ready`
and `revisit` states require at least two distinct options and enter human
attention. Ripeness, equal investigation and detail, and placing leanings in the
rationale are prompt guidance. They are not enforced. Queued Decisions may restate
their authored fields, including when they are pinned Experiment inputs.
The contract asks for new evidence and retention of still-open choices.
Admission checks queued status, not rationale quality. The selected option stays
unchanged.

Ordinary decisions require an explicit human ballot. Sync commits the selection,
decided status, accepted standing, and withdrawal of competing pending Proposals.
Historical Decision Proposals retain their named resolution path. Only the
Auto-research root may decide directly on its branch. Ordinary children cannot.

## Two permission gates

### Dispatch

Before any effects, dispatch checks the authorizer or admitted parent, membership,
profile, contract, target, execution, scope, and budget. It then persists the
binding. Refusal creates no scratch, launches nothing, and spends nothing.
A paid continuation must derive from that binding or pass admission again.
Lineage alone is not permission.

### Apply

Gate 1 runs under the append lock. `WriteAuthority` checks the producer, live
membership, task binding, and actions against the state before the Patch.
Approval needs an explicit authorizer. Branch writes use parent authority.
Even a no-change merge must check the live task.

Gate 2, `validate_patch`, checks graph legality with the resolved `Writer`.
It checks ordered rules that depend on state. It does not check identities,
membership, or tasks. `HistoryManager` requires authority. Agent validation reports
profile refusals before Apply. State movement alone never causes rejection.
Semantic invalidity or lost authority does.

**Why:** each gate has one owner shared by all writer paths. Staged validation
handles dependencies within the same Patch. Generated-sequence tests check
well-formedness instead of adding a second runtime invariant gate.
Refusal cannot undo provider cost, repository writes, calls, or compute.
Answers remain independent of the Patch verdict.

### Space-thread project bindings

A Space task's bookkeeping project id is the space id, never a graph target.
`space_turn_project_bindings` stores each admitted project's dispatch authority,
write-scope fingerprint and main target. `AppStore.agent_task_authority` requires
that exact `(operation_id, project_id)` binding; absence or a non-main binding
refuses authority. Ordinary tasks retain their exact project equality check.
Per-project Apply still checks current membership under the project's lock.
No cross-project atomicity or lifecycle authority follows from a Space task.

### Replay

Replay checks the persisted producer, author, kind, and graph validity.
It never checks today's permission. Later membership, task, or profile changes
cannot invalidate admitted or unattributed history. Restore, import, identity
publication, scope publication, branch metadata, and merge receipts retain their
own publication contracts.

## Agent profiles

Two constant code profiles grant semantic permission:

- The **ordinary** profile applies to Work, Seed and Refresh within ingestion,
  Experiment loops, and Auto-research child Work.
- The **orchestrator** profile applies to an Auto-research root, the merges it
  dispatches of forks its lineage owns, and a human-dispatched graph merge.

Manifests cannot edit these profiles. Surface contracts narrow them.
Discuss and Paper expose no Patch. Experiment work has its focused policy.
A merge has no repository write roots unless it lands episode code.
In that case, it gets exactly the local-merge roots used by chat Integrate:
the episode worktree and shared checkout.

## Graph action vocabulary

Typed operations map to named create, update, remove, supersede, merge, edge,
standing, Proposal, truth-scope, and ontology actions. Special human paths must
name `human_action`. An orchestrator Patch that records a Decision outcome must
name `agent_action="decision_choice"`, and only such a Patch may name it. A
Decision choice is never inferred from `update_nodes`.

Both profiles may create ResearchQuestions and Hypotheses. They may connect nodes
created earlier in the same outer Patch. Existing protected nodes require a
Proposal for edits, removal, superseding, merging, or protected structural changes.
`has_subquestion` and `has_hypothesis` are protected structural relations.
Connecting either endpoint created in the outer Patch remains direct.
A Proposal may still target a belief or relation created in its own outer Patch.
**Why:** a merge lands a branch-created Hypothesis and its pending Proposal in one
main revision. Refusing that combination left the branch unmergeable.

`has_decision` is an ordinary relation even between existing nodes.
So are `blocked_by` and `governed_by`. Evidence-to-Hypothesis epistemic edges remain
direct assertions. Hypothesis status is the protected judgment.
These authority rules do not exempt new edges from current endpoint and retirement
checks.

The orchestrator directly controls ordinary Evidence, Decisions, Experiments,
and Blockers, including their standing. Typed validation and the orchestrator's
branch or merge boundary still apply. No agent resolves Proposals or changes
truth scope or ontology. Graph-writing agents may `upsert_glossary`, including
during bounded episodes. There is no separate human glossary editor.
Historical Ambiguity operations are replayable. No current surface grants
authority to author them.

## Protected-belief Proposals

An ordinary Proposal has one intent from the restricted union in
[core operations](../../src/rcp/core/operations.py). It cannot combine unrelated
judgments. Superseding and merging require distinct nodes of one protected type.
A status change names one Hypothesis and a valid incoming epistemic Evidence edge
as its cause. Other intents use rationale without invented Evidence.

Canonical merge additionally permits exact standing changes, canonically proven
human-caused status changes, and removal of accepted ordinary nodes.
Dependency rule 3 groups connected intents when one intent's approval effects
overlap another intent's dependencies, in either direction. Read-only overlap does
not connect them.
Approval is atomic. A protected edge may be removed and recreated atomically.
This includes content edits to existing retirement edges that preserve their
identity, endpoints, and relation. New retirement uses dedicated operations.
If answers break the connection between intents, each surviving component gets
its own card.

Explicit standing overrides implicit acceptance. Rule 3 accepts only subjects
written by the approval. Earlier rules retain their recorded behavior for bundles
and standing. Approval replays the stored effects and exact implicit standing.
RCP builds merge effects from its plan and answers. These forms grant agents no
exception for direct approval or removal.

Every agent Proposal waits for a human, including orchestrator-authored work.
Any agent may withdraw obsolete or duplicate pending Proposals without semantic
effect.

Filing snapshots the referenced nodes, edges, and configuration.
For removal, it also snapshots exact incident sets, including empty sets.
Rules 2 and 3 also depend on the Evidence at the cause edge.
Rewording that Evidence or judging its standing later makes the Proposal stale.
Ordinary new filings use rule 2. Connected multi-intent merges and atomic
replacement of protected edges use rule 3. Rule 3 allows the replacement edge to
exist at filing if it is removed before recreation. Carried source Proposals
refile with the current destination stamps and rule. Replay keeps the recorded
rule.

Changed dependencies, unexpected recreated edges, or changed incident sets make
approval stale. Sync withdraws a stale Proposal without semantic effects.
Multiple judgments evaluate in order and commit atomically.

Attention uses the same stale check and drops a previously staged Approve.
Human Refresh from main starts ordinary node Work with the node-chat provider and
model at the lowest probed effort (the configured effort when no model catalog is
cached). It also adds Evidence triage when Evidence caused the Proposal and the
project enables that skill. Context names only the Proposal and moved dependencies.
The agent withdraws the old Proposal and refiles it if it is still valid.
A new filing still waits for a human. When the cause edge is itself a
[stale assessment](graph-history-and-transitions.md#stale-assessments), context
lists it and the same turn re-judges it first. Re-judging later would make the
refiled Proposal stale again.

Human Refresh of a stale assessment from main starts the same light Work on its
Evidence, with Evidence triage when the project enables it. It is keyed per edge
and Evidence revision.
Context names the edge, the Evidence, and the pending Proposals that cite the
edge. The agent recreates or removes that one edge and changes nothing else.
Evidence-to-Hypothesis edges are direct assertions, so neither refresh grants
authority beyond ordinary Work.

Replay-only and merge-only forms are not refreshable. These include multi-intent,
standing, human-caused status, and accepted ordinary-node removal forms.
Proposals whose nodes are all gone are not refreshable either.
Reject, or a Sync that stages a judgment on one, withdraws stale Proposals in these
cases; an unstaged stale Proposal stays pending.

## Guarded removal

`remove_nodes` deletes current nodes and incident edges while preserving history.
Each target must exist and lack accepted standing. An Experiment target must also
have no active bounded attempt. One invalid target rejects the operation.
Human approval of a current removal Proposal is the exception to the accepted
standing guard. This includes the merge review form for an accepted ordinary node.
Dependency and incident snapshots must still match. Before ordinary removal,
the UI requires a canonical change that clears standing. It never combines the two.

## Authorization lineage and provenance

Tasks bind the authorizer, project, target, profile, contract, scope, and execution.
They also bind the provider, session, machine, parent and episode lineage, and
budget. Continuations retain root authority and the exact target.
[Patch provenance](graph-history-and-transitions.md#patch-provenance) owns the
portable canonical subset. Credentials, private execution details, and the parent
chain stay in operational receipts.

## Orchestration commands

[Auto-research](auto-research-and-branch-merge.md#staged-command-client) owns the
closed command vocabulary and its audit path. Ordinary workers have narrower
contracts. Humans own root dispatch, merges outside the orchestrator's owned
forks, reauthorization, and protected controls. Commands do not grant graph authority. Changes still require typed
Patches through the transition manager and Apply gate.

## Private-thread approval cards

A member's private agent threads act for that member. Ordinary acts run without
a tap: Patches, compute, Bulletin posts, schedules and handoffs, starting the
Experiments and Auto-research the member asks for, and the
[episode controls](conversations-episodes-and-watchers.md#work-started-episodes)
and watcher stops for what the thread started. Each records its turn's operation and authorizer as any Work turn
does. Projections derive
[`via_agent`](api-web-and-desktop-projections.md#agent-thread-api) from the
origin operation's thread; nothing new enters a Patch, so replay is unchanged.
Protected acts wait for the member through a card. There are two kinds: a
derived Proposal card and an asked card.

**Proposal cards are derived, never stored.** Each pending Proposal on live main
whose `created_by_operation_id` names a turn of the member's open project thread
gets one card (`raising_turn` and `proposal_cards` in
[`runs/approval_cards.py`](../../src/rcp/runs/approval_cards.py)). `bound_thread`
alone decides which thread raised it; receipts never do. The draw writes and
caches nothing. A card carries Approve, Decline and Later. Later settles nothing
and never blocks the agent. Approve and Decline call `decide_proposal`, which
admits like an Inbox decision: a patch-capable member, project membership and
write admission. A thread check would add no authority, so the route is not tied
to a thread. It reads live main and decides only a pending, non-stale Proposal,
fenced at the revision it read. No presented hash is needed: a Proposal's content
never changes under its id, and staleness is rechecked at that revision. The
recorded reason is "Approved from an approval card" or "Declined from an approval
card". A Proposal that another tap or the Inbox decided first, including one that
moved the graph mid-decide, answers `proposal_not_pending`; any other moved graph
answers `graph_changed`. A tap starts no agent turn. Graph watchers armed on the
Proposal fire as after any human Sync.
Proposals raised by a Space turn, a node chat, an Experiment or a scheduled
consolidation get no derived card, because no member project thread raised them;
the member's Space thread can still ask one.

A member's private thread, project or Space, may attach a strict action to
`ask`. Work and the Space handler resolve this grant once with their verbs
(`approval_card_member`); the prompt shows the card usage and the ask handler
accepts an action only from that value, while the thread is still open. Which
action kinds and projects a thread may ask is one rule: `THREAD_CARD_KINDS` and
`card_project_allowed` in
[`runs/approval_cards.py`](../../src/rcp/runs/approval_cards.py), and the usage
renders the same set. A project thread's action names its own project and may
be any kind but an Auto-research start. The Space thread's names any project the member belongs to and may
be a Proposal judgment, truth membership, a branch merge or an episode merge:
everything an Inbox, branch view or Settings decision takes, so the member never
has to open the project to decide. An Auto-research start is the project thread's
`episode` verb, so the Space thread hands it off. A project outside the member's
set is refused before it is opened. The action supplies no choices or multiple
selection; RCP offers Approve and Decline. The action models live in
[`approval_actions.py`](../../src/rcp/approval_actions.py): Proposal judgment,
repository truth membership, graph-branch merge, and episode merge. A Proposal
judgment names an optional `branch_id`: without it the Proposal is on main, with
it on that graph branch (`proposal_graph`). A stored action from before
`branch_id` existed reads as main. The model also keeps Auto-research
authorization, which no thread kind may newly ask: a new ask is refused with
`auto_research_uses_episode_verb` because the member's Work chat starts
Auto-research with its `episode` verb. Stored Auto-research cards still render,
settle on a tap through the human Start, and settle after a restart from their
request receipt. Plain question answers never authorize these actions. A
`merge` command from a thread holding this grant is refused with
`approval_card_required`, whose message names the merge card, so the agent asks
instead of telling the member to merge by hand.

The question and its slim `approval_cards` row are committed together. The row
pins the action and live facts under one SHA-256: pending non-stale Proposal
content, repository identity and current membership, the resolved Auto-research
profile, or the merge source head and preview. The preview already pins main's
value of every field the source changed, and the merge plans against live main,
so an unrelated main commit leaves a merge card current. Ask refuses an action
whose target does not exist or no longer applies (for example `branch_not_found`,
`approval_target_not_found`, `proposal_not_pending_or_stale`, or
`truth_scope_unchanged`). Reusing an ask key
requires the same action and question; it does not refresh the presented pins.
The agent's usage renders each granted action kind and its fields from the action
models, and the meaning of each outcome. Ask refuses a main Proposal card whose
derived card the asking thread's dock already draws, with `proposal_card_shown`:
in a project thread, one this thread raised; in the Space thread, one any of the
member's open project threads raised. Another project thread may still ask for
the same Proposal, so the Space thread's dock can show both cards; deciding
either settles the other on its next refresh or tap. The project thread's
question snapshot shows each card's action and its result.

The Space thread's card question is owned by the Space thread under the space
id, as its other bookkeeping rows are; its action names the real project. Its
origin binds no write scope because a Space turn may have no writable project
(`SPACE_QUESTION_CAPABILITY`). No answer resumes a Space session, so a reused key
skips the session binding check: the agent reads the outcome by repeating the same
ask while it waits, or with the same key from a later turn or session. Chat
answer reconciliation skips these questions for the same reason. Its card shows in the Space thread's dock through the member-wide
card list, only while the member still belongs to the action's project. The
Space prompt raises a card when one fits and never describes one that `ask` did
not return, because a Space agent that could only point at a dock once invented
a card that did not exist.

Only the thread's member can resolve a card. The tap checks membership in the
action's project, claims once, compares the presented hash and live pins, and invokes the existing human
handler with its normal write admission. Direct Python calls enter the transfer
fence and Experiment lock only when the handler does not acquire them itself.
A tap settles `stale` only when the presented hash or the live pins no longer
match, their target is gone, or the merge handler reports changed heads; it
executes nothing and uses the card up. A mismatched presented hash settles
`stale` even for Decline. A Proposal card decides through the Inbox's own Sync
(`sync_graph`) on the Proposal's graph, main or its branch, so a branch Proposal
gets the branch's edit admission and pins its branch content. That Sync appends
only at the graph revision its live pins were read from, so the append lock rechecks them in the transition
that decides it; if any write moved the graph in between, nothing is decided.
That, a closed admission, an
unavailable writer or base, or a handler `503` releases the claim and returns
`409` (`graph_changed` for the moved graph) or `503`, so the member can tap
again and that tap rechecks the pins. A refusal settles `failed` with the
handler's own reason, including Patch rejection and halted replay. An approved
result names the `episode_id` or `operation_id` the action created. A card
expires after `APPROVAL_CARD_TTL_HOURS` or when its thread closes. The first tap
on an expired card in a live thread returns `expired`; a tap in a closed or
archived thread returns `409` with that thread refusal. Ordinary answer and
dismiss endpoints reject card questions with `409 approval_card`; any later tap
returns `409 card_taken`.

Decline on an asked card that approves a Proposal also rejects it. The card
settles `declined` first, so the member's answer is recorded before anything
else; then `decide_proposal` rejects the Proposal. The claim is never held across
that rejection, because it does not record which button was tapped. The
rejection reason is always the card's own Decline reason, never the agent's
approval reason, which would enter append-only history under the member's name.
The tap reports `rejected: true`, or `rejected: false` with the reason the
Proposal stays pending; a Proposal already decided elsewhere reports neither.
The card's stored result keeps that outcome, so a reload or another device still
shows that the Proposal stayed pending.
The agent reads the outcome from graph state. Decline on any other card executes
nothing, including a card that asks to reject a Proposal.

Card creation stores a random UUID4 request id; every result retains it.
Episode start, branch merge and episode merge pass it to their native request-id
admission. Episode merge compares the pinned source head again under its admission lock,
before reconciliation or merge writes.
Sync and repository membership have no request-id argument; the durable card
claim supplies their single-use execution identity. At startup a card claimed
before this process started and still without a result is never rerun. Claims
made after the start belong to live taps, which settle their own cards. It settles as approved when its effect exists:
the request id's episode-start, branch-merge task or accepted episode-merge
receipt, the Proposal's decided status, or the repository's requested truth
membership. Otherwise it fails as interrupted. When the graph cannot be read,
the claim is released and a later tap rechecks the pins.

Cards expire after `APPROVAL_CARD_TTL_HOURS`. Tap, list, a startup sweep, and
the sweep after every watcher poll settle due cards and cards of closed threads, and restore expires every pending
card. A list reads cards without a write lock unless one is due. The Web shows
the server's card state, never the device clock. Expiry answers the question with `expired`. Other results
answer it with approved, declined, stale or a failure reason, reusing ordinary question delivery
to steer or wake the thread. Both plain questions and cards in private threads
have owner-only list, answer, dismiss and Needs-you notification delivery.
The approval endpoint is a member control, not a voice or WebMCP tool.
