# Authority and Proposals

This specification owns who may start work or request a graph action, the two
permission gates, protected-belief Proposals, human judgment, and durable
authorization lineage. Runtime filesystem capability is specified separately in
[Providers and containment](providers-and-containment.md).

## Product authority and machine authority

RCP has two disjoint authority kinds, and neither can reach the other.

**Product authority** is everything below: authenticated members, agent
profiles, task contracts, and project membership. It governs graph actions,
conversations, episodes, and ordinary membership. RCP has equal members and no
administrator product role, so no member token is more powerful than another.

**Machine authority** is operating-system authority on the server host. It
governs installing, updating, restoring, configuring machine credentials,
provisioning a central checkout, and removing a member. Those operations live
under `rcp server ...` and are reached only from a console or SSH session with
the required OS account, never from a member session or an API route. A member
token cannot perform any of them, and no product role grants them.

The separation is structural rather than cooperative. The backend runs under a
dedicated operating-system account that owns its data directory and the
singleton lock, so an ordinary shell on the lab machine cannot read the control
plane, append to canonical history, or become the authority. A running-server
CLI command never opens SQLite beside the lock owner; it uses that account's
private control socket. `install`, `backup configure`, `restore`, and `update`
additionally need root because they change accounts, `/etc`, systemd, or
stopped-service state, and each drops back to the service account for ordinary
source and data work.

Machine authority never substitutes for human product judgment. An operator with
root cannot approve a Proposal, change project truth membership, or authorize an
episode through a machine command; those remain the protected human actions
below. The behavior of each server operation is specified in
[Server and machine operations](server-and-machine-operations.md).

## Four separate boundaries

RCP keeps these questions independent:

1. The authenticated human says **who authorized the root action**.
2. The constant agent profile says **which semantic and orchestration actions
   this agent role may request**.
3. The task contract says **which tools and RCP channels this surface exposes**.
4. Project membership, graph target, task scope, current state, and budget say
   **which concrete targets may be affected now**.

Every applicable boundary must admit an effect. A prompt, model, provider,
manifest, skill, native session, client field, file path, or parent-task id can
never widen another boundary.

Execution profiles choose provider, model, reasoning, execution machine, and
run-scope repositories. They grant no graph or orchestration permission.

## Human identity and project membership

Every human has an immutable random `user_id` inside a space and a mutable,
single-line display name. A personal space has one durable local owner. A team
request derives its human identity from server-authenticated state; a request
body cannot select an actor id.

All members have equal product authority at the space level. Project membership
is separate: creating a project seats the creator, project members may invite
another enrolled member, and a nonmember sees the same not-found behavior as an
unknown project. Project membership is checked on every project-scoped request
and again during Apply under the canonical append lock.

Leaving or losing membership durably applies the existing Stop fence: the
already-authorized turn finishes, but no new continuation or watcher claim may
start. The last project member cannot leave. Credential rotation or revocation
does not stop already-authorized work; membership removal is the project-level
authority change that does.

## Human authority

Only a human action may:

- approve or reject a Proposal;
- set ordinary node standing;
- select an option on an ordinary non-superseded Decision;
- accept a Hypothesis status transition;
- change project truth-scope membership;
- authorize a new bounded Experiment or Auto-research episode; or
- dispatch a graph-branch merge to main.

The Auto-research orchestrator is the deliberate Decision exception. The human
has already authorized project-wide bounded research, so the orchestrator may
queue and choose Decisions directly on its episode branch. A human-dispatched
merge agent receives that orchestrator graph profile for carrying legal branch
changes to main. This exception does not grant Proposal approval, project
configuration, membership, ontology, or server authority.

Contest and Agree are independent human controls. Clearing or replacing either
returns standing to the corresponding staged state, and Sync is the only
canonical commit. Blocker standing and lifecycle are independent: humans and
authorized graph agents may set a Blocker `open`, `resolved`, or `superseded`;
changing judged node content returns accepted or contested standing to asserted.

## Decision authority

A Decision records whether it is `open`, `ready`, `revisit`, `decided`, or
superseded. Agents may create and queue a Decision as `open`, `ready`, or
`revisit`. `ready` and `revisit` require at least two distinct options and enter
human attention; ripeness is prompt guidance, not an inferred scientific fact.

For ordinary work, the node-detail ballot is the only producer of
`selected_option` plus `status: decided`. Human Sync commits those fields and
accepted standing together and withdraws competing pending Proposals on that
Decision. Historical Decision Proposals remain replayable and resolvable through
the same named Decision-choice authority path.

The Auto-research orchestrator may queue and decide Decisions directly within
its authorized episode and graph branch. Its child workers use the ordinary
profile and do not inherit that exception.

## Two permission gates

### Dispatch

Before any provider-side effect, RCP resolves the named dispatch action against
the current human or authorized parent, project membership, agent profile, task
contract, graph target, execution profile, scope, and budget. Refusal launches
nothing, creates no scratch, and spends no unit. A permitted dispatch first
writes a durable task binding those facts.

Every continuation that spends a new unit must derive from that bound authority
or pass admission again. Task lineage explains causation; it is not itself a
permission principal.

### Apply

Immediately before canonical append, RCP reloads live membership and project
state, re-prepares RCP-owned bookkeeping, runs the transition manager and
semantic validator, and rechecks the target-specific action under the append
lock. Graph movement alone is not a rejection; current semantic invalidity or
lost authority is.

A refusal is not a rollback. Provider cost, repository writes, external calls,
and compute already performed remain real, and the task answer remains visible
independently of the Patch verdict.

### Replay

Replay performs no permission check. Once admitted, a Patch cannot later be
invalidated by user, membership, profile, or task changes. Historical records
with no current attribution continue to materialize.

## Agent profiles

