from __future__ import annotations

import json

import pytest

from rcp.agents import (
    agent_output_schema,
    normalize_agent_patch_bookkeeping,
    validate_agent_patch_shape,
)
from rcp.core.models import Patch, ValidationMessage
from tests.helpers import seed_patch


def test_agent_patch_schema_accepts_the_canonical_seed_shape() -> None:
    validate_agent_patch_shape(seed_patch())


def test_agent_patch_schema_rejects_invented_node_fields_and_slug_formats() -> None:
    patch = Patch(
        kind="seed",
        author="agent",
        summary="Used an invented graph vocabulary.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "hyp-invented-shape",
                        "type": "hypothesis",
                        "title": "Invented shape",
                        "statement": "The schema should reject this before graph validation.",
                        "state": "supported",
                        "asserted": True,
                    }
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="graph operation schema") as caught:
        validate_agent_patch_shape(patch)

    assert "hyp-invented-shape" in str(caught.value) or "Extra inputs" in str(caught.value)


def test_agent_output_schema_describes_operations_instead_of_arbitrary_objects() -> None:
    schema = agent_output_schema()
    rendered = json.dumps(schema)

    assert '"create_nodes"' in rendered
    assert '"set_coverage"' in rendered
    assert schema["$defs"]["NewEdge"]["properties"]["relation"]["pattern"].startswith("^")
    assert '"additionalProperties": false' in rendered
    assert "source_id" not in rendered
    assert "admission" not in schema["properties"]
    assert "admission_messages" not in schema["properties"]
    assert "ValidationMessage" not in schema["$defs"]
    assert "layer" not in schema["$defs"]["NewEdge"]["properties"]
    for definition in ("NodeUpdate", "SupersedeNode", "NodeMerge"):
        assert "cause" in schema["$defs"][definition]["properties"]


def test_new_agent_evidence_requires_an_explicit_origin() -> None:
    evidence = {
        "id": "ev/observed-recovery",
        "type": "evidence",
        "title": "Observed recovery",
        "observation": "The held-out learning curve recovered after replanning.",
    }
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Recorded evidence.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[{"op": "create_nodes", "nodes": [evidence]}],
    )

    with pytest.raises(ValueError, match="origin"):
        validate_agent_patch_shape(patch)

    evidence["origin"] = "internal_run"
    validate_agent_patch_shape(
        patch.model_copy(update={"ops": [{"op": "create_nodes", "nodes": [evidence]}]})
    )


@pytest.mark.parametrize(
    "cause",
    [
        {"kind": "evidence_edge", "ref_id": "ev/result::supports::hyp/claim"},
        {"kind": "decision", "ref_id": "dec/evaluation-rule"},
        {"kind": "proposal_resolution", "ref_id": "prop/revise-claim"},
    ],
)
def test_agent_belief_causes_have_strict_supported_shapes(cause: dict[str, str]) -> None:
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Changed a belief with a structured cause.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "changes": {"status": "supported"},
                        "cause": cause,
                    }
                ],
            }
        ],
    )

    validate_agent_patch_shape(patch)


@pytest.mark.parametrize(
    "cause",
    [
        {"kind": "evidence_edge"},
        {"kind": "decision", "ref_id": "dec/evaluation-rule", "note": "extra"},
        {"kind": "proposal_resolution", "ref_id": 7},
        {"kind": "human_edit"},
        {"kind": "human_edit", "ref_id": "human"},
        {"kind": "unknown"},
    ],
)
def test_agent_belief_causes_reject_missing_extra_or_unknown_fields(
    cause: dict[str, object],
) -> None:
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Used a malformed belief cause.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "supersede_nodes",
                "nodes": [
                    {
                        "id": "hyp/replanning-restores-plasticity",
                        "cause": cause,
                    }
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="graph operation schema"):
        validate_agent_patch_shape(patch)


def test_agent_edge_layer_is_backend_owned() -> None:
    patch = seed_patch()
    data = patch.model_dump(mode="python")
    data["ops"][1]["edges"][0]["layer"] = "action"

    with pytest.raises(ValueError, match="layer"):
        validate_agent_patch_shape(Patch.model_validate(data))


def test_agent_schema_accepts_the_generic_extension_namespace() -> None:
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Recorded an active project-specific construct.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "mechanism_claim/optimizer-memory",
                        "type": "hypothesis",
                        "extension_type": "mechanism_claim",
                        "extension_fields": {
                            "mechanism_family": "optimizer state",
                            "directly_testable": True,
                            "alternative_explanations": ["data order", "parameter drift"],
                        },
                        "title": "Optimizer state carries task history",
                        "statement": "Optimizer state retains information about earlier tasks.",
                    }
                ],
            }
        ],
    )

    validate_agent_patch_shape(patch)


