"""Rule prose names node types through the project type.

Authority prose and rule messages describe enforcement, so they render type
names from ``project_type_of()`` instead of spelling them out. Rendering them
for a project type with other names shows each sentence follows the type.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from rcp.agents.auto_research_prompt import (
    _decision_disposition,
    auto_research_orchestrator_task_contract,
    orchestrator_graph_authority_contract,
)
from rcp.agents.branch_merge_prompt import branch_merge_task_contract
from rcp.agents.prompts import PromptFactory, ask_contract
from rcp.core import project_types
from rcp.core.authority import (
    _AGENT_GRAPH_AUTHORITY_BODY,
    AGENT_GRAPH_AUTHORITY_POLICY_DIGEST,
    _render_agent_graph_authority_body,
)
from rcp.core.materialize import apply_valid_patch
from rcp.core.models import GraphState
from rcp.core.operations import ProposalContentChangeOperation
from rcp.core.research_type import RESEARCH
from rcp.core.validation import proposals
from rcp.runs.branch_merge import MERGE_RESIDUE_REASONS
from tests.helpers import seed_patch
from tests.test_prompts import _work_write_scope

RENAMED = replace(
    RESEARCH,
    labels={
        **RESEARCH.labels,
        "research_question": "Goal",
        "hypothesis": "Design",
        "decision": "Choice",
        "experiment": "Change",
    },
    plural_labels={
        **RESEARCH.plural_labels,
        "research_question": "Goals",
        "hypothesis": "Designs",
        "decision": "Choices",
        "experiment": "Changes",
    },
)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def renamed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_types, "_research", lambda: RENAMED)


def test_authority_body_and_digest_render_from_the_project_type() -> None:
    assert _render_agent_graph_authority_body(RESEARCH) == _AGENT_GRAPH_AUTHORITY_BODY
    assert (
        (sha256(_AGENT_GRAPH_AUTHORITY_BODY.encode("utf-8")).hexdigest()[:16])
        == AGENT_GRAPH_AUTHORITY_POLICY_DIGEST
    )
    body = _flat(_render_agent_graph_authority_body(RENAMED))
    assert "involving an existing Goal or Design waits for a human" in body
    assert "ResearchQuestion" not in body
    assert "Hypothesis" not in body


def test_contract_sentences_follow_the_project_type(renamed: None) -> None:
    orchestrator_authority = _flat(orchestrator_graph_authority_contract())
    assert "Create new Goals and Designs directly" in orchestrator_authority
    assert "existing Goal or Design must instead be one pending Proposal" in (
        orchestrator_authority
    )
    assert "ResearchQuestion" not in orchestrator_authority
    assert "Directly create and change Evidence, Choices, Changes, and Blockers" in (
        orchestrator_authority
    )
    assert "Decision" not in orchestrator_authority
    disposition = _flat(_decision_disposition())
    assert disposition.startswith("Choice disposition:")
    assert "Decision" not in disposition

    assert "A change to an existing Goal or Design is still a Proposal." in _flat(
        ask_contract("<how>")
    )

    graph = PromptFactory.graph_task_contract(
        "refresh",
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=False,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        provider_log_roots={},
        ingestion_watermark=None,
        repositories=[],
        patch_path="/stage/patch.json",
        output_schema_path="/stage/schema.json",
        validator_command="validate",
    )
    assert "Existing goal changes require a Proposal" in _flat(graph)

    orchestrator = auto_research_orchestrator_task_contract(
        project_name="Example",
        graph_path="/stage/graph.json",
        research_path="/stage/research.md",
        repositories=[],
        patch_path="/stage/patch.json",
        output_schema_path="/stage/schema.json",
        command_client="test-command-client",
        write_scope=_work_write_scope(),
    )
    assert "without changing an existing Goal or Design" in _flat(orchestrator)

    merge = _flat(
        branch_merge_task_contract(
            context_path="/stage/context.json",
            context_id="a" * 64,
            patch_path="/stage/patch.json",
            validator_command="validate",
            review_contract_json="{}",
            plan_path="/stage/plan.json",
            residue_block="",
        )
    )
    assert "for that same Goal or Design" in merge
    assert "actually chooses a Choice" in merge


def test_merge_residue_reasons_name_the_project_types() -> None:
    assert (
        RESEARCH.label_list(RESEARCH.protected_belief_types)
        in (MERGE_RESIDUE_REASONS["protected_node"])
    )
    assert (
        RESEARCH.label_list(RESEARCH.chooser_types) in (MERGE_RESIDUE_REASONS["decision_outcome"])
    )


def test_proposal_intent_messages_follow_the_project_type(renamed: None) -> None:
    seeded = apply_valid_patch(GraphState(), seed_patch().model_copy(update={"revision": 1}))
    content = ProposalContentChangeOperation(
        op="update_nodes",
        intent="content_change",
        nodes=[{"id": "missing/node", "changes": {"title": "x"}}],
    )
    assert proposals._validate_content_change_intent(seeded, None, content) == (
        "a content change must target one existing Goal or Design."
    )
