---
id: S67-proposal-action-legibility
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_prompts.py::test_graph_contract_keeps_fanout_and_points_to_payload_files
  - web/tests/inlineGlossary.test.mjs
invariants: [3]
---

# Pending proposals state what would change

When a human reviews a pending proposal, the card states the exact option or
status transition the agent is asking them to approve. The card may derive this
from the stored operation for older proposals, while newly generated proposals
are instructed to write the same action in plain prose.

## Drive

1. Open a project with a pending proposal and open its Inbox.
2. Read the proposal card before choosing Approve or Reject.

## Assert

- The card includes a **Proposed action** row naming the exact option or status
  transition.
- Approve and Reject remain the human controls for that stored operation.
- A proposal with an older or incomplete card still shows a useful action
  derived from its replay operation.
