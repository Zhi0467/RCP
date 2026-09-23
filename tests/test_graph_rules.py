from __future__ import annotations

from typing import get_args

from rcp.agents.graph_rules import (
    _EDIT_METHOD,
    _EXTENSION_RULES,
    GRAPH_RULES_VERSION,
    graph_rules,
)
from rcp.agents.schema import agent_output_schema
from rcp.core.models import (
    RELATION_SPEC,
    Edge,
    EvidenceAssessment,
    ExperimentProxy,
    ProjectNode,
)

NODE_MODELS = get_args(get_args(ProjectNode)[0])


def test_every_graph_field_and_relation_carries_its_meaning_in_code() -> None:
    models = (*NODE_MODELS, Edge, EvidenceAssessment, ExperimentProxy)
    undescribed = [
        f"{model.__name__}.{name}"
        for model in models
        for name, field in model.model_fields.items()
        if name != "type" and not field.description
    ]
    assert not undescribed
    assert all(spec.description for spec in RELATION_SPEC.values())

    definitions = agent_output_schema()["$defs"]
    agent_shapes = ("AgentExperiment", "AgentEvidence", "NewEdge", "AgentExperimentProxy")
    assert all(
        "description" in schema
        for name in agent_shapes
        for field, schema in definitions[name]["properties"].items()
        if field != "type"
    )


def test_rendered_rules_name_every_field_and_relation() -> None:
    rendered = graph_rules(edits=True, ontology_extensions=True)

    for model in NODE_MODELS:
        assert f"(`type: {get_args(model.model_fields['type'].annotation)[0]}`" in rendered
        for name in model.model_fields.keys() - {"type"}:
            assert f"- `{name}`" in rendered, f"{model.__name__}.{name}"
    for name in RELATION_SPEC:
        assert f"- `{name}`:" in rendered
    # Layers are derived per endpoint pair, so one relation can span two layers.
    assert "ResearchQuestion -> Blocker (seam)" in rendered


def test_read_only_rules_omit_authoring_method() -> None:
    read = graph_rules(edits=False, ontology_extensions=True)
    edit = graph_rules(edits=True, ontology_extensions=True)

    assert GRAPH_RULES_VERSION in read and GRAPH_RULES_VERSION in edit
    assert _EDIT_METHOD not in read and _EXTENSION_RULES not in read
    assert _EDIT_METHOD in edit and _EXTENSION_RULES in edit
    assert _EXTENSION_RULES not in graph_rules(edits=True, ontology_extensions=False)
