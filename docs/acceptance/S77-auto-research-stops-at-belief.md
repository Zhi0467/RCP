---
id: S77-auto-research-stops-at-belief
status: pending — not human-confirmed
tier: hermetic
driver: pytest
covered_by: none
invariants: [3, 4, 10b]
---

# Auto-research runs the action layer and stops at belief

This scenario is a proposal and is **not yet human-confirmed**. The design is
settled in
[the orchestrator handoff](../handoffs/handoff-2026-08-07-orchestrator.md).

The orchestrator owns *what we do to find out*. The human owns *what we ask and
what we believe*. This scenario is that line, and nothing about it lives in the
browser — its sibling [S78](S78-one-budget-one-stop.md) owns the lifecycle the
human watches.

## Setup

A project with a ResearchQuestion, two Hypotheses, an open governed Decision, a
ready Experiment, and an open Blocker. An orchestrator actor holding the
elevated profile from [S92](S92-actor-identity-and-permission-checks.md).

## Drive — proposal

1. Start a campaign and let the orchestrator take a turn that queues the
   governed Decision as `ready`, sets an Experiment status, and resolves the
   Blocker.
2. Let a turn attempt a Hypothesis status change grounded in an Evidence edge.
3. Let a turn attempt to set node standing, and to approve a pending Proposal.
4. Let a turn attempt to seat a worker on the Decision, then on the
   ResearchQuestion, then on the Experiment and the Blocker.
5. Confirm the Experiment remains gated until the human decides its governing
   Decision, then read its readiness again after that choice.

## Assert

- `orchestrator_queues_decisions_and_sets_action_layer_status_directly`
- `orchestrator_resolves_blockers_without_a_proposal`
- `hypothesis_status_movement_arrives_as_a_proposal_not_an_applied_change`
- `research_question_authoring_is_refused`
- `orchestrator_cannot_set_standing`
- `orchestrator_cannot_approve_or_reject_any_proposal_including_its_own`
- `orchestrator_seats_workers_only_on_experiments_and_blockers`
- `seating_refusal_does_not_reduce_direct_authority_over_the_same_node`
- `governing_decision_readiness_remains_gated_on_the_human_choice`
- `evidence_creation_remains_ordinary_agent_authoring`

## Boundary

Seating scope and authority scope are different. The orchestrator may not seat a
worker on a Decision and may queue that Decision itself, but it may not write
the choice. A refusal in step 4 must not be read as a restriction on the queue
transition in step 1.

Exactly one profile carries this authority. This scenario does not promise a
family of elevated agents.

Budget accounting, Stop, and the Runs surface belong to
[S78](S78-one-budget-one-stop.md).
