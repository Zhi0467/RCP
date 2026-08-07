---
id: S86-human-decides-a-decision
status: implemented
tier: hermetic
driver: pytest + browser
covered_by:
  - tests/test_sync.py::test_graph_sync_directly_decides_an_ungoverned_decision
  - tests/test_sync.py::test_graph_sync_direct_choice_atomically_withdraws_same_decision_proposals
  - tests/test_sync.py::test_direct_choice_withdraws_a_replay_valid_mixed_target_legacy_proposal
  - tests/test_sync.py::test_direct_choice_validator_requires_every_targeted_proposal_withdrawal
  - tests/test_sync.py::test_graph_sync_rejects_incoherent_direct_decision_choice
  - tests/test_proposal_boundary.py::test_decision_proposal_requires_a_coherent_choice_transition
  - tests/test_proposal_boundary.py::test_legacy_decision_selection_approval_adds_implied_decided_status
  - web/tests/humanDraft.test.mjs
  - web/tests/decisionChoice.test.mjs
  - browser 2026-08-07
invariants: [1, 3]
reported_by: human, 2026-08-07
last_passed: 2026-08-07 — direct choice, persistence, replacement, Reset, and Sync driven in browser; pytest and web suites passed
---

# A human decides a Decision by clicking the option

A Decision node exists to record a choice a human made. Until now the human
could not make it. `selected_option` and `status` were reachable only through an
agent Proposal, so the only way to decide was to wait for an agent to guess an
option and then approve or reject that one guess. A Decision with three listed
options and no Proposal was unanswerable: the options rendered as one more prose
block in **Context**, indistinguishable from Reasoning and Consequences, and
nothing on the screen could be clicked.

The options are the point of the node, so they get their own control. Selecting
one is a direct human authority action, staged and Synced through the same
project draft as a node edit or Proposal decision. It is not an ordinary node
edit: editing describes the Decision, while selecting decides it.

This is also the first concrete UI expression of the actor-profile direction.
The graph operation is an authority action independent of its affordance. The
human gets a choice control; a future agent profile may be allowed to invoke the
same action through its own surface. This scenario does not build that general
permission system.

## UI path

1. In every Decision's detail window, the question and options appear in a
   dedicated choice section above **Context**. Each option is a full clickable
   row in one accessible single-choice group, visually distinct from Reasoning
   and Consequences. The current **Open**, **Decided**, **Revisit**, or
   **Superseded** status is readable beside the choice.
2. On an Open or Revisit Decision, one click on an option stages that option as
   `selected_option` and stages `status: decided`. On a decided Decision,
   clicking a different option replaces the choice. Exactly one row is selected
   at a time, and the selected row remains unmistakable without relying on
   color alone. A Superseded Decision remains historical and is not selectable.
3. The selection stages in the same project-wide draft as node edits, judgments,
   and Proposal decisions. It is visible as staged until the human presses
   **Sync**, survives closing and reopening the node window, and can be replaced
   or reset with the existing staged-draft controls before Sync.
4. Selecting an option also stages the node as **accepted**: a human who picks
   the option has endorsed the record, so the judgment follows the decision
   rather than being a second click. This is a judgment, not an edit — it never
   goes through the standing-reset path that ordinary content edits use. **Agree**
   and **Contest** remain the controls for a later independent judgment.
5. If one or more pending Proposals target the same Decision, the choice section
   says so. Staging a direct choice supersedes any separately staged resolution
   for those Proposals. Sync commits the human choice and withdraws every still-
   pending Proposal for that Decision atomically as stale, rather than leaving
   the Inbox able to re-decide a question the human already answered. This is a
   withdrawal, not an approval or rejection: the human chose directly and did
   not implicitly judge the agent's rationale.
6. An option the human wants but the agent did not list is added through **Edit
   node**, which already owns `options`. Selection chooses among the listed
   options only.
7. A decided Decision reads as decided everywhere it already claimed to: the
   Experiment **Run** gate that requires every `governed_by` Decision to be
   decided with a selected option is satisfied by a human selection with no
   agent Proposal involved.
8. The same semantic rule closes an existing hole on the Proposal path. New
   Decision Proposals must describe a coherent transition: choosing a listed
   option means `status: decided`. When approving an already-recorded Proposal
   that selects an option but omitted status, RCP records the implied
   `status: decided` in the approval rather than preserving an open-but-selected
   state. A Proposal that says `decided` without a listed selection is refused.

## Assert

- Direct Decision choice is a dedicated human-authority transition. It is
  accepted through the service and approval validator without adding
  `selected_option` or Decision status to the ordinary node editor's field set.
  The backend, not the presence of a button, enforces that authority boundary.
- A staged selection whose value is not one of the node's current `options` is
  rejected with a diagnostic naming the node.
- Sync commits an approval Patch carrying `selected_option`, `status`, and the
  accepted judgment, authored `human`; the patch log stays append-only.
- Syncing a selection on a Decision with pending Proposals commits the selection
  and every targeted Proposal withdrawal in one atomic Patch, and the history
  entry explains that the direct human decision made the Proposals stale.
- A selection Sync records `accepted` standing through the judgment path, not
  the edit path, so it does not trip the reset-on-edit rule.
- Approving a Decision Proposal that carries `selected_option` and no `status`
  yields a Decision with `status == "decided"`.
- After Sync, `experiment_control` reports the Decision as satisfied for every
  Experiment that `governed_by` it, and the pinned option recorded on a
  subsequent attempt is the human-selected one.
- In the browser: clicking an option marks the draft dirty, clicking another
  replaces it, resetting the draft restores canonical selection, closing and
  reopening the window preserves the staged selection, keyboard selection
  works, and no console or network errors occur across the sequence.

## Not this scenario

Per-identity affordance in general — including an orchestrator whose profile
permits an action through configuration rather than a human click — is the
direction this belongs to, not the change. See the
[actor-identity handoff](../handoffs/handoff-2026-08-07-actor-identity-and-permissions.md).
