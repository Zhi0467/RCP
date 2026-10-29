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
commands, service-account boundary, shared provider login, and cooperative
team trust model. Machine authority never grants human product judgment.
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

The member's authenticated page and an ordinary Work chat starting a requested
Experiment count as human authorization only under the
[member-page](conversations-episodes-and-watchers.md#native-chat-context) and
[Work-started Experiment](conversations-episodes-and-watchers.md#work-started-experiments)
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
Dependency rule 3 groups connected intents when their approval effects overlap
another intent's effects or dependencies. Read-only overlap does not connect them.
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
A new filing still waits for a human.

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
