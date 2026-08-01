from __future__ import annotations

from copy import deepcopy

from rcp.core.materialize import apply_valid_patch, materialize_patches
from rcp.core.models import GraphState, OntologyState, Patch
from rcp.core.validation import proposal_dependencies, validate_patch
from tests.helpers import refresh_patch, seed_patch


def _ontology() -> dict[str, object]:
    return {
        "types": [
            {
                "name": "training_run",
                "definition": "One concrete model training run.",
                "base_type": "experiment",
                "layer": "action",
            }
        ],
        "fields": [
            {
                "owner_type": "training_run",
                "name": "accelerators",
                "definition": "Number of accelerators used.",
                "kind": "number",
                "required": True,
            },
            {
                "owner_type": "hypothesis",
                "name": "review_note",
                "definition": "A note controlled by the human reviewer.",
                "kind": "text",
                "agent_writable": False,
            },
        ],
        "relations": [
            {
                "name": "evaluates",
                "definition": "The run evaluates the hypothesis.",
                "source_types": ["training_run"],
                "target_types": ["hypothesis"],
                "layer": "action",
            }
        ],
    }


def _approval(revision: int, ops: list[dict[str, object]]) -> Patch:
    return Patch(
        revision=revision,
        kind="approval",
        author="human",
        summary="Human ontology action.",
        ops=ops,
    )


def _agent(revision: int, ops: list[dict[str, object]]) -> Patch:
    return Patch(
        revision=revision,
        kind="refresh",
        author="agent",
        summary="Agent graph action.",
        run_truth_scope=["repo"],
        repositories_read=["repo"],
        ops=ops,
    )


def _set_ontology(revision: int, ontology: dict[str, object]) -> Patch:
    return _approval(revision, [{"op": "set_ontology", "ontology": ontology}])


def _custom_node() -> dict[str, object]:
    return {
        "id": "training_run/baseline",
        "type": "experiment",
        "extension_type": "training_run",
        "extension_fields": {"accelerators": 8},
        "title": "Baseline training run",
        "objective": "Measure adaptation after the shift.",
    }


def _hypothesis() -> dict[str, object]:
    return {
        "id": "hyp/adapts",
        "type": "hypothesis",
        "title": "The learner adapts",
        "statement": "The learner adapts after the task changes.",
    }


def _codes(report) -> set[str]:
    return {message.code for message in report.messages}


def test_old_project_opens_without_ontology_or_extension_keys() -> None:
    old_payload = {
        "revision": 1,
        "project_truth_scope": ["repo"],
        "nodes": {
            "hyp/legacy": {
                "id": "hyp/legacy",
                "type": "hypothesis",
                "title": "Legacy hypothesis",
                "statement": "Old projects remain readable.",
            }
        },
        "edges": {},
    }

    state = GraphState.model_validate(old_payload)

    assert state.ontology == OntologyState()
    assert state.nodes["hyp/legacy"].extension_type is None
    assert state.nodes["hyp/legacy"].extension_fields == {}


def test_legacy_patch_replay_preserves_every_recorded_node_and_edge_field() -> None:
    patches = [
        seed_patch().model_copy(update={"revision": 1}),
        refresh_patch().model_copy(update={"revision": 2}),
    ]
    before = materialize_patches(patches, ["repo-a", "repo-b"])
    recorded = before.state.model_dump(mode="json")
    for node in recorded["nodes"].values():
        node.pop("extension_type")
        node.pop("extension_fields")
    recorded.pop("ontology")

    opened = GraphState.model_validate(recorded)
    replayed = materialize_patches(patches, ["repo-a", "repo-b"])

    assert all(not report.rejected for report in replayed.reports.values())
    for node_id, old_node in recorded["nodes"].items():
        current = replayed.state.nodes[node_id].model_dump(mode="json")
        assert {key: current[key] for key in old_node} == old_node
    for edge_id, old_edge in recorded["edges"].items():
        current = replayed.state.edges[edge_id].model_dump(mode="json")
        assert {key: current[key] for key in old_edge} == old_edge
    assert opened.revision == replayed.state.revision


def test_validation_uses_the_ontology_in_force_before_each_patch() -> None:
    define = _set_ontology(1, _ontology())
    create = _agent(2, [{"op": "create_nodes", "nodes": [_custom_node()]}])

    before = validate_patch(GraphState(project_truth_scope=["repo"]), create, ["repo"])
    assert "unknown-extension-type" in _codes(before)

    defined = materialize_patches([define], ["repo"]).state
    after = validate_patch(defined, create, ["repo"])
    assert not after.rejected

    combined = _approval(
        1,
        [
            {"op": "set_ontology", "ontology": _ontology()},
            {"op": "create_nodes", "nodes": [_custom_node()]},
        ],
    )
    assert "unknown-extension-type" in _codes(
        validate_patch(GraphState(), combined, [], mode="admission")
    )


