from __future__ import annotations

import pytest

from rcp.core.models import Patch
from rcp.core.validation import validate_patch
from rcp.history import HistoryManager
from tests.helpers import seed_patch


def _agent_patch(*ops: dict) -> Patch:
    return Patch(
        kind="refresh",
        author="agent",
        summary="Exercised the minimal Proposal boundary.",
        run_truth_scope=["repo-a"],
        repositories_read=["repo-a"],
        ops=list(ops),
    )


def _proposal(*, proposal_id: str, node_id: str, changes: dict, cause: dict | None = None) -> dict:
    update = {"id": node_id, "changes": changes}
    if cause is not None:
        update["cause"] = cause
    return {
        "op": "create_proposals",
        "proposals": [
            {
                "id": proposal_id,
                "title": "Review the semantic transition",
                "card": {
                    "situation_cold": "The research state now supports a semantic transition.",
                    "why_human_now": "Only the human controls this transition.",
                    "consequences": "The selected research state will change.",
                    "decision_needed": "Approve or reject the transition.",
                },
                "ops": [{"op": "update_nodes", "nodes": [update]}],
                "related_node_ids": [node_id],
                "base_rev": 2,
            }
        ],
    }


def _state_with_decision(manifest, *, governed: bool = True):
    history = HistoryManager(manifest)
    history.append(seed_patch())
    edges = [
        {
            "source": "exp/evaluation",
            "target": "hyp/replanning-restores-plasticity",
            "relation": "tests",
        },
        {
            "id": "edge/evaluation-support",
            "source": "ev/evaluation-result",
            "target": "hyp/replanning-restores-plasticity",
            "relation": "supports",
        },
        {
            "source": "exp/evaluation",
            "target": "ev/evaluation-result",
            "relation": "produces",
        },
    ]
    if governed:
        edges.append(
            {
                "source": "exp/evaluation",
                "target": "dec/evaluation-rule",
                "relation": "governed_by",
            }
        )
    history.append(
        _agent_patch(
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "dec/evaluation-rule",
                        "type": "decision",
                        "title": "Evaluation rule",
                        "question": "Which evaluation rule should govern the experiment?",
                        "options": ["matched", "shifted"],
                    },
                    {
                        "id": "exp/evaluation",
                        "type": "experiment",
                        "title": "Evaluation",
                        "objective": "Evaluate the intervention under the chosen rule.",
                    },
                    {
                        "id": "ev/evaluation-result",
                        "type": "evidence",
                        "title": "Evaluation result",
                        "observation": "The matched evaluation improved.",
                        "origin": "internal_run",
                    },
                ],
            },
            {"op": "create_edges", "edges": edges},
        )
    )
    return history.state()


def test_ordinary_agent_update_clears_accepted_standing(manifest) -> None:
    history = HistoryManager(manifest)
    history.append(seed_patch())
    history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Accepted the question.",
            ops=[
                {
                    "op": "set_standing",
                    "node_id": "rq/learning-after-shift",
                    "standing": "accepted",
                }
            ],
        )
    )

    history.append(
        _agent_patch(
            {
                "op": "update_nodes",
                "nodes": [
                    {
                        "id": "rq/learning-after-shift",
                        "changes": {"motivation": "Repeated shifts make this question urgent."},
                    }
                ],
            }
        )
    )

    node = history.state().nodes["rq/learning-after-shift"]
    assert node.motivation == "Repeated shifts make this question urgent."
    assert node.standing == "asserted"


