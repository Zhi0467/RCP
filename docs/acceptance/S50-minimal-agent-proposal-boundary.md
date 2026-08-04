---
id: S50-minimal-agent-proposal-boundary
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_proposal_boundary.py
  - tests/test_api.py::test_work_patch_adds_decision_edges_to_an_accepted_question_without_correction
  - tests/test_control.py::test_a_loop_proposes_the_belief_change_its_own_evidence_implies
  - tests/test_agent_schema.py::test_agent_created_decisions_and_hypotheses_start_unresolved
  - tests/test_proposals.py::test_agent_withdraws_a_pending_proposal_with_provenance
  - tests/test_proposals.py::test_later_removal_of_a_belief_cause_withdraws_the_stale_proposal
invariants: [3, 10b]
last_passed: 2026-08-03
---

# Agents propose only decisions and belief changes

An agent-authored graph patch is an assertion, not a request for blanket human
approval. A Proposal is reserved for the two semantic transitions the agent may
recommend but may not apply: choosing an upstream Decision and changing a
Hypothesis belief status.

This is a backend contract. It adds no UI path: the existing node review,
Inbox, and Experiment Run surfaces render the resulting asserted nodes,
Proposals, and readiness state.

## Scenario

- An agent creates an asserted/open/unselected Decision, an
  asserted/proposed Hypothesis, Evidence, Blockers, and valid relations among
  new or existing nodes. The patch applies directly; touching an accepted
  endpoint does not create or require a Proposal.
- An agent updates ordinary node content, including accepted content. The patch
  applies and any changed accepted node returns to `standing="asserted"` for
  ordinary node review.
- An agent may not create a Decision already selected or decided, or create a
  Hypothesis with a non-default belief status. It instead creates the ordinary
  asserted node and a Proposal for the human-authoritative transition.
- Selecting, changing, reopening, or otherwise changing a Decision's choice is
  rejected as a direct agent update. It succeeds as a Proposal only when an
  Experiment uses that Decision through a `governed_by` edge already in the
  graph or asserted in the same outer patch. The Decision may itself be created
  earlier in that outer patch.
- Changing a Hypothesis status is rejected as a direct agent update and succeeds
  only as a Proposal with an `evidence_edge` cause naming a valid
  Evidence-to-Hypothesis epistemic edge. No other agent belief-cause kind is
  admitted. An experiment-loop belief Proposal targets one hypothesis tested by
  that experiment and is grounded by an evidence edge from the same patch.
- An experiment loop may propose changes only to its pinned governing Decisions
  or to the status of a tested, same-patch-evidence-grounded Hypothesis.
- `set_standing`, approving or rejecting a Proposal, and pressing Experiment
  **Run** remain human-only actions. An agent may withdraw an obsolete pending
  Proposal with the dedicated lifecycle operation; withdrawal never replays
  the Proposal's semantic operations. Proposal approval never starts or
  resumes an experiment; the human presses **Run** again.
- A belief Proposal's Evidence edge remains a live dependency. If it disappears
  or changes before judgment, the Proposal is stale and is withdrawn instead of
  failing during approval. Creating and then removing the cause in the proposal's
  own patch is rejected at admission.
- Project configuration remains human-authoritative: ontology changes use
  Settings/Sync, and project truth-scope operations are accepted only in human
  patches. Neither is an agent Proposal shape.

## Assertions

- `agent_edges_never_require_proposals_because_an_endpoint_is_accepted`
- `ordinary_agent_updates_clear_accepted_standing_instead_of_becoming_proposals`
- `agent_created_decisions_start_open_and_unselected`
- `agent_created_hypotheses_start_proposed`
- `decision_choice_transitions_require_proposals`
- `decision_proposals_require_an_experiment_governed_by_edge`
- `hypothesis_status_transitions_require_proposals`
- `hypothesis_proposals_require_an_evidence_edge_cause`
- `experiment_loop_proposals_have_exactly_two_admissible_shapes`
- `standing_and_run_authority_remain_human_only`
- `agents_cannot_approve_or_reject_proposals`
- `belief_causes_survive_until_approval_or_make_the_proposal_stale`

## Failure means

RCP rejects ordinary asserted graph structure because it touches accepted
content; admits a direct agent decision or belief transition; permits a third
agent Proposal shape; lets an agent set standing; or lets Proposal resolution
start an experiment without another explicit human **Run**.
