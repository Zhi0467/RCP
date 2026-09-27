from __future__ import annotations

import pytest

from rcp.agents.continuation_prompt import (
    SECTIONS,
    LaunchPhase,
    MasterRef,
    classify,
    compose,
)
from rcp.agents.graph_rules import GRAPH_RULES_VERSION
from rcp.agents.prompts import CHAT_MASTER_CONTEXT_VERSION, PromptFactory, chat_master_contract_key


@pytest.mark.parametrize(
    ("phase", "continued"),
    [
        ("turn", "human_turn"),
        ("wake", "wake"),
        ("recovery", "recovery"),
        ("correction", "correction"),
    ],
)
def test_the_session_id_decides_start_versus_continuation(phase, continued) -> None:
    assert classify(LaunchPhase(session_id=None, phase=phase)) == "session_start"
    assert classify(LaunchPhase(session_id="native", phase=phase)) == continued


def test_chat_master_key_keeps_the_key_existing_sessions_hold() -> None:
    assert chat_master_contract_key() == (
        f"chat-master-v{CHAT_MASTER_CONTEXT_VERSION}-rules-{GRAPH_RULES_VERSION}"
    )


@pytest.mark.parametrize(
    ("node", "master", "section"),
    [
        ("session_start", MasterRef(path="/m.md", bootstrap=True), "master_bootstrap"),
        ("human_turn", MasterRef(path="/m.md", bootstrap=False), "master_pointer"),
        ("human_turn", MasterRef(path="/m.md", bootstrap=True), "master_bootstrap"),
        ("wake", MasterRef(path="/m.md", bootstrap=True, replaces=True), "master_rebootstrap"),
    ],
)
def test_compose_places_the_master_by_node_type(node, master, section) -> None:
    delta = None if node == "session_start" else {"settings": {"reasoning": "high"}}
    sections = compose(node, parts=["owner one", "owner two"], master=master, delta=delta).split(
        "\n\n"
    )

    expected = SECTIONS[section].format(path=master.path)
    if node == "session_start":
        assert sections == [expected, "owner one", "owner two"]
    else:
        assert sections[:2] == ["owner one", "owner two"]
        assert sections[2].startswith(SECTIONS["context_delta"])
        assert sections[3:] == [expected]


def test_session_start_refuses_a_pointer_to_a_master_it_never_had() -> None:
    with pytest.raises(ValueError):
        compose("session_start", parts=["x"], master=MasterRef("/m.md", False), delta=None)


def test_a_continuation_carries_no_master_text() -> None:
    master = PromptFactory.chat_master_context(
        project_name="Example",
        ontology_path="/state/graph.json#ontology",
        ontology_extensions=False,
        graph_path="/state/graph.json",
        research_path="/state/research.md",
        graph_revision=3,
        focused_node_id=None,
        repositories=[],
        introduction_path=None,
        patch_path="/stage/workspace/patch.json",
        workspace_path="/stage/workspace",
        output_schema_path="/stage/inputs/schema.json",
        validator_command="python3 /stage/inputs/validate.py",
    )
    assert GRAPH_RULES_VERSION in master

    prompt = PromptFactory.work_turn_prompt(
        artifact_path="/stage/workspace/turns/op/artifacts",
        human_message="Continue the analysis.",
        node="human_turn",
        master=MasterRef(path="/stage/inputs/chat-master.md", bootstrap=False),
        context_delta={"current": {"graph_revision": 4}},
    )

    assert GRAPH_RULES_VERSION not in prompt
    assert not any(block in prompt for block in master.split("\n\n") if len(block) > 80)