@pytest.mark.parametrize(
    ("direct_update", "proposal"),
    [
        (
            {
                "id": "dec/evaluation-rule",
                "changes": {"status": "decided", "selected_option": "matched"},
            },
            _proposal(
                proposal_id="prop/select-evaluation",
                node_id="dec/evaluation-rule",
                changes={"status": "decided", "selected_option": "matched"},
            ),
        ),
        (
            {
                "id": "hyp/replanning-restores-plasticity",
                "changes": {"status": "active"},
                "cause": {"kind": "evidence_edge", "ref_id": "edge/evaluation-support"},
            },
            _proposal(
                proposal_id="prop/activate-hypothesis",
                node_id="hyp/replanning-restores-plasticity",
                changes={"status": "active"},
                cause={"kind": "evidence_edge", "ref_id": "edge/evaluation-support"},
            ),
        ),
    ],
)
def test_decision_and_belief_transitions_require_exact_proposals(
    manifest, direct_update: dict, proposal: dict
) -> None:
    state = _state_with_decision(manifest)

    direct_report = validate_patch(
        state,
        _agent_patch({"op": "update_nodes", "nodes": [direct_update]}),
        ["repo-a", "repo-b"],
    )
    proposal_report = validate_patch(
        state,
        _agent_patch(proposal),
        ["repo-a", "repo-b"],
    )

    assert direct_report.rejected
    assert not proposal_report.rejected


def test_decision_proposal_requires_an_experiment_input_edge(manifest) -> None:
    state = _state_with_decision(manifest, governed=False)
    proposal = _proposal(
        proposal_id="prop/select-evaluation",
        node_id="dec/evaluation-rule",
        changes={"status": "decided", "selected_option": "matched"},
    )

    ungoverned = validate_patch(state, _agent_patch(proposal), ["repo-a", "repo-b"])
    historical = validate_patch(
        state,
        _agent_patch(proposal).model_copy(update={"revision": 3}),
        ["repo-a", "repo-b"],
        mode="replay",
    )
    same_patch_governed = validate_patch(
        state,
        _agent_patch(
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "exp/evaluation",
                        "target": "dec/evaluation-rule",
                        "relation": "governed_by",
                    }
                ],
            },
            proposal,
        ),
        ["repo-a", "repo-b"],
    )
    same_patch_experiment = validate_patch(
        state,
        _agent_patch(
            {
                "op": "create_edges",
                "edges": [
                    {
                        "source": "exp/same-patch",
                        "target": "dec/evaluation-rule",
                        "relation": "governed_by",
                    }
                ],
            },
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "exp/same-patch",
                        "type": "experiment",
                        "title": "Same-patch experiment",
                        "objective": "Use the proposed evaluation rule.",
                    }
                ],
            },
            proposal,
        ),
        ["repo-a", "repo-b"],
    )

    assert ungoverned.rejected
    assert any(message.code == "invalid-agent-proposal-shape" for message in ungoverned.messages)
    assert not historical.rejected
    assert not same_patch_governed.rejected
    assert not same_patch_experiment.rejected


@pytest.mark.parametrize(
    "cause",
    [
        None,
        {"kind": "decision", "ref_id": "dec/evaluation-rule"},
        {"kind": "proposal_resolution", "ref_id": "prop/activate-hypothesis"},
    ],
)
def test_hypothesis_proposal_requires_an_evidence_edge_cause(manifest, cause) -> None:
    state = _state_with_decision(manifest)
    proposal = _proposal(
        proposal_id="prop/activate-hypothesis",
        node_id="hyp/replanning-restores-plasticity",
        changes={"status": "active"},
        cause=cause,
    )

    report = validate_patch(state, _agent_patch(proposal), ["repo-a", "repo-b"])

    assert report.rejected
    assert any(message.code == "invalid-agent-proposal-shape" for message in report.messages)


def test_agent_proposal_rejects_a_third_shape(manifest) -> None:
    state = _state_with_decision(manifest)
    edge_proposal = {
        "op": "create_proposals",
        "proposals": [
            {
                "id": "prop/add-edge",
                "title": "Add an ordinary edge",
                "card": {"decision_needed": "Approve the edge?"},
                "ops": [
                    {
                        "op": "create_edges",
                        "edges": [
                            {
                                "source": "rq/learning-after-shift",
                                "target": "dec/evaluation-rule",
                                "relation": "has_decision",
                            }
                        ],
                    }
                ],
                "related_node_ids": ["rq/learning-after-shift", "dec/evaluation-rule"],
                "base_rev": 2,
            }
        ],
    }

    report = validate_patch(
        state,
        _agent_patch(edge_proposal),
        ["repo-a", "repo-b"],
    )

    assert report.rejected