def test_removed_type_and_fields_remain_readable_during_replay() -> None:
    active = _ontology()
    deprecated = deepcopy(active)
    deprecated["types"][0]["deprecated"] = True
    deprecated["fields"][0]["deprecated"] = True
    deprecated["relations"][0]["deprecated"] = True
    removed = deepcopy(deprecated)
    removed["types"] = []
    removed["fields"] = [deprecated["fields"][1]]
    removed["relations"] = []
    patches = [
        _set_ontology(1, active),
        _agent(2, [{"op": "create_nodes", "nodes": [_custom_node()]}]),
        _set_ontology(3, deprecated),
        _set_ontology(4, removed),
    ]

    at_creation = materialize_patches(patches[:2], ["repo"])
    final = materialize_patches(patches, ["repo"])

    assert at_creation.state.replay_status == "complete"
    assert final.state.replay_status == "complete"
    assert final.state.nodes["training_run/baseline"].model_dump() == (
        at_creation.state.nodes["training_run/baseline"].model_dump()
    )
    assert final.state.ontology == OntologyState.model_validate(removed)


def test_removal_requires_prior_deprecation_but_replay_does_not_rejudge_safety() -> None:
    state = materialize_patches([_set_ontology(1, _ontology())], ["repo"]).state
    report = validate_patch(state, _set_ontology(2, {}), ["repo"])
    assert "ontology-removal-without-deprecation" in _codes(report)

    # Safety is an admission verdict stored with the patch, not a new replay rule.
    replay = validate_patch(state, _set_ontology(2, {}), ["repo"], mode="replay")
    assert "ontology-removal-without-deprecation" not in _codes(replay)


def test_base_ontology_cannot_be_redefined() -> None:
    colliding = _ontology()
    colliding["types"][0]["name"] = "experiment"
    colliding["fields"][0]["name"] = "title"
    colliding["relations"][0]["name"] = "tests"
    report = validate_patch(GraphState(), _set_ontology(1, colliding), [])
    assert "base-ontology-collision" in _codes(report)


def test_new_nodes_validate_required_fields_kinds_and_custom_prefix() -> None:
    state = materialize_patches([_set_ontology(1, _ontology())], ["repo"]).state
    missing = _custom_node()
    missing["extension_fields"] = {}
    wrong_kind = _custom_node()
    wrong_kind["extension_fields"] = {"accelerators": True}
    wrong_prefix = _custom_node()
    wrong_prefix["id"] = "exp/baseline"

    assert "missing-required-extension-field" in _codes(
        validate_patch(state, _agent(2, [{"op": "create_nodes", "nodes": [missing]}]), ["repo"])
    )
    assert "invalid-extension-field-kind" in _codes(
        validate_patch(
            state, _agent(2, [{"op": "create_nodes", "nodes": [wrong_kind]}]), ["repo"]
        )
    )
    assert "wrong-slug-prefix" in _codes(
        validate_patch(
            state, _agent(2, [{"op": "create_nodes", "nodes": [wrong_prefix]}]), ["repo"]
        )
    )


def test_agent_writable_is_admission_only_and_human_may_write_project_field() -> None:
    state = materialize_patches([_set_ontology(1, _ontology())], ["repo"]).state
    raw = _hypothesis() | {"extension_fields": {"review_note": "Human checked."}}
    agent_patch = _agent(2, [{"op": "create_nodes", "nodes": [raw]}])
    assert "extension-field-human-only" in _codes(validate_patch(state, agent_patch, ["repo"]))
    assert "extension-field-human-only" not in _codes(
        validate_patch(state, agent_patch, ["repo"], mode="replay")
    )

    state = materialize_patches(
        [_set_ontology(1, _ontology()), _agent(2, [{"op": "create_nodes", "nodes": [_hypothesis()]}])],
        ["repo"],
    ).state
    human_patch = _approval(
        3,
        [
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "hyp/adapts",
                        "base_updated_rev": 2,
                        "changes": {"extension_fields": {"review_note": "Human checked."}},
                    }
                ],
            }
        ],
    )
    assert not validate_patch(state, human_patch, ["repo"]).rejected


def test_standalone_human_new_node_is_exactly_one_asserted_custom_node() -> None:
    state = materialize_patches([_set_ontology(1, _ontology())], ["repo"]).state
    valid = _approval(2, [{"op": "create_nodes", "nodes": [_custom_node()]}])
    assert not validate_patch(state, valid, ["repo"]).rejected

    base_only = _approval(2, [{"op": "create_nodes", "nodes": [_hypothesis()]}])
    assert "invalid-direct-node-create" in _codes(validate_patch(state, base_only, ["repo"]))

    sourced = _custom_node() | {
        "source_refs": [
            {
                "machine": "local",
                "truth_repository": "repo",
                "source": "codex",
                "session_id": "session",
                "record_uuid": "record",
                "timestamp": "2026-07-30T00:00:00Z",
                "excerpt": "A human-created node may not claim this source.",
            }
        ]
    }
    report = validate_patch(
        state,
        _approval(2, [{"op": "create_nodes", "nodes": [sourced]}]),
        ["repo"],
    )
    assert "invalid-direct-node-create" in _codes(report)


