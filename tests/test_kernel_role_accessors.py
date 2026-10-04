"""Research-layer accessors the kernel calls after asking a node-type role."""

from __future__ import annotations

import pytest

from rcp.control import experiment_node, released_attempts
from rcp.core.models import Decision, Experiment, ExperimentAttempt, GraphState, Hypothesis
from rcp.core.validation.approval import checked_human_decision_choice, is_decision_choice
from rcp.storage.episodes import _ops_pause_for_human


def _decision(**updates: object) -> Decision:
    fields: dict[str, object] = {
        "id": "dec/shape",
        "type": "decision",
        "title": "Shape",
        "question": "Which shape?",
        "options": ["wide", "deep"],
    }
    fields.update(updates)
    return Decision.model_validate(fields)


def _experiment(*statuses: str) -> Experiment:
    return Experiment(
        id="exp/run",
        type="experiment",
        title="Run",
        objective="Test it.",
        attempts=[
            ExperimentAttempt(id=f"a{index}", sequence=index, purpose="Try.", status=status)
            for index, status in enumerate(statuses, start=1)
        ],
    )


def _hypothesis() -> Hypothesis:
    return Hypothesis(id="hyp/claim", type="hypothesis", title="Claim", statement="It holds.")


def test_decision_choice_is_a_selection_or_a_decided_status() -> None:
    assert is_decision_choice({"selected_option": "wide"})
    assert is_decision_choice({"status": "decided"})
    assert not is_decision_choice({"status": "ready", "title": "New"})


def test_human_decision_choice_returns_the_effective_option() -> None:
    assert (
        checked_human_decision_choice(_decision(), {"selected_option": "deep", "status": "decided"})
        == "deep"
    )
    # Repeating the carried option moves only the status.
    carried = _decision(selected_option="wide")
    assert checked_human_decision_choice(carried, {"status": "decided"}) == "wide"
    assert checked_human_decision_choice(_decision(), {"status": "revisit"}) is None
    assert checked_human_decision_choice(_hypothesis(), {"status": "decided"}) is None


@pytest.mark.parametrize(
    ("node", "changes", "message"),
    [
        (
            _decision(),
            {"status": "closed"},
            "Direct edits to dec/shape may queue it as open, ready, or revisit; "
            "only the Decision choice control may decide it.",
        ),
        (
            _decision(status="superseded"),
            {"status": "decided", "selected_option": "wide"},
            "Decision dec/shape is superseded and cannot be decided again.",
        ),
        (
            _decision(),
            {"selected_option": "wide"},
            "Direct choice on dec/shape must set status exactly to decided.",
        ),
        (
            _decision(),
            {"selected_option": "tall", "status": "decided"},
            "Direct choice on dec/shape must select one non-empty option from its current options.",
        ),
    ],
)
def test_human_decision_choice_refusals(
    node: Decision, changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError) as refused:
        checked_human_decision_choice(node, changes)
    assert str(refused.value) == message


def test_experiment_node_returns_only_experiments() -> None:
    experiment = _experiment()
    state = GraphState(nodes={experiment.id: experiment, "hyp/claim": _hypothesis()})
    assert experiment_node(state, experiment.id) is state.nodes[experiment.id]
    assert experiment_node(state, "hyp/claim") is None
    assert experiment_node(state, "missing") is None
    assert experiment_node(state, None) is None


def test_released_attempts_close_only_named_open_attempts() -> None:
    released = released_attempts(_experiment("running", "completed", "planned"), ["a1"])
    assert [attempt["status"] for attempt in released] == ["cancelled", "completed", "planned"]
    assert released[0]["failure_reason"] == "Released by the human."
    assert released[0]["finished_at"] is not None
    with pytest.raises(ValueError, match=r"^exp/run has no open attempt named: a2\.$"):
        released_attempts(_experiment("running", "completed"), ["a2"])
    with pytest.raises(ValueError, match=r"^hyp/claim has no attempts to release\.$"):
        released_attempts(_hypothesis(), ["a1"])


def test_legacy_pause_detects_a_new_blocker_gating_the_control_node() -> None:
    def operations(node_type: object, relation: object) -> list[object]:
        return [
            {"op": "create_nodes", "nodes": [{"id": "blk/1", "type": node_type}]},
            {
                "op": "create_edges",
                "edges": [{"source": "exp/run", "relation": relation, "target": "blk/1"}],
            },
        ]

    assert _ops_pause_for_human(operations("blocker", "blocked_by"), "exp/run")
    assert not _ops_pause_for_human(operations("evidence", "blocked_by"), "exp/run")
    assert not _ops_pause_for_human(operations("blocker", "tests"), "exp/run")
    # Malformed legacy records never raise.
    assert not _ops_pause_for_human(operations(["blocker"], ["blocked_by"]), "exp/run")
