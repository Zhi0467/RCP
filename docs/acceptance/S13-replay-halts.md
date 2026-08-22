---
id: S13-replay-halts
status: implemented
tier: hermetic
driver: pytest
covered_by:
  - tests/test_history.py::test_malformed_agent_patch_is_auditable_without_poisoning_replay
  - tests/test_history.py::test_tampered_accepted_patch_halts_before_it_and_blocks_later_writes
  - tests/test_api.py::test_degraded_replay_is_visible_and_canonical_api_writes_are_blocked
pins_current_behavior:
  - tests/test_history.py::test_malformed_agent_patch_is_auditable_without_poisoning_replay
  - tests/test_history.py::test_invalid_agent_patch_is_auditable_but_not_materialized
invariants: [1]
last_passed: >-
  2026-08-22 — re-run after replay stopped halting on RCP's own in-memory
  migration. A tampered or structurally invalid accepted revision still halts and
  still blocks canonical writes; what no longer halts is a legacy Patch whose
  adapter retires a value or drops a field the current payload forbids. All four
  of the human's real project histories replay to their head.
spec: ../specs/graph-history-and-transitions.md#append-only-history-and-replay
---

# A bad patch stops replay instead of vanishing

RCP distinguishes a patch rejected when authored from an accepted patch whose
structural integrity later fails. A recorded rejection advances history but
never enters graph truth. A structural failure in an accepted patch halts replay
at the last coherent revision; later patches are not attempted.

There are two different situations behind that one `continue`, and the promise
differs between them:

- **A patch that was rejected when it was written.** It never entered the graph,
  its rejection is recorded, and skipping it on replay is correct. Nothing
  silent happens.
- **A patch that was accepted when written and fails a rule added later.** It
  *was* part of the graph and now silently is not. The graph becomes a function
  of *(the log, whatever the code currently thinks)* rather than of the log
  alone, so the same log opens differently on two versions.

RCP persists its admission verdict and messages on the canonical patch.
Rejected patches stay auditable and are skipped for that recorded reason;
accepted patches alone undergo replay-time structural validation. The
agent-facing schema omits those receipt fields, and the history manager
overwrites supplied values rather than trusting agent output.

Structural rules run on admission and replay. Authoring rules run only on
admission, so tightening an authoring policy cannot retroactively erase work.

The two tests named in `pins_current_behavior` exercise the first situation and
now pin the recorded-rejection branch. Keep them coupled to the
structural/authoring split when validation changes.

---

## UI path

### What the person sees

Opening a project whose log cannot fully replay shows the last coherent graph
under a persistent **Replay degraded** banner. The banner names the failed
revision, structural rule, and reason, and explicitly says the visible state is
the last coherent graph.

Canonical mutation controls are unavailable: Sync, seed/Refresh, graph-authorized
Ask, node edits and judgments, proposal decisions, and settings writes cannot
advance history. Read-only Ask, history inspection, and non-authoritative paper
work remain available. There is no in-app quarantine or repair action in v0.5;
repair is an explicit development/operations action outside the graph UI.

---

## Drive

Exercise three logs: one containing a recorded admission rejection followed by
a valid patch; one containing an accepted patch whose file was structurally
tampered followed by another patch; and one whose authoring rule would be
rejected today but whose stored accepted receipt predates that rule.

## Assert

- `replay_halted` — it stopped at the bad patch; it did not continue past it
- `failure_names_the_patch` — by revision, code, and reason
- `no_partial_graph_presented_as_complete`
- `nothing_was_deleted` — the log is untouched; halting is a read-time refusal,
  not a repair
- `structural_vs_authoring_distinguished` — a rule that tightened after the fact
  must not halt replay; only rules whose verdict can never change do
- `canonical_writes_are_blocked_while_read_only_chat_remains_available`

## Failure means

Graphs silently disagree across versions, and nobody finds out until two people
compare the same project and see different research.