def test_agent_extension_fields_cannot_escape_the_namespace() -> None:
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Put a custom field at the node top level.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "mechanism_claim/optimizer-memory",
                        "type": "hypothesis",
                        "extension_type": "mechanism_claim",
                        "extension_fields": {},
                        "mechanism_family": "optimizer state",
                        "title": "Optimizer state carries task history",
                        "statement": "Optimizer state retains information about earlier tasks.",
                    }
                ],
            }
        ],
    )

    with pytest.raises(ValueError, match="mechanism_family|Extra inputs"):
        validate_agent_patch_shape(patch)


def test_agent_schema_accepts_custom_relation_names_without_a_layer() -> None:
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Connected two nodes with an active custom relation.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "mechanism_claim/optimizer-memory",
                        "target": "hyp/plasticity-loss",
                        "relation": "mechanistically_explains",
                    }
                ],
            }
        ],
    )

    validate_agent_patch_shape(patch)


def _ontology_proposal() -> dict[str, object]:
    return {
        "id": "prop/add-mechanism-claim",
        "title": "Add mechanism claims",
        "card": {
            "situation_cold": "The graph needs to distinguish causal mechanisms from predictions.",
            "why_human_now": "Only a human may activate project ontology changes.",
            "consequences": "Future agents may author mechanism claims and their fields.",
            "decision_needed": "Approve or reject the proposed ontology.",
        },
        "ops": [
            {
                "op": "set_ontology",
                "ontology": {
                    "types": [
                        {
                            "name": "mechanism_claim",
                            "definition": "A causal account of an observed research result.",
                            "base_type": "hypothesis",
                            "layer": "epistemic",
                        }
                    ],
                    "fields": [
                        {
                            "owner_type": "mechanism_claim",
                            "name": "mechanism_family",
                            "definition": "The family of mechanisms under study.",
                            "kind": "text",
                            "required": True,
                            "agent_writable": True,
                        }
                    ],
                    "relations": [
                        {
                            "name": "mechanistically_explains",
                            "definition": "Connects a mechanism claim to what it explains.",
                            "source_types": ["mechanism_claim"],
                            "target_types": ["hypothesis"],
                            "layer": "epistemic",
                        }
                    ],
                },
            }
        ],
        "related_node_ids": [],
        "related_config_keys": ["ontology"],
        "base_rev": 3,
    }


def test_agent_cannot_apply_ontology_directly() -> None:
    proposal = _ontology_proposal()
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Tried to activate an ontology directly.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=proposal["ops"],
    )

    with pytest.raises(ValueError, match="set_ontology|graph operation schema"):
        validate_agent_patch_shape(patch)


def test_agent_can_propose_a_complete_ontology_state() -> None:
    patch = Patch(
        kind="refresh",
        author="agent",
        summary="Proposed a project ontology extension for human review.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=[{"op": "create_proposals", "proposals": [_ontology_proposal()]}],
    )

    validate_agent_patch_shape(patch)

    rendered = json.dumps(agent_output_schema())
    assert '"set_ontology"' in rendered
    assert '"OntologyState"' in rendered


def test_agent_revision_bookkeeping_is_normalized_before_shape_validation() -> None:
    patch = seed_patch().model_copy(
        update={
            "revision": 7,
            "admission": "rejected",
            "admission_messages": [
                ValidationMessage(
                    level="reject",
                    code="forged",
                    message="The provider does not own admission.",
                )
            ],
        }
    )
    data = patch.model_dump(mode="python")
    data["ops"][0]["nodes"][0]["created_rev"] = 7
    data["ops"][0]["nodes"][0]["updated_rev"] = 7

    normalized = normalize_agent_patch_bookkeeping(Patch.model_validate(data))

    assert normalized.revision == 0
    assert normalized.admission == "accepted"
    assert normalized.admission_messages == []
    assert normalized.ops[0]["nodes"][0]["created_rev"] == 0
    assert normalized.ops[0]["nodes"][0]["updated_rev"] == 0
    validate_agent_patch_shape(normalized)


def test_work_is_an_agent_patch_kind() -> None:
    patch = seed_patch().model_copy(update={"kind": "work"})

    validate_agent_patch_shape(patch)
