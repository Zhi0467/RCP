---
id: S77-auto-research-stops-at-belief
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_s77_auto_research.py
  - tests/test_auto_research_authority.py
  - tests/test_auto_research_commands.py
  - tests/test_auto_research_stream.py
invariants: [3, 4, 10b]
---

# Auto-research creates freely and proposes changes to existing epistemic nodes

Confirmed 2026-08-12. The design is settled in
[the orchestrator handoff](../archive/handoffs/handoff-2026-08-07-orchestrator.md) and
[Identity, permissions, and agent profiles](../design/identity-permissions-and-agent-profiles.md#a-proposal-is-an-escalation-to-a-human).

It depends on [S115](S115-beliefs-change-only-through-you.md), which builds the
protected-type rule and the widened Proposal vocabulary this scenario exercises
from the orchestrator's side. Do not implement it first.

Step 9's finish boundary was confirmed by the human on 2026-08-16 after an
orchestrator ended with unused budget at a self-resolvable infrastructure
Blocker.

The orchestrator may structure and conduct the research, including creating new
questions and hypotheses and directly controlling every other graph node type.
Once a ResearchQuestion or Hypothesis exists, the orchestrator changes it only
through a Proposal, and **that Proposal waits for a human**. This scenario is
that line, and nothing about it lives in the browser — its sibling
[S78](S78-one-budget-one-stop.md) owns the lifecycle and episode report the
human watches.

An earlier version of this scenario let the orchestrator approve a Proposal
produced by an eligible ordinary child while barring it from approving its own.
That rule did not bind: the orchestrator writes the instructions for the child
whose Proposal it would then approve, so the route around it was one step long,
and it cost an extra paid invocation. It is removed.

## Setup

A project with a ResearchQuestion, two Hypotheses, an open governed Decision, a
ready Experiment, and an open Blocker. A project-owned orchestrator using the
elevated profile from the
[permission design](../design/identity-permissions-and-agent-profiles.md#project-orchestrator-profile).

## Drive

1. Start an Auto-research episode and let the orchestrator create a new ResearchQuestion and
   Hypothesis, create Evidence and relations, and choose the governed Decision.
2. Let it set lifecycle and standing on the Decision, Experiment, Blocker, and
   Evidence, and remove one expendable node of each type.
3. Let it attempt ordinary content, status, standing, relation, and removal
   changes against existing ResearchQuestions and Hypotheses. Confirm each
   change can be raised as a Proposal but cannot apply directly.
4. Let the orchestrator attempt to approve its own Proposal. Then let a human
   judge it.
5. Give an ordinary child work it cannot resolve without changing an existing
   Hypothesis. Read what the child returns and what reaches the graph.
6. Let a turn attempt to seat a worker on the Decision, then on the
   ResearchQuestion, then on the Experiment and the Blocker.
7. Confirm the orchestrator's Decision choice satisfies the Experiment's
   governing gate without another human approval.
8. Let the episode continue on independent work while protected Proposals
   remain pending.
9. Give the orchestrator a Blocker whose remaining prerequisite is delayed by
   temporary compute capacity but can be resolved with its existing authority
   and tools. Let the seated worker settle after diagnosing that path. Confirm
   the orchestrator uses the remaining episode budget to act, delegate, or
   arrange an observable continuation instead of treating the settled child,
   the open Blocker, or the downstream human-only Experiment launch as normal
   episode completion.

## Assert

- `orchestrator_decides_decisions_and_sets_action_layer_status_directly`
- `orchestrator_resolves_blockers_without_a_proposal`
- `orchestrator_directly_creates_new_questions_and_hypotheses`
- `orchestrator_has_full_direct_control_of_evidence`
- `orchestrator_sets_standing_on_decisions_experiments_blockers_and_evidence`
- `every_orchestrator_change_to_an_existing_question_or_hypothesis_is_a_proposal`
- `no_agent_approves_any_proposal`
- `every_agent_produced_proposal_waits_for_a_human`
- `a_human_can_judge_the_orchestrators_proposal`
- `a_blocked_child_reports_the_difficulty_in_its_answer`
- `orchestrator_seats_workers_only_on_experiments_and_blockers`
- `seating_refusal_does_not_reduce_direct_authority_over_the_same_node`
- `governing_decision_readiness_accepts_the_orchestrator_choice`
- `pending_epistemic_review_does_not_stop_independent_campaign_work`
- `agent_resolvable_blockers_and_temporary_capacity_do_not_finish_the_episode`

## Boundary

Seating scope and authority scope are different. The orchestrator may not seat a
worker on a Decision, but it may choose that Decision itself. A refusal in step 6
must not be read as a restriction on its direct Decision authority in step 1.

**A seated worker gets no scope of its own** (confirmed 2026-08-12). Where it may
be seated is bounded; what it may then touch is not bounded separately. It is an
ordinary Work agent held by the protected-type rule and the shared budget, with
repositories arriving through the run-scope pointers that already exist. A worker
seated on one Experiment can reach another Experiment's nodes, and only its
instructions discourage that — the accepted cost of refusing a second fence
beside the budget. This scenario must not assert a mechanical seat boundary,
because there is deliberately none to assert.

Likewise, the boundary is new versus existing for ResearchQuestions and
Hypotheses. Creation is direct. Every later modification is Proposal-only, and
Proposal-only now means human-only. Evidence is not inside that protected core.

**Sub-agent scoping is a prompt contract, not a mechanism.** RCP instructs the
orchestrator to give its children clear executable work and to have them state
difficulties in prose rather than reaching for a Proposal. Step 5 asserts that
this is what happens under the shipped prompt; it does not assert that RCP
mechanically prevents a child from producing one, and nothing may be built on
the assumption that it does.

Human-initiated work outside an Auto-research episode is unchanged: an ordinary Work task a
person starts still produces a Proposal when it touches a gated operation,
because that person is there to judge it.

A Blocker is an operational fact, not a human-authority boundary by type. Before
calling `finish`, the orchestrator must identify the exact remaining dependency.
If no new human judgment, credential, approval, privileged action, or
coordination with another person is required, it keeps going within the existing
episode authorization. Temporary resource occupancy and a downstream
human-started Experiment do not excuse leaving agent-resolvable preparation
unfinished. The orchestrator acts directly, seats another executable worker, or
arranges a durable observable continuation; it does not busy-poll or merely keep
a provider turn open.

Exactly one profile carries this authority. This scenario does not promise a
family of elevated agents.

Budget accounting, Stop, and the Runs surface belong to
[S78](S78-one-budget-one-stop.md).
In-turn Apply, child Experiment kickoff, lifecycle delivery, and the mechanical
finish guard belong to pending [S123](S123-auto-research-orchestrates-in-one-turn.md)
and [S124](S124-auto-research-harvests-child-lifecycle.md); this implemented
scenario does not claim those commands already exist.
