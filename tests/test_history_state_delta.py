from __future__ import annotations

from dataclasses import dataclass

import pytest

from rcp.core.models import BeliefTransition, GraphState, ValidationMessage
from rcp.core.validation.report import ValidationReport
from rcp.history.memory_size import retained_size
from rcp.history.state_delta import StateDelta, apply_state_delta, capture_state_delta, copy_state


def test_accumulating_bookkeeping_stores_only_tails_and_reconstructs_exactly():
    state = GraphState()
    stored_items = []
    for revision in range(1, 129):
        after = state.model_copy(
            update={
                "revision": revision,
                "validation_messages": [
                    *state.validation_messages,
                    ValidationMessage(
                        level="reject" if revision % 2 else "flag",
                        code="history-test",
                        message=f"Receipt {revision}",
                        patch_revision=revision,
                    ),
                ],
                "belief_transitions": [
                    *state.belief_transitions,
                    BeliefTransition(
                        hypothesis_id="claim/example",
                        from_status="open",
                        to_status="supported",
                        revision=revision,
                        cause={"event": f"event-{revision}"},
                    ),
                ],
            }
        )
        delta = capture_state_delta(state, after)
        assert set(delta.replacements) == {"revision"}
        assert set(delta.splices) == {"validation_messages", "belief_transitions"}
        for splice in delta.splices.values():
            assert splice.start == revision - 1
            assert splice.delete_count == 0
            assert len(splice.items) == 1
        stored_items.append(sum(len(splice.items) for splice in delta.splices.values()))
        restored = apply_state_delta(state, StateDelta.model_validate_json(delta.model_dump_json()))
        assert restored == after
        assert state.revision == revision - 1
        state = restored
    assert sum(stored_items) == 2 * sum(stored_items[: len(stored_items) // 2])


@pytest.mark.parametrize(
    ("old", "new", "start", "deleted", "items"),
    [
        ([], ["a"], 0, 0, ["a"]),
        (["a"], [], 0, 1, []),
        (["a", "b", "c"], ["a", "x", "y", "c"], 1, 1, ["x", "y"]),
        (["a", "b", "c"], ["a", "c"], 1, 1, []),
        (["a", "c"], ["a", "b", "c"], 1, 0, ["b"]),
        (["a", "b"], ["b", "a"], 0, 2, ["b", "a"]),
    ],
)
def test_list_splices_preserve_unchanged_prefix_and_suffix(old, new, start, deleted, items):
    before = GraphState(project_truth_scope=old)
    after = before.model_copy(update={"project_truth_scope": new})
    delta = capture_state_delta(before, after)
    assert delta.splices["project_truth_scope"].model_dump() == {
        "start": start,
        "delete_count": deleted,
        "items": items,
    }
    assert apply_state_delta(before, delta) == after
    assert before.project_truth_scope == old


def test_splice_rejects_out_of_bounds_predecessor():
    delta = StateDelta.model_validate(
        {"splices": {"project_truth_scope": {"start": 1, "delete_count": 0, "items": []}}}
    )
    with pytest.raises(ValueError, match="exceeds its predecessor"):
        apply_state_delta(GraphState(), delta)


def test_snapshot_copy_isolates_nested_bookkeeping():
    state = GraphState(
        validation_messages=[
            ValidationMessage(
                level="flag", code="test", message="test", cause_chain=[{"ids": ["one"]}]
            )
        ]
    )
    copied = copy_state(state)
    copied.validation_messages[0].cause_chain[0]["ids"].append("two")
    assert state.validation_messages[0].cause_chain == [{"ids": ["one"]}]


def test_memory_accounting_deduplicates_models_and_slotted_dataclasses():
    @dataclass(slots=True)
    class Holder:
        state: GraphState

    state = GraphState(project_truth_scope=["claim/example"])
    first, second = Holder(state), Holder(state)
    assert retained_size([first, second]) < retained_size([first]) + retained_size([second])
    assert retained_size(first) > retained_size(state)
    report = ValidationReport()
    empty_size = retained_size(report)
    report.flag("test", "A retained report owns its validation messages")
    assert retained_size(report) >= empty_size + retained_size(report.messages[0])
