---
id: S115-beliefs-change-only-through-you
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_proposal_boundary.py
  - tests/test_agent_schema.py
  - tests/test_staged_graph_validation.py
  - tests/test_proposals.py
  - tests/test_proposal_judgment.py
  - web/tests/proposalJudgment.test.mjs
invariants: [1, 3, 4, 10b]
last_passed: 2026-08-12 — focused authority and judgment checks plus an isolated
  served Inbox drive across all six Proposal intents
---

# An agent may rewrite anything except what you believe

An agent can do almost anything to this project. It can run experiments, record
evidence, resolve blockers, decide decisions, and add whatever structure the work
needs. There are two things it cannot quietly change: the questions you are
asking and the hypotheses you hold. Those are your beliefs. An agent may argue
for a change to one, and then it waits for you.

This is the brake. [S76](S76-graph-condition-wake.md) makes agents that sleep and
wake on their own for hours. Auto-research will make that ordinary. The brake has
to exist before the thing it brakes.

## What changes for you today

Right now, if you ask an agent to reword a research question, it rewords it. The
question drops back to asserted standing and you find out afterwards.

After this, it raises the reword in your Inbox and the question does not move
until you judge it. Every other node type behaves exactly as it does now.

This applies to every agent from the day it lands, including an ordinary Work
turn you started and are watching. You are there to judge it, so the judgment
costs you one click and the rule stays one rule.

## The gap this has to close

The rule cannot be enforced as the code stands.
[`_validate_agent_proposal_boundary`](../../src/rcp/core/validation/proposals.py)
refuses every agent Proposal except a single Hypothesis status change carrying an
`evidence_edge` cause. An agent asked to propose a reworded question has no legal
move at all: the direct edit is forbidden by the new rule, and the Proposal is
rejected by the old one.

So widening what a Proposal may say is not a companion to this rule. It is the
only exit the rule leaves open, and it is most of the work.

## Three decisions, confirmed 2026-08-12

1. **A content Proposal carries no machine-checkable cause.** The `evidence_edge`
   requirement stays on status changes only, because a status claim is the thing
   evidence exists to support. Rewording a question is not a claim about the
   world, and demanding an evidence edge for it would force the agent to invent
   one. The Proposal card already carries the reasoning in prose.
2. **A Proposal carries one intent, not one operation.** Today it must be exactly
   one operation on exactly one node. Superseding and merging inherently touch
   two, so an agent that finds a genuine duplicate hypothesis would have no way to
   say so. The limit becomes one intent spanning the nodes that intent needs, and
   the Inbox still shows a single question to answer.

   The cost is taken deliberately: validation must now establish that the
   operations in one Proposal really are one intent, rather than a bundle
   smuggled through a relaxed limit. Intent is declared and checked against a
   closed set of shapes, never inferred from how the operations happen to look.
3. **One explicit orchestrator profile.** Its deliberate Decision exception is
   defined in [Agent profiles](../specs/authority-and-proposals.md#agent-profiles),
   not inferred from campaign wording.

The human also chose to build the dispatch-time gate alongside this. That promise
belongs to [S100](S100-permission-is-checked-twice.md), which shares this
scenario's action table but is a separate, narrower drive.

## Setup

A project with an accepted ResearchQuestion, an accepted Hypothesis with Evidence
attached, a second Hypothesis, an open Decision, a ready Experiment, and an open
Blocker. A deterministic agent.

## Drive — proposal

1. Ask Work to reword the existing ResearchQuestion. Read the graph and the
   Inbox.
2. Approve that Proposal. Read the question and its history.
3. Ask for another reword and reject it. Read the question.
4. Ask Work to create a new ResearchQuestion and a new Hypothesis, connect them,
   and edit the one it just created — all in one turn.
5. Ask Work to attach Evidence to the existing Hypothesis, and to create, edit,
   and resolve the Blocker, the Experiment, and the Decision.
6. Ask Work to remove the existing Hypothesis. Then to supersede it. Then to
   merge it with the second one.
7. Ask Work to change the existing Hypothesis's status, citing its Evidence edge.
8. Ask Work to set standing on anything, and to approve a pending Proposal.
9. Reword the ResearchQuestion yourself in the prose editor and Sync.
10. Replay the project with every profile and permission record deleted.

## Assert

- `an_agent_edit_to_an_existing_question_becomes_a_proposal_not_a_change`
- `the_question_does_not_move_until_the_human_judges_it`
- `approving_applies_the_edit_and_records_who_approved_it`
- `rejecting_leaves_the_question_exactly_as_it_was`
- `creating_a_new_question_or_hypothesis_still_applies_directly`
- `a_node_created_in_the_same_patch_can_be_edited_and_connected_in_that_patch`
- `attaching_evidence_to_an_existing_hypothesis_stays_direct`
- `evidence_decisions_experiments_and_blockers_are_untouched_by_the_rule`
- `removing_superseding_or_merging_an_existing_belief_becomes_a_proposal`
- `a_status_change_still_requires_an_evidence_edge_cause`
- `a_content_proposal_needs_no_evidence_edge_cause`
- `one_proposal_carries_one_intent_across_the_nodes_that_intent_needs`
- `an_agent_never_sets_standing_and_never_approves_a_proposal`
- `the_human_prose_editor_and_sync_are_unaffected_by_the_rule`
- `the_action_is_derived_from_the_operation_never_guessed_from_its_shape`
- `the_inbox_renders_and_judges_every_new_proposal_kind`
- `replay_succeeds_with_no_profile_or_permission_records`

## UI path — proposal

The Inbox is where this becomes real. A rule that manufactures Proposals nobody
can read is worse than no rule, because the queue fills with items the human
cannot act on and stops being trustworthy.

- A **reword** Proposal shows the current wording and the proposed wording, so
  the judgment is a comparison rather than a reconstruction.
- A **removal** Proposal shows what goes with it — an accepted node's incident
  edges are part of what you are agreeing to lose.
- A **supersede** or **merge** Proposal shows both nodes, because one of them
  alone does not state the question.
- Approve and Reject are the existing controls. No new destination, no new
  vocabulary, and no explanatory line under anything.

## Boundary

Not team membership, not the orchestrator, and not campaign scope. Those are
[S100](S100-permission-is-checked-twice.md),
the [orchestration command contract](../specs/authority-and-proposals.md#orchestration-commands),
and [S78](S78-one-budget-one-stop.md). This scenario does not restate those
separate boundaries.

Attaching Evidence to a Hypothesis stays direct, even though it argues for a
status change. The status change is gated one layer down, and gating both would
route every Seed and Refresh through the Inbox.

Your own edits are not agent edits. Correcting node wording in the prose editor
stays a literal human edit that Sync commits, and this rule never touches it.

Rejecting a Proposal here is not undoing work, because nothing operational
happened — the agent argued and stopped. The harder case, where a task already
wrote a repository before its patch was refused, belongs to
[S100](S100-permission-is-checked-twice.md).

## Failure means

An agent quietly rewrites the question you are trying to answer, or the
hypothesis you are trying to test, and you find out when the paper no longer
matches what you remember deciding.
