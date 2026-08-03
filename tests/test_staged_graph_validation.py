from __future__ import annotations

from rcp.core.materialize import apply_valid_patch
from rcp.core.models import GraphState, Patch
from rcp.core.validation import validate_patch


def _agent_patch(*operations: dict[str, object]) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Exercised staged graph validation.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=list(operations),
    )


def _validate(*operations: dict[str, object]):
    state = GraphState(project_truth_scope=["repo-a"])
    return validate_patch(state, _agent_patch(*operations), ["repo-a"])


def _research_question(node_id: str, *, title: str = "Staged question") -> dict[str, object]:
    return {
        "id": node_id,
        "type": "research_question",
        "title": title,
        "question": "Can later operations use graph objects created earlier in this patch?",
    }


def test_create_node_then_update_it_validates_in_written_order() -> None:
    report = _validate(
        {"op": "create_nodes", "nodes": [_research_question("rq/staged-question")]},
        {
            "op": "update_nodes",
            "nodes": [
                {
                    "id": "rq/staged-question",
                    "changes": {"motivation": "The prior operation established this node."},
                }
            ],
        },
    )

    assert not report.rejected


def test_create_ambiguity_then_resolve_it_validates_in_written_order() -> None:
    report = _validate(
        {
            "op": "create_ambiguities",
            "ambiguities": [
                {
                    "id": "amb/staged-ambiguity",
                    "question": "Which interpretation should be retained?",
                    "why_it_matters": "The answer changes the next experiment.",
                }
            ],
        },
        {
            "op": "resolve_ambiguities",
            "resolutions": [{"id": "amb/staged-ambiguity", "status": "resolved"}],
        },
    )

    assert not report.rejected


def test_same_node_id_created_by_separate_operations_is_rejected() -> None:
    report = _validate(
        {"op": "create_nodes", "nodes": [_research_question("rq/repeated-id")]},
        {
            "op": "create_nodes",
            "nodes": [_research_question("rq/repeated-id", title="Repeated question")],
        },
    )

    assert report.rejected
    assert any(message.code == "duplicate-node-id" for message in report.messages)


def test_proposal_can_target_decision_created_earlier_in_the_same_patch() -> None:
    report = _validate(
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "dec/staged-rule",
                    "type": "decision",
                    "title": "Staged evaluation rule",
                    "question": "Which evaluation rule should govern the experiment?",
                    "options": ["matched", "shifted"],
                },
                {
                    "id": "exp/staged-evaluation",
                    "type": "experiment",
                    "title": "Staged evaluation",
                    "objective": "Evaluate the intervention under the chosen rule.",
                },
            ],
        },
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "exp/staged-evaluation",
                    "target": "dec/staged-rule",
                    "relation": "governed_by",
                }
            ],
        },
        {
            "op": "create_proposals",
            "proposals": [
                {
                    "id": "prop/select-staged-rule",
                    "title": "Select the staged evaluation rule",
                    "card": {
                        "situation_cold": "The experiment needs one evaluation rule.",
                        "why_human_now": "Selecting the rule is a human decision.",
                        "consequences": "The experiment will use the matched rule.",
                        "decision_needed": "Approve or reject this selection.",
                    },
                    "ops": [
                        {
                            "op": "update_nodes",
                            "nodes": [
                                {
                                    "id": "dec/staged-rule",
                                    "changes": {
                                        "selected_option": "matched",
                                        "status": "decided",
                                    },
                                }
                            ],
                        }
                    ],
                    "related_node_ids": ["dec/staged-rule"],
                    "base_rev": 0,
                }
            ],
        },
    )

    assert not report.rejected


def test_edge_can_reference_a_node_created_later_in_the_same_patch() -> None:
    patch = _agent_patch(
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "rq/forward-reference",
                    "target": "hyp/forward-reference",
                    "relation": "has_hypothesis",
                }
            ],
        },
        {
            "op": "create_nodes",
            "nodes": [
                _research_question("rq/forward-reference"),
                {
                    "id": "hyp/forward-reference",
                    "type": "hypothesis",
                    "title": "Forward reference",
                    "statement": "The validator recognizes a same-patch node reference.",
                },
            ],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])
    report = validate_patch(state, patch, ["repo-a"])

    assert not report.rejected
    materialized = apply_valid_patch(state, patch)
    assert (
        materialized.edges["rq/forward-reference::has_hypothesis::hyp/forward-reference"].layer
        == "epistemic"
    )


def test_forward_edge_layer_is_derived_after_later_endpoints_materialize() -> None:
    patch = _agent_patch(
        {
            "op": "create_edges",
            "edges": [
                {
                    "source": "rq/forward-blocked",
                    "target": "blk/forward-blocker",
                    "relation": "blocked_by",
                }
            ],
        },
        {
            "op": "create_nodes",
            "nodes": [
                _research_question("rq/forward-blocked"),
                {
                    "id": "blk/forward-blocker",
                    "type": "blocker",
                    "title": "Forward blocker",
                    "description": "The blocker is created after its edge.",
                },
            ],
        },
    )
    state = GraphState(project_truth_scope=["repo-a"])

    report = validate_patch(state, patch, ["repo-a"])

    assert not report.rejected
    materialized = apply_valid_patch(state, patch)
    assert materialized.edges["rq/forward-blocked::blocked_by::blk/forward-blocker"].layer == "seam"