Agent semantic permission is represented by two constant code profiles:

- **ordinary** — shared by ordinary Work, Seed/Refresh within its ingestion
  contract, Experiment-loop work, and Auto-research child Work; and
- **orchestrator** — one project-owned profile used only by an Auto-research root
  and by a human-dispatched graph merge.

The manifest cannot edit these profiles. Surface contracts narrow them further:
Discuss and Paper expose no Patch; Experiment-loop work is limited to its
focused Experiment policy; Seed/Refresh owns coverage; merge is graph-only and
receives no repository write scope.

## Graph action vocabulary

Graph authority derives from the typed operation plus a named exception wherever
the target type or field changes who may act. It is never guessed from an
operation's superficial shape.

The base action families are create, update, remove, supersede, and merge node;
create and remove edge; set standing; create, resolve, and withdraw Proposal;
set coverage; set project truth scope; and set ontology. Current profiles apply
these rules:

- Humans may perform current product graph actions, subject to their explicit UI
  action and project membership.
- Ordinary and orchestrator agents may create ResearchQuestions and Hypotheses
  and connect nodes created earlier in the same outer Patch.
- Once a ResearchQuestion or Hypothesis exists, every agent must use a Proposal
  to edit, remove, supersede, merge, or restructure that protected record.
- Attaching or replacing Evidence-to-Hypothesis epistemic edges remains a direct
  agent assertion. The later Hypothesis status change is the protected judgment.
- Ordinary agents may queue Decisions but cannot choose them. The orchestrator
  may queue and choose Decisions within its bounded episode or merge.
- The orchestrator has direct current control of Evidence, Decisions,
  Experiments, and Blockers, including standing, subject to operation validation.
- No agent may resolve a Proposal or change project truth scope or ontology.
- Seed/Refresh alone may write coverage bookkeeping.
- Historical Ambiguity and glossary authoring operations remain replayable but
  no current agent or human surface grants their legacy authoring authority.

Any action whose ordinary typed payload cannot distinguish a special human path
must declare the exact `human_action`; for example, a Decision choice cannot be
inferred from a generic `update_nodes` shape.

## Protected-belief Proposals

An agent Proposal contains exactly one declared intent:

- `content_change`;
- `removal`;
- `supersede`;
- `merge`;
- `protected_relation_change`; or
- `status_change`.

The intent is checked against its restricted typed operation union and may not
bundle an unrelated judgment. Supersede and merge join two distinct nodes of the
same protected type. A status-change Proposal changes exactly one Hypothesis and
names one valid Evidence-to-Hypothesis epistemic edge as its cause. Other
Proposal intents use their human-readable rationale and do not invent an
evidence cause.

Every agent-produced Proposal waits for a human. The orchestrator writes its
children's instructions and therefore may not approve a child's Proposal as an
independence shortcut. Any agent may withdraw a still-pending Proposal that later
work proves obsolete or duplicated; withdrawal applies no semantic operation.

RCP snapshots Proposal dependencies at creation: referenced nodes, edges, and
project configuration. Removal also snapshots the exact incident-edge set,
including empty. A changed dependency, recreated edge, or changed incident set
makes approval stale. Sync then records withdrawal and applies no semantic
change. Several human judgments in one Sync are evaluated in order against the
state produced by earlier judgments and commit atomically.

## Guarded removal

`remove_nodes` removes current nodes and their incident edges without rewriting
history. Every target must exist, lack accepted standing, and, for an
Experiment, have no active bounded episode. One invalid target rejects the whole
operation.

The only accepted-standing exception is human approval of a current removal
Proposal for a protected node, and only while its dependency and exact incident
snapshot remain current. The UI does not combine clearing standing and removal;
the standing change must first become canonical.

## Authorization lineage and provenance

The durable task record binds authorizer, project, graph target, agent profile,
task contract, scope, execution profile, provider/session/machine, parent and
episode lineage, and budget allocation. Every continuation preserves its exact
root authority and target.

Canonical Patch provenance retains the facts required outside the originating
space:

- `producer`;
- immutable `authorized_by` snapshot;
- `profile` where an agent produced it;
- direct `task_id`;
- `episode_id` for episode work;
- source operation/effect identities where applicable; and
- strict branch-merge provenance for a merge Patch.

Display names are snapshots and are never repainted after rename. Legacy
Patches remain unattributed. Provider credentials, operational parent chains,
and private execution details stay in task receipts rather than canonical graph
history.

## Orchestration commands

Orchestration commands use a separate closed vocabulary and audit path from
graph operations. The Auto-research orchestrator may validate/apply, inspect
status, spawn ordinary workers, pause/resume/stop its children, exchange
star-topology mail, arm graph conditions, start or stop bounded child Experiment
episodes, harvest lifecycle notices, and request guarded finish within its
episode and budgets. Ordinary workers can reply only through their narrower
contract. Humans own root dispatch, reauthorization, merge dispatch, and human
controls.

Commands never become graph authority by themselves. A graph change still uses
the typed Patch, transition manager, and Apply gate.

## Verification contracts

The durable observable boundaries are [S08 human authority](../acceptance/S08-human-authority.md),
[S53 truthful attention](../acceptance/S53-truthful-attention-and-run-surfaces.md),
[S78 bounded Auto-research](../acceptance/S78-one-budget-one-stop.md),
[S100 two permission checks](../acceptance/S100-permission-is-checked-twice.md),
[S101 project membership](../acceptance/S101-project-membership.md),
[S113 attribution](../acceptance/S113-campaign-attribution.md),
[S115 protected beliefs](../acceptance/S115-beliefs-change-only-through-you.md),
[S121 truthful refusal](../acceptance/S121-a-refusal-explains-itself.md), and
[S125 branch merge](../acceptance/S125-auto-research-graph-branch-merge.md).
