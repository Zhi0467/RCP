# Identity, permissions, and agent profiles

**State:** confirmed working design from the 2026-08-08 and 2026-08-09 grilling
sessions; pre-blueprint and pre-implementation.

This module defines who authorizes work, which permission-bearing agent roles
exist, and when RCP checks authority. Deployment and durable project ownership
belong to [Spaces and project homes](spaces-and-project-homes.md). Credentials
and enrollment belong to
[Team authentication and membership](team-authentication-and-membership.md).

## Outcome

RCP keeps four boundaries separate:

1. The authenticated human answers **who requested this**.
2. The agent profile answers **which semantic or orchestration actions this
   agent role may request**.
3. The task contract answers **which tools and RCP channels this task can use**.
4. Task scope, current project state, and budget answer **which concrete targets
   this invocation may affect now**.

Every required boundary must allow an authority-bearing action. None may widen
another.

Team users are authenticated by the team backend. All space members have equal
space-level authority in the first version, while project membership determines
who may act inside a particular project. A request cannot choose a different
user id. Personal space has one local human owner.

There is no durable directory of user-owned agent actors. Permission-bearing
agent identity is intentionally limited to one ordinary profile for the space
and one orchestrator profile for each project. Concrete tasks, campaigns,
workers, provider sessions, and machines still have identities for lifecycle and
audit, but they are not permission principals.

## Runtime capability is not semantic permission

RCP already gives different task surfaces different runtime access. Discuss,
Work, Seed/Refresh, Experiment loop, Paper coach, correction, and orchestration
do not expose the same tools or output channels. That distinction remains.

An agent profile does not replace a task contract:

- Discuss exposes no graph-change channel.
- Paper coach remains read-only.
- Work retains its operational tools and optional Patch channel.
- Seed/Refresh retains its ingestion Patch contract.
- An Experiment task remains bounded to its episode and control node.
- Orchestration commands exist only in the orchestrate contract.

An action is admitted only when all applicable checks succeed:

```text
profile permits the action
AND task contract exposes the action
AND target is inside task scope
AND current project or campaign state permits it
AND required budget and dispatch authorization remain valid
```

No prompt, skill, manifest, provider, model, machine, native session, or task
payload can grant more authority.

The existing Project Settings concept called an agent profile describes
provider, model, reasoning, and execution machine. It must be renamed
**execution profile** when this design lands. An execution profile answers where
and how a task runs; it grants no semantic permission.

