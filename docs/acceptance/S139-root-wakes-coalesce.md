---
id: S139-root-wakes-coalesce
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_auto_research_delivery.py::test_root_wake_coalesces_notices_and_mail_with_lifecycle_grace
  - tests/test_auto_research_delivery.py::test_watcher_wake_prefix_race_rolls_back_allocation_and_claims
  - tests/test_auto_research_delivery.py::test_active_child_reply_waits_and_coalesces_with_its_lifecycle_notice
  - tests/test_auto_research_children_storage.py::test_lifecycle_wake_invalid_inputs_roll_back_paid_task
  - tests/test_auto_research_children_storage.py::test_non_root_watcher_wake_refuses_even_empty_root_claims
  - tests/test_auto_research_commands.py::test_orchestrator_messages_spawned_child_work_by_stable_worker_id
  - tests/test_auto_research_commands.py::test_unknown_message_recipient_has_readable_diagnostic
last_passed: >-
  2026-09-08 — focused Auto-research delivery, storage, and command suites plus
  the full backend suite, Ruff, and pre-commit passed on the implementing branch.
invariants: [3, 4b, 8, 10g]
reported_by: >-
  human-confirmed fix for two cost observations on the live episode fd79bb7c
  (dark matter denoising, 2026-09-08), where three of six invocations were wakes
  and every orchestrator-to-worker message failed
---

# One settlement, one paid wake

When a worker settles, the root orchestrator learns everything that settlement
produced in a single paid turn: the graph condition it armed, the RCP lifecycle
notice, and any mail waiting for it. The orchestrator can also address the
workers it spawned. Nothing about this promise lives in the browser; it is
admission, budget spending, and delivery.

## Setup

An Auto-research episode on a graph branch with a ceiling of ten invocations.
The root orchestrator has a saved native session and stage. A deterministic
provider that answers each turn immediately.

## Drive

1. Arm a graph condition from the orchestrator on a worker's Experiment. Record
   two lifecycle notices and one human message to the orchestrator, the second
   notice inside the grace window.
2. Ask for lifecycle delivery. Confirm nothing wakes, nothing is spent, and the
   notices and mail stay pending.
3. Ask for ordinary mail delivery to the orchestrator. Confirm it also waits.
4. Complete the graph condition and deliver its group. Confirm exactly one wake
   whose allocation carries both notices and the mail, the watcher is marked
   notified with that wake, and one invocation is spent.
5. Alternatively let the grace window pass and ask for lifecycle delivery.
   Confirm the same single wake carrying notices and mail.
6. Between reading pending inputs and admitting the wake, add a notice or a
   message. Confirm the wake is refused, the allocation rolled back, the watcher
   left unnotified, and a later pass succeeds.
7. From the orchestrator, send a message to a spawned child worker by its worker
   id. Confirm the message is recorded and addressed to that worker. Send one to
   an unknown id and confirm a readable refusal that names the id.

## Assert

- `a_root_graph_condition_wake_claims_pending_notices_and_root_mail_in_its_admission`
- `lifecycle_delivery_waits_out_the_grace_window_before_spending`
- `root_mail_waits_while_a_lifecycle_notice_is_pending`
- `a_changed_notice_or_mail_prefix_rolls_the_whole_allocation_back`
- `only_the_root_orchestrator_wake_may_claim_notices_or_mail`
- `a_child_reply_is_held_until_its_attempt_settles_and_travels_with_its_notice`
- `the_orchestrator_addresses_a_spawned_worker_by_its_stable_worker_id`
- `an_unknown_mail_recipient_is_refused_with_a_readable_diagnostic`

## Boundary

Mail stays Markdown hearsay with no graph authority; `patch.json` remains the
only graph channel. The claim and the paid allocation commit in one SQLite
transaction or not at all. The grace window delays a lifecycle wake by at most
its length; a graph-condition wake fired inside it is not delayed. Legacy
`auto_research`-kind worker recipients and child Work watcher wakes are outside
this promise: they claim nothing for the root.

The current contract is in
[Mail and lifecycle notices](../specs/auto-research-and-branch-merge.md#mail-and-lifecycle-notices).
[S76](S76-graph-condition-wake.md) covers the single-conversation graph
condition; this scenario is the orchestrator's cross-input delivery.
