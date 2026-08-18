---
id: S69-agent-proposal-withdrawal
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_agent_schema.py::test_agent_can_withdraw_a_pending_proposal
  - tests/test_proposals.py::test_agent_withdraws_a_pending_proposal_with_provenance
invariants: [3, 10b]
---

# Agents can withdraw obsolete proposals

An agent may explicitly withdraw any pending Proposal when a later turn makes
that Proposal obsolete or duplicated. Withdrawal is a lifecycle action only:
it applies no Proposal semantic operations and cannot approve or reject the
Proposal. RCP preserves creation and withdrawal provenance on the historical
Proposal record.

## Assert

- The agent patch schema exposes a dedicated `withdraw_proposals` operation.
- The operation may name any pending Proposal and include a plain-language
  reason.
- The Proposal becomes `withdrawn` without replaying its stored semantic ops.
- Approval and rejection remain human-only operations.
- The resulting Proposal records the creating and withdrawing task ids when
  those ids are available.