Execution profiles also carry an operational consequence that is not a semantic
permission but should be understood as a real boundary: the execution account
determines what a Work task can reach on that machine. Configuring a project to
execute as a particular account grants every project member unrestricted
operational reach as that account. In a team space this is always the space's
service account; see
[Spaces and project homes](spaces-and-project-homes.md#team-runs-execute-as-the-spaces-service-account).

## Permission is checked before execution

Starting work is itself an authority-bearing action named `dispatch`.
Apply-only checking is too late: an unauthorized task could already spend
provider budget, read project context, use server credentials, modify a
repository, or call an external service before its Patch was rejected.

Before any provider-side effect, dispatch admission binds at least:

- the authenticated human authorizer or authorized parent task;
- home space and project;
- agent profile;
- task contract and mode;
- execution profile and target machine;
- project, node, episode, or campaign scope;
- budget or invocation authorization; and
- parent and campaign lineage for spawned work.

The durable task record is created before execution begins. A continuation that
spends a new invocation—Resume, Retry, watcher wake, graph wake, message wake,
or spawned work—must point to the bounded authorization that permits it or pass
dispatch admission again.

## Permission is checked again before Apply

A candidate graph or project-truth change is checked against current permission
immediately before RCP appends it to canonical history. A launch-time semantic
check can improve an error message, but it never reserves authority for later.

If permission changes while a task is running:

- a now-forbidden Patch is rejected at Apply;
- completed compute, repository writes, external calls, and other operational
  effects are not undone; and
- cancellation or Stop is a separate operational action.

Because of that gap, revoking a person's access is paired with stopping their
work rather than left to fail at Apply hours later; see
[member removal](team-authentication-and-membership.md#member-removal).

The permission decision and canonical append belong to one serialized server
path. A member client or SSH storage process never applies the Patch on the
server's behalf.

## Replay never checks permission

Replay does not load users, memberships, tokens, profiles, or current
permission. Once a Patch was admitted to append-only history, later permission
changes cannot invalidate it.

Replay continues to validate graph structure and history ordering. Permission
checks such as "may this ordinary agent or orchestrator decide this Decision"
move to live admission before append. Historical patches remain byte-for-byte
unchanged, and missing identity records can never prevent graph materialization.

This extends to the project's own nameplate. Canonical history records
`home_space_id`, and replay reads it, but replay never refuses on it. A graph
must materialize in the wrong space; only the backend refuses to write there.

Audit and replay therefore have different jobs:

- provenance explains who or what caused an admitted change;
- replay trusts that admission already happened.

## Human members

All team members have the same space-level product authority in the first
version. There is no everyday owner/admin/member hierarchy and no PI role. Any
space member may create a project or invite another person into the team space.

Every team project has its own membership set. Its creator is its first member,
and every project member has the same project role and may invite another
existing space member to join. A project invitation is an authenticated
in-product item for an existing space member, not a reusable team-enrollment
credential. The invited person sees it on the project index and may join.

Equal authority does not allow a member to impersonate another person, use or
rotate another person's token, submit work attributed to another person, or
rewrite historical attribution. Nor does space membership alone authorize a
project action: dispatch and Apply both check current project membership.

Equality is preserved by keeping dangerous operations out of the product rather
than by ranking members. Backup, restore, update, and member removal require
machine privilege on the server, not a higher RCP role.

Whether later team versions introduce different human roles is outside the first
implementation. The first data model must represent project membership without
pretending that project members already have different ranks.

## Ordinary agent profile

One ordinary agent profile is shared across the space. It is not cloned per
human. Its permission is the semantic ceiling for ordinary agent work; each task
contract and concrete scope narrow it further.

When an orchestrator starts a Blocker worker or bounded Experiment loop, that
child uses the ordinary profile. It does not inherit the orchestrator's elevated
authority.

## Project orchestrator profile

Each project that supports auto-research has one project-owned orchestrator
profile. It does not belong to the member who starts a campaign. Every campaign
still records which authenticated member started it.

The orchestrator profile and its dedicated orchestrate task contract both allow
the bounded elevated actions settled in the
[orchestrator design](../handoffs/handoff-2026-08-07-orchestrator.md):

- both `queue_decision` and `decide_decision`, including choosing a Decision
  option and marking it decided;
- full direct control of Decisions, Experiments, Blockers, and Evidence,
  including their creation, ordinary content, lifecycle, standing, relations,
  and removal;
- direct creation of new ResearchQuestions and Hypotheses in their normal
  unresolved initial states;
- Proposal-only changes to an existing ResearchQuestion or Hypothesis,
  including ordinary content, status, standing, relations whose change modifies
  that existing node's meaning, and removal;
- bounded dispatch and worker management; and
- no work outside the project's campaign scope and budget.

The sharp boundary is **new versus existing** for ResearchQuestions and
Hypotheses, not a blanket ban on epistemic work. An orchestrator may directly
create a new open ResearchQuestion or proposed Hypothesis and connect new work
into the graph. Once one of those node records exists, the orchestrator may
request a change only by creating a Proposal, and **that Proposal always waits
for a human.**

The orchestrator edits all other graph node types as a human project member
could, subject to campaign scope and budget. Those direct actions take effect
immediately. Later human review of its Decisions, Blockers, Experiments, and
Evidence is retrospective and may produce a new correcting action; it is not a
delayed permission check or a retraction of work that already ran.

The two-gate design matters. Running the orchestrator profile through ordinary
Work does not expose orchestration commands. Running the orchestrate contract
with the ordinary profile does not grant elevated semantic authority.

## A Proposal is an escalation to a human

Inside a campaign, the orchestrator is the only producer of Proposals, and no
agent approves one. A Proposal means the orchestrator judged that something
needs human judgment rather than its own discretion.

The orchestrator scopes its sub-agents so they receive clear, executable work.
When a sub-agent cannot resolve something, it states the difficulty **in its
answer or notes**, and the orchestrator decides what to do with it—handle it
within its own authority, or escalate it as a Proposal.

This is a **prompt contract**, in the same category as the Work prohibition on
direct canonical `.research` writes. RCP instructs the orchestrator on how to
scope the agents it launches; it does not mechanically prevent a sub-agent from
producing a Proposal. Nothing may be built on the assumption that sub-agent
scoping is enforced.

The earlier producer-separation rule—an orchestrator approving an eligible
child-produced Proposal while being barred from approving its own—is removed. It
did not bind, because the orchestrator writes the instructions for the child
whose Proposal it would then approve, and it cost an extra paid invocation plus
an unresolved question about which children were eligible.

Human-initiated work outside a campaign is unchanged. An ordinary Work task a
person starts still produces a Proposal when it touches a gated operation.

## Authorization lineage replaces agent ownership

The root task records the authenticated human who authorized it. Every spawned
task records its parent task and campaign. Each task also records its active
profile, task contract, resolved scope, execution profile, budget use, and
provider/session/machine provenance.

```text
human authorizes root task
  -> root task dispatches child task
    -> child task produces operational effects and/or a Patch candidate
```

This lineage explains causation but does not grant permission on its own. Every
dispatch and Apply still passes the relevant current checks.

Removed concepts from the earlier proposal include:

- a durable `Actor` record for every agent;
- `owner_actor_id`;
- one default agent owned by each user;
- permission as an intersection with an owning user's agent profile; and
- globally addressing agents through an owner directory.

Task, campaign, and worker ids remain because running work needs durable
addresses and recovery state.

## Action vocabulary

**Shape confirmed 2026-08-12.** Three rulings settle how the list is built; do
not relitigate them.

| | Ruling | Why |
|---|---|---|
| How fine-grained? | **One action per patch operation, plus a named exception wherever a single field or target type changes who may act.** | It is already the shape of the only such rule that exists — `decide_decision` split from `queue_decision`. Coarse groups would force one name to cover both adding Evidence and deleting a node. Splitting by operation *and* node type would produce a list too long to read, and long lists are where mistakes hide. |
| Fixed or editable? | **Both profiles are constants in code.** Changing what an agent may do is a code change with a scenario attached. | Permission must not become configuration. A wrong toggle in Settings would quietly widen what agents may do to the graph, and no scenario would catch it. |
| One list or two? | **Two.** Graph actions and orchestration commands are separate vocabularies with separate checks. | They are different mechanisms — a file the agent writes versus a command it runs — with different failure modes and different audit records. One list would blur that for the sake of a single lookup. |

### List one — graph actions

Keyed to the operations materialization already applies
([materialize.py](../../src/rcp/core/materialize.py)). The base action carries
the operation's name; an indented action is a named exception carved out of the
one above it, and takes precedence over it.

| Action | What it covers | ordinary | orchestrator | human |
|---|---|---|---|---|
| `create_node` | Create any node, subject to the type-aware initial states already in the agent contract: a new Hypothesis starts `proposed`, a new Decision starts `open` or `ready`. | yes | yes | yes |
| `update_node` | Ordinary content edit on any node outside the exceptions below. | yes | yes | yes |
| → `update_protected_epistemic` | Any change to an **existing** ResearchQuestion or Hypothesis — content, status, or standing. | Proposal only | Proposal only | yes |
| → `decide_decision` | Write `selected_option`, or set a Decision `decided`. | no | yes | yes |
| → `queue_decision` | Move a Decision among `open`, `ready`, `revisit`. | yes | yes | yes |
| `remove_node` | Remove an asserted or contested node and its incident edges. | yes | yes | yes |
| → `remove_protected_epistemic` | Remove an existing ResearchQuestion or Hypothesis. | Proposal only | Proposal only | yes |
| `supersede_node` | Replace a node with a successor. | yes | yes | yes |
| → `supersede_protected_epistemic` | Retire an existing ResearchQuestion or Hypothesis in favor of a successor. | Proposal only | Proposal only | yes |
| `merge_node` | Fold one node into another. | yes | yes | yes |
| → `merge_protected_epistemic` | Fold an existing ResearchQuestion or Hypothesis into another node. | Proposal only | Proposal only | yes |
| `create_edge` | Add any legal relation. | yes | yes | yes |
| `remove_edge` | Remove any legal relation. | yes | yes | yes |
| → `restructure_protected_epistemic` | Attach, detach, or re-parent an **existing** ResearchQuestion or Hypothesis through `has_subquestion` or `has_hypothesis`, or retire or fold one through `supersedes` or `duplicate_of`. | Proposal only | Proposal only | yes |
| `set_standing` | Judge a Decision, Experiment, Blocker, or Evidence. | no | yes | yes |
| `create_proposal` | Raise a change for human judgment. | yes, belief only | yes | n/a |
| `resolve_proposal` | Approve or reject a pending Proposal. | **no** | **no** | yes |
| `withdraw_proposal` | Retract an obsolete or duplicated pending Proposal. | yes | yes | yes |
| `set_coverage` | Ingestion bookkeeping. | Seed/Refresh only | no | no |
| `set_project_truth_scope` | Change which repositories are project truth. | no | no | yes |
| `set_ontology` | Extend or deprecate type vocabulary. | no | no | yes |
| `upsert_glossary` | Author a glossary term. | no | no | no — see [Q4](../open-questions.md) |

`create_ambiguity` and `resolve_ambiguity` remain in the operation set for wire
compatibility and replay of historical patches. No profile permits either.

Two properties of this table are load-bearing. `resolve_proposal` is the row
that makes "no agent approves a Proposal" mechanical rather than a prompt
contract. And `set_standing` is target-aware by construction: judging an
existing ResearchQuestion or Hypothesis is not this action at all, it is
`update_protected_epistemic`, so there can be no profile-wide
`may_set_standing` boolean.

### List two — orchestration commands

These are staged-client commands and RCP admission points, not patch
operations. They produce task events, never graph history.

| Action | What it covers | ordinary | orchestrator | human |
|---|---|---|---|---|
| `dispatch` | Start any task at all. Checked before a provider launches. | n/a | within campaign scope and budget | yes |
| `spawn` | Seat a worker on an Experiment or Blocker. | no | yes | n/a |
| `pause` / `resume` / `stop` | Control a task it owns. | no | yes | yes |
| `message` | Address a worker it spawned; workers reply to it. | reply only | yes | orchestrator only |
| `watch_graph` | Arm a graph condition by command. | no — loops use `watch.json` | yes | n/a |
| `reauthorize_campaign` | Extend an exhausted budget. | no | no | yes |

`validate` and `status` are queries on the staged client, carry no authority,
and are available wherever the client is staged.

### Deriving the action, not guessing it

Invariant 3 already forbids inferring which action produced a patch from its
operation shape. That rule scales to this table unchanged: RCP derives the
action from the operation plus its target's type where that is unambiguous, and
requires the producer to name the action where it is not. A direct Decision
choice and an ordinary node edit are both one `update_nodes` on one node, which
is why `human_action` exists on the Patch today. Every new exception in this
table must be checkable the same way, or it needs its own declared name.

### The protected-type rule

**Confirmed 2026-08-12.** One line governs every exception above: an operation
is free unless it touches an existing **ResearchQuestion** or **Hypothesis**
record. Those two types are the project's beliefs, and only a human moves a
belief.

`supersede_node` and `merge_node` therefore get their own exceptions rather than
folding into `update_protected_epistemic`. Both are ways to change a hypothesis
without ever calling it an edit, and leaving them open would put an unlocked
door beside a locked one.

### Campaign scope is the project

**Confirmed 2026-08-12.** An auto-research campaign is scoped to the project,
not to the question the human started it from. It may create Evidence, run
Experiments, and open Blockers anywhere in the project graph, including under a
different ResearchQuestion than the one it began with.

The brake is the budget and the protected-type rule, not a fence around one
subtree. This is what makes the budget the enforced safety mechanism rather
than bookkeeping — there is no second boundary quietly doing that job.

### Which arrows are protected

**Confirmed 2026-08-12.** Only the ones that restructure or retire. Attaching
Evidence to a Hypothesis through `supports`, `weakens`, `refutes`,
`inconclusive`, or `contradicts`, and pointing an Experiment at one through
`tests`, all stay direct.

The reasoning is that attaching evidence does not move a belief. The
Hypothesis's `status` does, and that is already Proposal-only for both agent
profiles, so the protection is in place one layer down. Routing evidence
attachment through the Inbox as well would fill it on every Seed and Refresh,
and would deadlock the existing belief Proposal, whose required cause is a valid
Evidence to Hypothesis edge it could no longer draw.

**Connecting a node created in the same Patch is not restructuring.** The
orchestrator may create a new ResearchQuestion or Hypothesis directly, and that
permission would be void if the edge attaching it needed approval. This follows
the same new-versus-existing line as every other exception: the rule protects
records that already exist.

### Who the rule binds, and what it costs

**Confirmed 2026-08-12.** Every agent, from the day it lands — including an
ordinary Work turn the human started and is watching. Not only campaign workers.

Two reasons. One authority regime is explainable and two are not; and the brake
gets exercised against real use before anything ever runs unattended, rather than
having its first real test be the situation it exists to protect.

The cost is concrete and was paid in S115. Before that change,
`_validate_agent_proposal_boundary` in
[proposals.py](../../src/rcp/core/validation/proposals.py) refused every agent
Proposal except a single Hypothesis `status` change carrying an `evidence_edge`
cause. An agent asked to reword an existing ResearchQuestion therefore had no
legal move at all: the direct edit was forbidden and the Proposal was refused.
S115 widened the Proposal vocabulary because that widening was the rule's only
legal exit, not a detail beside it.

Two rulings follow, both confirmed 2026-08-12:

- **A content Proposal carries no machine-checkable cause.** `evidence_edge`
  stays required for status changes only, because a status claim is what evidence
  exists to support. Rewording a question is not a claim about the world, and
  demanding an edge for it would force the agent to invent one. The `GatedCard`
  already carries the reasoning in prose.
- **A Proposal carries one intent, not one operation.** Supersede and merge touch
  two nodes, so the old one-operation-one-node limit would leave an agent unable
  to report a genuine duplicate hypothesis. Intent is declared and validated
  against a closed set of shapes — content change, removal, supersede, merge,
  protected relation change, status change — and never inferred from how the
  operations happen to look. An unchecked relaxation is a bundle smuggled
  through, which is exactly the risk this ruling accepted.

[S115](../acceptance/S115-beliefs-change-only-through-you.md) is the promise;
[S100](../acceptance/S100-permission-is-checked-twice.md) owns the two gates.

### A seated worker gets no scope of its own

**Confirmed 2026-08-12, and this deletes the target grammar rather than writing
it.** A worker the orchestrator seats on an Experiment or Blocker is an ordinary
Work agent. What binds it is what already binds every agent: the protected-type
rule, and the shared campaign budget. Its repositories arrive through the
run-scope pointers that already exist.

This is the campaign-scope ruling applied one level down. The reasoning is the
same and so is the refusal: the brakes are the budget and the protected-type
rule, and there is no second fence quietly doing that job.

The cost is real and accepted. A worker seated on one Experiment can touch
another Experiment's nodes, and only its instructions discourage that. Both
alternatives were worse — mechanical seat enforcement is the second fence the
scope ruling exists to prevent, and a scope the orchestrator declares at spawn is
a fence drawn by the thing being fenced, which means nothing by the tenth turn.

### Not settled by this pass
- **How independence is driven once membership exists.** S100 demonstrates the
  two gates through graph movement, because the textbook case — permission
  changing mid-run — needs a permission that can change, which today means team
  membership. The revocation drive is added to S100 when that lands.

## Provenance

Every live admitted action must be traceable to the direct human or root human
authorizer, active agent profile, task and parent/campaign ids, task contract,
resolved scope, execution profile, and existing operation/idempotency identity.

Those facts are split by the question each answers, because operational SQLite
does not travel with a project and canonical history does.

**The patch envelope carries who is responsible, and under what kind of
authority.** It must stay legible standalone, forever, on a repository someone
finds on a disk:

```text
producer:      human | agent | system
authorized_by: { space_id, user_id, display_name } | null
profile:       ordinary | orchestrator | null
task_id:       string | null
```

`system` is reserved for RCP-owned identity and migration revisions. It is not
available to an agent or ordinary request and never widens materialized
`created_by` beyond its legacy human-or-agent meaning.

The confirmed base contract in S99 populates `authorized_by` for every new
human or ordinary-agent Patch, `profile="ordinary"` for an ordinary-agent
Patch, and `task_id` with the direct producing task. Human Patches have no
profile or task id. Current Experiment-loop tasks are ordinary.

`profile="orchestrator"` joined that contract on 2026-08-12, once S77 and S78
were confirmed. It is one added value on a field S99 already made canonical, set
by RCP and never by the agent, and it is the least that lets a campaign turn
avoid signing its work `"ordinary"`. The *campaign, parent, and worker* fields
still do not enter the envelope and wait on
[S113](../acceptance/S113-campaign-attribution.md) — that lineage lives in
operational storage, which is what keeps those choices reversible under
invariant 1.

**Task receipts carry how it was computed**: execution profile, machine,
provider, model, session id, budget spend, task contract, and resolved scope.
These may reasonably require the originating space to interpret.

`display_name` is a **snapshot taken at append time and never re-resolved**. It
is what keeps the record truthful after a project moves to another space, where
`user_id` would otherwise be a meaningless string. It carries the same
consequence as an authorship line in a commit: a name written into append-only
history can never be removed or corrected, by anyone. That is accepted
deliberately.
It is a one-line label capped at 120 characters so a team member cannot amplify
every future task, Patch, and History response with an unbounded stored value.

Three constraints on the change:

- **It is purely additive.** Legacy `Patch.author: "human" | "agent"` remains,
  historical Patch files are never rewritten, and the materialized `created_by`
  field keeps its existing `"human" | "agent"` values so older clients cannot
  misread a name as a role. Attribution is new fields alongside, not a
  redefinition.
- **Replay never requires it.** A patch with no attribution block materializes
  exactly as it does today; renderers show it as an unattributed pre-team
  change rather than guessing or backfilling.
- **Personal spaces participate.** A personal space mints a `space_id` and one
  durable local-owner `user_id`. Before the first newly attributed write, its
  owner chooses an explicit RCP display name that warns it will be snapshotted
  into permanent project history. Reads, Discuss, and the paper coach remain
  available before then; RCP never guesses from the operating-system account.

This is a stored-graph schema change, not a team API change, though it touches
`src/rcp/core/models.py` and `web/src/types.ts` and therefore lands serially
before any consumer work fans out.

The earlier proposal for an empty Patch signature field is not part of this
design. Authentication is provided by server admission. Any future signing
design must not make replay depend on current users or credentials.

## Details still to settle after base attribution

The confirmed boundaries above are ready to be turned into smaller design and
acceptance passes. Those passes still need to specify:

- the target and scope grammar, and the three open items left by the
  [action vocabulary](#action-vocabulary) pass;
- the exact Proposal operation shapes for every permitted modification of an
  existing ResearchQuestion or Hypothesis;
- whether ordinary and orchestrator profiles are fixed, editable, or versioned;
- the campaign, parent-task, and worker extension to the now-settled base Patch
  attribution fields, plus the final immutable receipt schema;
- the permission UI and campaign HTML report lifecycle; and
- how future human or peer-agent messaging consumes budget and authorization.

These details remain with this module rather than as separate entries in the
repository-wide open-question list.

## Acceptance boundaries before implementation

At minimum, separate human-confirmed scenarios must prove:

1. Unauthorized execution never starts.
2. A now-unauthorized semantic change never applies.
3. Completed operational effects are not described as retracted.
4. Replay succeeds without identity or permission data.
5. Ordinary and orchestrator profiles stay distinct.
6. Every agent-produced Proposal waits for a human, and no agent approves one.
7. Spawned workers use ordinary authority and retain their human/task lineage.
8. A space member without project membership cannot read, dispatch, or Apply in
   that project.
9. Attribution written into canonical history stays truthful after the project
   moves to another space.
10. Denials and provenance are visible through a truthful UI.
