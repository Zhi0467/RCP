# Authority and Proposals

This spec owns graph and orchestration permission, protected-belief Proposals,
and authorization lineage. [Providers and containment](providers-and-containment.md)
owns runtime filesystem capability.

## Product authority and machine authority

**Product authority** governs graph actions, conversations, episodes, and
membership through authenticated members, agent profiles, and task contracts.
Members are equal; RCP has no administrator product role.

**Machine authority** governs installation, updates, restore, machine credentials,
central checkouts, and member removal through `rcp server ...`. Running-server
commands use the private control socket rather than opening SQLite beside its
lock owner. `install`, `backup configure`, `restore`, and `update` require root,
then drop to the service account for ordinary source and data work. See
[Server and machine operations](server-and-machine-operations.md).

Members may sign shared provider accounts in, verify them, and sign them out.
These actions run as the execution account, name the acting member, and are
visible to all members. A dead shared login blocks everyone's work.

A member terminal inherits the service account's authority, including its
control socket. Project membership gates opening the terminal, not deliberate
use of that account's access. Mirrored sessions mount canonical state read-only
to resist mistakes; cooperative sessions report that this fence is unavailable.
Neither is a hostile-member security boundary or a graph-write channel. See the
[terminal trust boundary](providers-and-containment.md#member-terminals).
Unrelated OS accounts do not inherit the service account's data, lock, or socket.
Machine authority never grants human product judgment.

## Four separate boundaries

Every effect must pass four independent boundaries:

1. The authenticated human authorizes the root action.
2. A constant agent profile permits semantic and orchestration actions.
3. The task contract exposes tools and RCP channels.
4. Membership, graph target, scope, current state, and budget admit the targets.

Prompts, providers, manifests, skills, paths, sessions, and parent-task ids cannot
widen these boundaries. Execution profiles select provider, model, reasoning,
machine, and repositories; they grant no graph permission.

There is no selectable "provider default" model. An unnamed model resolves to
the first model in that machine's probed CLI readiness catalog. Only an unknown
catalog leaves the model empty for the CLI to choose. Projections export saved
and effective models separately on every readiness refresh without pinning an
unnamed profile to its current effective model.

## Human identity and project membership

Humans have an immutable random `user_id` within a space and a mutable single-line
display name. Personal spaces have one durable local owner. Team requests derive
identity from server authentication, never an actor id supplied in the body.

Project creation seats its creator; project members may invite enrolled space
members. Nonmembers receive the same not-found response as for an unknown project.
Membership is checked on each project request and again under the Apply lock.

Leaving or losing membership durably fences new continuations and watcher claims;
already-authorized turns finish. The last member cannot leave. Credential rotation
or revocation alone does not stop already-authorized work.

### Who-may rules

Members are equal, so most actions need only project membership. The membership
rule and the few rules that name a particular member (the requester of a
provisioning change, the admitter of a transfer) are pure predicates in
`rcp.policies`. Where a route, a storage transaction, and a `can_*` projection
judge the same rule, they call the same predicate. Queries that list a member's
projects or a project's seats filter seats in their own SQL.

Each owner still loads the facts and keeps its own check order, refusal status, and
lifecycle conditions, so a control can be offered and still be refused (a busy
project), or hidden while a repeat is accepted (cancelling a cancelled request). A
predicate never replaces a human authorization snapshot.

**Why:** a rule written twice, once for the button and once for the server, drifts.

## Human authority

The [protected authority boundary](../design.md#cross-cutting-invariants) reserves
Proposal resolution, ordinary standing, Decision choice, Hypothesis status
judgment, truth-scope membership, episode authorization, and merge dispatch to
humans. The orchestrator profile's bounded exceptions are listed below.

An action in the member's authenticated page counts as that member's action,
including through WebMCP or RCP's voice agent. Their fixed catalog can authorize
and stop episodes but exposes no other protected judgment controls; those remain
visible controls. Provider agents never run in the member page. A voice
confirmation toggle removes a guard, not a capability. See the
[member-page authority decision](conversations-episodes-and-watchers.md#native-chat-context).

Contest and Agree are independent staged controls. Clearing or replacing one
updates the corresponding staged standing; Sync commits it. Blocker standing is
independent of its `open`, `resolved`, or `superseded` lifecycle, which authorized
graph agents may change. Editing judged node content returns accepted or
contested standing to asserted.

## Decision authority

A Decision is `open`, `ready`, `revisit`, `decided`, or superseded. Agents may
queue `open`, `ready`, or `revisit`; the latter two require at least two distinct
options and enter human attention. Ripeness is prompt guidance. Options should
receive equal investigation and comparable detail; a leaning belongs in
`rationale`, not their order or wording.

Any update leaving a Decision queued may restate its title, question, options,
rationale, and consequences, even if it was queued earlier. Admission checks the
queued status, not the quality of the reason. The contract asks agents to
restate only on new evidence and retain choices that evidence has not closed;
these are guidance, not enforcement. This also holds for pinned Experiment
Decisions. The prior `selected_option` stays untouched so a revisit retains the
previous choice.

For ordinary work, only the node-detail human ballot produces `selected_option`
with `status: decided`. Sync commits these with accepted standing and withdraws
competing pending Proposals. Historical Decision Proposals remain replayable and
resolvable through the named Decision-choice path.

The Auto-research root may queue and decide on its branch. Its ordinary-profile
children and Experiment Work have no Decision exception.

## Two permission gates

### Dispatch

Before provider effects, dispatch resolves the action against the human or
admitted parent, membership, profile, contract, graph target, execution profile,
scope, and budget. Refusal creates no scratch, launches nothing, and spends no
unit. Admission first persists a task binding those facts. A paid continuation
must derive from that binding or pass admission again; lineage is not permission.

### Apply

Under the append lock, core `WriteAuthority` runs Gate 1: producer, live human
membership, agent task binding, and operation actions against the pre-Patch graph.
Human approvals require an explicit authorizer. Branch writes use their parent's
authority; no-change merge receipts also require live task authority.

Gate 2, `validate_patch`, checks graph legality with the resolved `Writer`.
Ordered staging checks state-dependent writer rules but never identities,
membership, or tasks. `HistoryManager` requires a write authority. Agent validation
reports profile refusals before Apply. Graph movement alone is not rejection;
semantic invalidity or lost authority is.

**Why:** one core owner per gate makes new writer paths inherit both checks.
Rules about nodes created earlier in a Patch still need staged state, so they
read the resolved role in the single validation loop. Graph well-formedness is
tested with generated operation sequences; defects belong in operation checks,
not a second runtime invariant gate.

Refusal does not undo provider cost, repository writes, calls, or compute. The
task answer stays visible independently of its Patch verdict.

### Replay

Replay checks persisted producer/author/kind consistency and graph validity, not
live permission. Later membership, task, or profile changes cannot invalidate an
admitted Patch; unattributed history remains readable. Restore, transfer import,
identity publication, scope bootstrap, branch metadata, and merge receipts keep
their own publication contracts; a no-change merge receipt still checks authority.

## Agent profiles

Two constant code profiles grant semantic permission:

- **ordinary:** Work, Seed/Refresh within ingestion, Experiment loops, and
  Auto-research child Work.
- **orchestrator:** an Auto-research root and a human-dispatched graph merge.

Manifests cannot edit them. Surface contracts narrow them: Discuss and Paper
expose no Patch; Experiment work has its focused policy. A merge has no repository
write roots unless it lands episode code, when it gets exactly chat Integrate's
local-merge roots: the episode worktree and shared checkout.

## Graph action vocabulary

Typed operations map to named create, update, remove, supersede, merge, edge,
standing, Proposal, truth-scope, and ontology actions. Special human paths must
name `human_action`; a Decision choice is never inferred from `update_nodes`.

Both profiles may create ResearchQuestions and Hypotheses and connect nodes
created earlier in the same outer Patch. Existing protected nodes require a
Proposal for edits, removal, supersede, merge, or structural changes. Evidence-to-
Hypothesis epistemic edges remain direct assertions; Hypothesis status is the
protected judgment.

The orchestrator directly controls ordinary Evidence, Decisions, Experiments,
and Blockers, including standing, within typed validation and its bounded branch
or merge. No agent resolves Proposals or changes truth scope or ontology.
Graph-writing agents may `upsert_glossary`, including during bounded episodes;
there is no separate human glossary editor. Historical Ambiguity operations are
replayable but no current surface grants their authoring authority.

## Protected-belief Proposals

An ordinary Proposal declares exactly one intent: `content_change`, `removal`,
`supersede`, `merge`, `protected_relation_change`, or `status_change`. Its restricted
typed operation union cannot bundle unrelated judgment. Supersede and merge join
distinct nodes of the same protected type. Status change targets one Hypothesis
and names a valid Evidence-to-Hypothesis epistemic edge as cause. Other intents
use a rationale without an invented evidence cause.

Canonical branch merge may propose an exact `standing_change` or a `status_change`
with canonically proven human cause. Dependency rule 3 permits a connected group
of intents whose approval effects overlap another member's effects or dependencies,
validated and approved atomically. Shared read-only dependencies alone do not
connect a group. This includes removing and recreating one protected edge.
An existing retirement edge may be replaced to edit its content while keeping
its id, endpoints, and relation; new retirement relations still require the
dedicated supersede or merge operation. If merge answers remove a connecting
effect, the surviving components become separate Proposals using the supplied card.
Explicit proposed standing overrides implicit accepted standing; rule 3 implicitly
accepts only written subjects, leaving read-only dependencies unchanged. Earlier
rules retain their same-node bundle admission and approval-standing behavior.
Approval replays the stored effects plus their exact implicit standing operations.
Merge may also propose exact removal of an accepted ordinary node.
Merge conformance proves every effect against the source delta; none of
these forms grants direct approval or accepted-node removal.

Every agent Proposal waits for a human. An orchestrator writes its children's
instructions and cannot independently approve them. Any agent may withdraw a
pending obsolete or duplicate Proposal; withdrawal applies no semantic operation.

Creation snapshots referenced nodes, edges, and project configuration. Removal
also snapshots the exact incident-edge set, including empty. A Proposal filed
under dependency rule 2 or 3 also depends on each Evidence-edge cause's Evidence node,
so any change to that Evidence stales it: rewording it (Address, consolidation) or
accepting or rejecting it in a later Sync. New ordinary Proposals use rule 2;
only new connected multi-intent merge groups, including atomic protected-edge
replacements, use rule 3. Rule 3 allows a replacement edge to exist at filing
when the same Proposal removes it before recreation. A carried pending source
Proposal is a new destination filing and receives current destination stamps,
including rule 2 or 3 according to its form. Replay of existing records preserves
their recorded rule, because it must judge them as filed.
Changed dependencies, unexpected recreated edges, or changed incident sets make
approval stale: Sync withdraws it without semantic change. Multiple judgments in
one Sync evaluate in order and commit atomically.

Attention lists stale pending Proposals by the same rule, so the card never
offers an Approve that Sync would withdraw. An Approve staged before the card
went stale is dropped, not sent. A human refreshes a stale Proposal from main.
Refresh starts an ordinary node Work chat. It uses the node-chat provider and
model at the lowest effort that model's probed catalog lists, and invokes
Evidence triage when Evidence is the cause. Its message carries only the
Proposal and what moved. The agent withdraws the Proposal and files one fresh
pending Proposal if the change still holds. The fresh Proposal waits for a
human like any other. Forms ordinary Work cannot file (a replay-only legacy intent, or
what only a branch merge files: a multi-intent bundle, standing change,
human-caused status change, removal of an accepted unprotected node) are not
refreshable, nor is one whose nodes are all gone; a stale one is withdrawn by
Reject or the next Sync judgment.

## Guarded removal

`remove_nodes` deletes current nodes and incident edges, preserving history.
Each target must exist, lack accepted standing, and, for an Experiment, have no
active bounded attempt. One invalid target rejects the operation. Human approval
of a current removal Proposal is the accepted-standing exception, including the
merge review form for an accepted ordinary node. Dependency and incident snapshots
must still match. The UI requires clearing standing to become canonical before
ordinary removal; it never combines them.

## Authorization lineage and provenance

Tasks bind authorizer, project, graph target, profile, contract, scope, execution
profile, provider/session/machine, parent and episode lineage, and budget.
Continuations preserve root authority and exact target.

[Patch provenance](graph-history-and-transitions.md#patch-provenance) keeps the
portable subset: producer, immutable authorizer snapshot, agent profile and task,
episode where applicable, source operation/effect identity, and merge provenance.
Display-name snapshots never repaint after rename; legacy Patches remain
unattributed. Credentials, private execution details, and operational parent
chains stay in task receipts.

## Orchestration commands

[Auto-research](auto-research-and-branch-merge.md#staged-command-client) owns the
closed command vocabulary and its audit path. Ordinary workers have narrower
contracts. Humans own root and merge dispatch, reauthorization, and protected
controls. Commands do not grant graph authority: changes still require typed
Patches through the transition manager and Apply gate.