def test_custom_relation_uses_semantic_types_and_materializes_declared_layer() -> None:
    state = materialize_patches([_set_ontology(1, _ontology())], ["repo"]).state
    create = _agent(
        2,
        [
            {"op": "create_nodes", "nodes": [_custom_node(), _hypothesis()]},
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "training_run/baseline",
                        "target": "hyp/adapts",
                        "relation": "evaluates",
                    }
                ],
            },
        ],
    )
    assert not validate_patch(state, create, ["repo"]).rejected
    applied = materialize_patches([_set_ontology(1, _ontology()), create], ["repo"])
    edge = applied.state.edges["training_run/baseline::evaluates::hyp/adapts"]
    assert edge.layer == "action"

    reversed_edge = _agent(
        2,
        [
            {"op": "create_nodes", "nodes": [_custom_node(), _hypothesis()]},
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "hyp/adapts",
                        "target": "training_run/baseline",
                        "relation": "evaluates",
                    }
                ],
            },
        ],
    )
    assert "custom-relation-type-mismatch" in _codes(
        validate_patch(state, reversed_edge, ["repo"])
    )


def test_relation_narrowing_names_the_edges_and_nodes_that_block_it() -> None:
    patches = [
        _set_ontology(1, _ontology()),
        _agent(
            2,
            [
                {"op": "create_nodes", "nodes": [_custom_node(), _hypothesis()]},
                {
                    "op": "create_edges",
                    "edges": [
                        {
                            "source": "training_run/baseline",
                            "target": "hyp/adapts",
                            "relation": "evaluates",
                        }
                    ],
                },
            ],
        ),
    ]
    state = materialize_patches(patches, ["repo"]).state
    narrowed = _ontology()
    narrowed["relations"][0]["source_types"] = ["experiment"]
    report = validate_patch(state, _set_ontology(3, narrowed), ["repo"])
    message = next(
        item for item in report.messages if item.code == "relation-narrowing-breaks-existing-edges"
    )
    assert message.related_node_ids == ["hyp/adapts", "training_run/baseline"]
    assert message.related_edge_ids == ["training_run/baseline::evaluates::hyp/adapts"]
    assert all(node_id in message.message for node_id in message.related_node_ids)


def test_required_field_cannot_strand_existing_nodes() -> None:
    optional = _ontology()
    optional["fields"][0]["required"] = False
    patches = [
        _set_ontology(1, optional),
        _agent(
            2,
            [
                {
                    "op": "create_nodes",
                    "nodes": [_custom_node() | {"extension_fields": {}}],
                }
            ],
        ),
    ]
    state = materialize_patches(patches, ["repo"]).state
    report = validate_patch(state, _set_ontology(3, _ontology()), ["repo"])
    assert "required-field-breaks-existing-nodes" in _codes(report)
    assert report.messages[-1].related_node_ids == ["training_run/baseline"]


def test_new_required_field_cannot_strand_existing_nodes() -> None:
    state = materialize_patches(
        [_agent(1, [{"op": "create_nodes", "nodes": [_hypothesis()]}])],
        ["repo"],
    ).state
    desired = _ontology()
    desired["fields"][1]["required"] = True

    report = validate_patch(state, _set_ontology(2, desired), ["repo"])

    message = next(
        item for item in report.messages if item.code == "required-field-breaks-existing-nodes"
    )
    assert message.related_node_ids == ["hyp/adapts"]


def test_ontology_proposal_depends_on_config_and_has_no_effect_before_approval() -> None:
    ontology_op = {"op": "set_ontology", "ontology": _ontology()}
    assert proposal_dependencies(GraphState(), [ontology_op]) == ([], ["ontology"])
    proposal = {
        "id": "prop/add-training-run",
        "title": "Add TrainingRun",
        "card": {
            "situation_cold": "Runs need domain-specific fields.",
            "why_human_now": "Ontology changes are human authority.",
            "consequences": "New runs can use the extension.",
            "decision_needed": "Approve the ontology extension.",
        },
        "ops": [ontology_op],
        "related_config_keys": ["ontology"],
        "base_rev": 0,
    }
    patch = _agent(1, [{"op": "create_proposals", "proposals": [proposal]}])
    report = validate_patch(GraphState(project_truth_scope=["repo"]), patch, ["repo"])
    assert not report.rejected

    state = apply_valid_patch(GraphState(project_truth_scope=["repo"]), patch)
    assert state.ontology == OntologyState()
    assert state.proposals["prop/add-training-run"].related_config_keys == ["ontology"]


def test_set_ontology_rejects_unknown_operation_keys() -> None:
    op = {"op": "set_ontology", "ontology": _ontology(), "base_types": "replace"}
    report = validate_patch(GraphState(), _approval(1, [op]), [])
    assert "invalid-ontology-operation" in _codes(report)
