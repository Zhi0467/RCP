from __future__ import annotations

import json
import uuid

import pytest

from rcp.agents.schema import parse_agent_patch_json, prepare_agent_patch
from rcp.core.authority import (
    AgentDispatchAuthority,
    AgentDispatchScope,
    AgentTaskAuthority,
    require_apply,
)
from rcp.core.materialize import apply_valid_patch, prepare_patch_bookkeeping
from rcp.core.models import Experiment, GraphBranchMetadata, GraphState, Patch
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.core.validation.patch import validate_patch
from rcp.history import HistoryManager
from tests.helpers import fabricated_authorizer, seated_on_every_project


def _definition_patch(definition, *, kind="work", profile="ordinary", term="EWC"):
    draft = parse_agent_patch_json(
        json.dumps(
            {
                "summary": "Explained the project terminology.",
                "ops": [
                    {
                        "op": "upsert_glossary",
                        "terms": [{"term": term, "plain_definition": definition}],
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
            "A penalty for changing parameters important to earlier tasks.",
            profile=profile,
            term="ewc",
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
    assert updated.ops[0].terms[0].term == "EWC"
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


def test_branch_glossary_upsert_is_isolated_and_replays_its_own_state(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(_definition_patch("Main definition."))
    before = {
        path.relative_to(history.root): path.read_bytes() for path in history.root.rglob("*.json")
    }
    base = history.head_ref()
    branch_id = str(uuid.uuid4())
    target = GraphTargetRef(kind="branch", branch_id=branch_id)
    branch = history.create_auto_research_branch(
        GraphBranchMetadata(
            branch_id=branch_id,
            episode_id=branch_id,
            project_id="project",
            base_head=base,
            head=GraphHeadRef(
                target=target, revision=base.revision, transition_id=base.transition_id
            ),
            authorized_by=fabricated_authorizer("Researcher"),
        )
    )
    _, result = branch.append(_definition_patch("Branch definition.", term="ewc"))
    assert result.state.glossary["EWC"].plain_definition == "Branch definition."
    assert branch.materialize().state.glossary == result.state.glossary
    assert history.state().glossary["EWC"].plain_definition == "Main definition."
    assert history.head_ref() == base
    for relative, contents in before.items():
        assert (history.root / relative).read_bytes() == contents


def test_failed_apply_after_glossary_upsert_preserves_shared_previous_term() -> None:
    state = GraphState(
        glossary={"EWC": {"term": "EWC", "plain_definition": "Original definition."}}
    )
    term = state.glossary["EWC"]
    patch = Patch(
        kind="work",
        author="agent",
        summary="Attempt to revise a definition and a missing node.",
        ops=[
            {
                "op": "upsert_glossary",
                "terms": [{"term": "EWC", "plain_definition": "Candidate definition."}],
            },
            {"op": "update_nodes", "nodes": [{"id": "hyp/missing", "changes": {"title": "New"}}]},
        ],
    )
    with pytest.raises(KeyError):
        apply_valid_patch(state, patch)
    assert state.glossary["EWC"] is term
    assert term.plain_definition == "Original definition."


def test_same_patch_cased_glossary_upserts_share_first_spelling(manifest) -> None:
    patch = _definition_patch("First definition.", term="Ewc")
    patch.ops[0].terms.append(
        patch.ops[0].terms[0].model_copy(update={"term": "EWC", "plain_definition": "Second."})
    )
    patch.ops.extend(_definition_patch("Last definition.", term="ewc").ops)
    history = HistoryManager(manifest)
    recorded, result = history.append(patch)
    assert list(result.state.glossary) == ["Ewc"]
    assert result.state.glossary["Ewc"].plain_definition == "Last definition."
    assert all(term.term == "Ewc" for op in recorded.ops for term in op.terms)
    assert history.materialize().state.glossary == result.state.glossary


def test_legacy_cased_glossary_replay_is_unchanged_but_revision_updates_visible_term() -> None:
    state = GraphState()
    for revision, spelling in enumerate(("EWC", "ewc"), 1):
        historical = _definition_patch(f"Old {spelling}.", term=spelling).model_copy(
            update={"revision": revision}
        )
        state = apply_valid_patch(state, historical)
    assert list(state.glossary) == ["EWC", "ewc"]
    patch = _definition_patch("Current definition.", term="eWc").model_copy(update={"revision": 3})
    prepared = prepare_patch_bookkeeping(state, patch)
    updated = apply_valid_patch(state, prepared)
    assert prepared.ops[0].terms[0].term == "EWC"
    assert updated.glossary["EWC"].plain_definition == "Current definition."
    assert updated.glossary["ewc"].plain_definition == "Old ewc."
    assert state.glossary["EWC"].plain_definition == "Old EWC."
