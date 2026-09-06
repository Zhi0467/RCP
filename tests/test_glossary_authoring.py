from __future__ import annotations

import json

import pytest

from rcp.agents.schema import parse_agent_patch_json, prepare_agent_patch
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    require_apply,
)
from rcp.core.models import Experiment, GraphState
from rcp.core.transition_models import GraphTargetRef
from rcp.core.validation.patch import validate_patch
from rcp.history import HistoryManager
from tests.helpers import fabricated_authorizer, seated_on_every_project


def _definition_patch(definition, *, kind="work", profile="ordinary"):
    draft = parse_agent_patch_json(
        json.dumps(
            {
                "summary": "Explained the project terminology.",
                "ops": [
                    {
                        "op": "upsert_glossary",
                        "terms": [{"term": "EWC", "plain_definition": definition}],
                    }
                ],
            }
        ),
        profile=profile,
    )
    return prepare_agent_patch(draft, kind=kind, run_truth_scope=["repo-a"])


@pytest.mark.parametrize(
    ("kind", "profile"),
    [("seed", "ordinary"), ("refresh", "ordinary"), ("work", "ordinary"), ("work", "orchestrator")],
)
def test_project_glossary_can_be_added_revised_and_replayed(manifest, kind, profile) -> None:
    history = HistoryManager(manifest)
    original, first = history.append(
        _definition_patch("Elastic weight consolidation.", kind=kind, profile=profile)
    )
    original_path = history.root / "patches" / f"{original.revision:06d}.json"
    original_bytes = original_path.read_bytes()
    updated, second = history.append(
        _definition_patch(
            "A penalty for changing parameters important to earlier tasks.", profile=profile
        )
    )

    assert not second.state.nodes
    assert list(second.state.glossary) == ["EWC"]
    assert first.state.glossary["EWC"].plain_definition == "Elastic weight consolidation."
    assert first.state.glossary["EWC"].updated_rev == original.revision
    assert second.state.glossary["EWC"].updated_rev == updated.revision
    assert (
        second.state.glossary["EWC"].plain_definition
        != first.state.glossary["EWC"].plain_definition
    )
    assert original_path.read_bytes() == original_bytes
    assert HistoryManager(manifest).materialize().state.glossary == second.state.glossary
    assert (
        json.loads((history.root / "glossary.json").read_text())["EWC"]["updated_rev"]
        == updated.revision
    )


def test_experiment_loop_can_explain_project_terms_without_changing_control() -> None:
    state = GraphState(
        project_truth_scope=["repo-a"],
        nodes={
            "exp/test": Experiment(
                id="exp/test", type="experiment", title="Test", objective="Measure retention."
            )
        },
    )
    patch = _definition_patch("Elastic weight consolidation.", kind="experiment_loop")
    report = validate_patch(state, patch, ["repo-a"], experiment_control_node_id="exp/test")
    assert not report.rejected, report.messages


def test_glossary_operation_does_not_grant_discuss_an_apply_channel() -> None:
    task = AgentTaskAuthority(
        operation_id="discuss-turn",
        project_id="project-one",
        apply_target=GraphTargetRef(),
        authorized_by=fabricated_authorizer("Researcher"),
        dispatch_authority=AgentDispatchAuthority(
            profile="ordinary",
            task_contract="discuss",
            scope=AgentDispatchScope(
                run_truth_scope=["repo-a"], chat_scope="project", chat_id="discussion"
            ),
        ),
    )
    with pytest.raises(ValueError, match="exposes no graph Patch channel"):
        require_apply(
            task,
            _definition_patch("Elastic weight consolidation."),
            is_project_member=seated_on_every_project,
        )
