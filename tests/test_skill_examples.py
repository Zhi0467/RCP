from __future__ import annotations

import json
import re

import pytest

from rcp.agents.schema import parse_agent_patch_json, prepare_agent_patch
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import GraphState
from rcp.core.validation import validate_patch
from rcp.skill_registry import official_registry

from .helpers import fabricated_authorizer


def _causality_examples() -> list[str]:
    registry = official_registry()
    package = registry.package("skill", "experiment-causality")
    path = registry.package_path(package.reference()) / "references" / "worked-examples.md"
    examples = re.findall(r"```json\n(.*?)\n```", path.read_text(encoding="utf-8"), re.DOTALL)
    assert len(examples) == 3
    return examples


def test_causality_examples_plan_then_observe_without_choosing_the_gate() -> None:
    planning, results, _choice = _causality_examples()
    state = GraphState(project_truth_scope=["repo"])

    for example in (planning, results):
        patch = prepare_agent_patch(
            parse_agent_patch_json(example), kind="work", run_truth_scope=["repo"]
        ).model_copy(update={"revision": state.revision + 1})
        report = validate_patch(state, patch, ["repo"])
        assert not report.rejected, [message.model_dump() for message in report.messages]
        state = apply_valid_patch(state, patch)

        assert state.nodes["dec/measurement-duration"].selected_option is None
        assert state.nodes["exp/compare-methods"].status == "proposed"
        if example == planning:
            assert not any(node.type == "evidence" for node in state.nodes.values())
            assert state.nodes["dec/measurement-duration"].status == "open"
            assert state.nodes["exp/calibrate-duration"].status == "proposed"

    assert state.nodes["exp/calibrate-duration"].status == "completed"
    assert state.nodes["dec/measurement-duration"].status == "ready"
    assert state.nodes["ev/duration-calibration"].origin == "internal_run"
    assert {(edge.source, edge.relation, edge.target) for edge in state.edges.values()} == {
        ("exp/compare-methods", "governed_by", "dec/measurement-duration"),
        ("exp/calibrate-duration", "produces", "ev/duration-calibration"),
        ("ev/duration-calibration", "informs", "dec/measurement-duration"),
    }
    assert all(edge.assessment is None for edge in state.edges.values())


def test_causality_example_choice_requires_orchestrator_authority() -> None:
    planning, results, choice = _causality_examples()
    state = GraphState(project_truth_scope=["repo"])
    for example in (planning, results):
        patch = prepare_agent_patch(
            parse_agent_patch_json(example), kind="work", run_truth_scope=["repo"]
        ).model_copy(update={"revision": state.revision + 1})
        assert not validate_patch(state, patch, ["repo"]).rejected
        state = apply_valid_patch(state, patch)

    with pytest.raises(ValueError, match="graph operation schema"):
        parse_agent_patch_json(choice)

    patch = prepare_agent_patch(
        parse_agent_patch_json(choice, profile="orchestrator"),
        kind="work",
        run_truth_scope=["repo"],
        profile="orchestrator",
    ).model_copy(
        update={
            "revision": state.revision + 1,
            "profile": "orchestrator",
            "authorized_by": fabricated_authorizer("Example episode owner"),
            "task_id": "example-orchestrator-turn",
        }
    )
    report = validate_patch(state, patch, ["repo"])
    assert not report.rejected, [message.model_dump() for message in report.messages]
    chosen = apply_valid_patch(state, patch)
    assert chosen.nodes["dec/measurement-duration"].selected_option == "10 seconds"
    assert chosen.nodes["dec/measurement-duration"].status == "decided"
    assert chosen.nodes["exp/compare-methods"].status == "proposed"

    # Omitting the orchestrator marker does not turn the same choice into ordinary Work.
    ordinary = json.loads(choice)
    ordinary.pop("agent_action")
    ordinary_patch = prepare_agent_patch(
        parse_agent_patch_json(json.dumps(ordinary)), kind="work", run_truth_scope=["repo"]
    )
    assert validate_patch(state, ordinary_patch, ["repo"]).rejected
