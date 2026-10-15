from __future__ import annotations

import pytest

from rcp.agents import AgentEvent
from rcp.core.attention import project_graph_attention
from rcp.core.models import Patch, Proposal
from rcp.core.validation.proposals import proposal_is_stale
from rcp.providers import profile_for
from tests.helpers import (
    append_fixture_patch,
    create_named_app,
    seed_patch,
    signed_in_client,
    wait_for_task,
)
from tests.test_api_tasks import _event_frame

HYP = "hyp/replanning-restores-plasticity"
EV = "ev/replan-ablation"
PROPOSAL = "prop/activate-replanning"


def _agent_patch(service, ops):
    append_fixture_patch(
        service,
        Patch(
            kind="refresh",
            author="agent",
            summary="Agent change.",
            run_truth_scope=["repo-a"],
            repositories_read=["repo-a"],
            ops=ops,
        ),
    )


@pytest.fixture
def filed(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    _agent_patch(
        service,
        [
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": EV,
                        "type": "evidence",
                        "title": "Replanning ablation",
                        "observation": "Replanning recovers most plasticity.",
                        "origin": "analytic",
                    }
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "id": "edge/replan",
                        "source": EV,
                        "target": HYP,
                        "relation": "supports",
                        "assessment": {"relevance": "direct", "weight": "moderate"},
                    }
                ],
            },
        ],
    )
    _agent_patch(
        service,
        [
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": PROPOSAL,
                        "title": "Activate the replanning hypothesis",
                        "card": {
                            "situation_cold": "s",
                            "why_human_now": "w",
                            "consequences": "c",
                            "decision_needed": "d",
                        },
                        "ops": [
                            {
                                "op": "update_nodes",
                                "intent": "status_change",
                                "nodes": [
                                    {
                                        "id": HYP,
                                        "changes": {"status": "active"},
                                        "cause": {"kind": "evidence_edge", "ref_id": "edge/replan"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    )
    return app


def _reword_evidence(service):
    _agent_patch(
        service,
        [{"op": "update_nodes", "nodes": [{"id": EV, "changes": {"observation": "Narrower."}}]}],
    )


def test_rewording_cause_evidence_stales_only_rule_two_proposals(filed):
    service = filed.state.service
    proposal = service.history.state().proposals[PROPOSAL]
    assert proposal.dependency_rule == 2
    assert project_graph_attention(service.history.state()).stale_proposal_ids == []

    _reword_evidence(service)

    state = service.history.state()
    assert project_graph_attention(state).stale_proposal_ids == [PROPOSAL]
    # Proposals filed before rule 2 stay judged as replay judged them.
    legacy = Proposal.model_validate(proposal.model_dump(mode="json") | {"dependency_rule": 1})
    assert not proposal_is_stale(state, legacy)


def test_refresh_starts_one_light_work_chat_only_when_stale(filed):
    service = filed.state.service
    store = filed.state.background_tasks.store
    launches = []

    async def stream(project_id, kind, request, execution):
        launches.append(kind)
        yield _event_frame(AgentEvent(event="done"))

    filed.state.background_tasks.stream = stream
    client = signed_in_client(filed)
    url = f"/api/projects/{filed.state.default_project_id}/proposals/{PROPOSAL}/refresh"

    fresh = client.post(url, json={})
    assert fresh.status_code == 409
    assert fresh.json()["detail"]["code"] == "proposal_not_stale"

    _reword_evidence(service)
    result = client.post(url, json={}).json()
    assert client.post(url, json={}).json() == result

    task = wait_for_task(store, result["operation_id"], expect="succeeded")
    provider = service.resolve_agent_profile("node_chat").provider
    assert task.kind == "node_chat"
    assert task.request["mode"] == "work"
    assert task.request["node_id"] == HYP
    assert task.request["chat_id"] == result["chat_id"]
    assert task.request["reasoning"] == profile_for(provider).lowest_reasoning
    assert task.request["invoked_skill_ids"] == ["evidence-triage"]
    assert PROPOSAL in task.request["message"] and EV in task.request["message"]
    assert launches == ["node_chat"]

    # A response lost after the agent withdrew the Proposal still finds its chat.
    _agent_patch(
        service,
        [{"op": "withdraw_proposals", "proposals": [{"id": PROPOSAL, "reason": "Refiled."}]}],
    )
    assert client.post(url, json={}).json() == result


def test_cause_evidence_counts_when_its_edge_follows_the_proposal(manifest, tmp_path):
    app = create_named_app(str(manifest.path), data_dir=tmp_path / "data")
    service = app.state.service
    append_fixture_patch(service, seed_patch())
    _agent_patch(
        service,
        [
            {
                "op": "create_proposals",
                "proposals": [
                    {
                        "id": PROPOSAL,
                        "title": "Activate the replanning hypothesis",
                        "card": {
                            "situation_cold": "s",
                            "why_human_now": "w",
                            "consequences": "c",
                            "decision_needed": "d",
                        },
                        "ops": [
                            {
                                "op": "update_nodes",
                                "intent": "status_change",
                                "nodes": [
                                    {
                                        "id": HYP,
                                        "changes": {"status": "active"},
                                        "cause": {"kind": "evidence_edge", "ref_id": "edge/replan"},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": EV,
                        "type": "evidence",
                        "title": "Replanning ablation",
                        "observation": "Replanning recovers most plasticity.",
                        "origin": "analytic",
                    }
                ],
            },
            {
                "op": "create_edges",
                "edges": [
                    {
                        "id": "edge/replan",
                        "source": EV,
                        "target": HYP,
                        "relation": "supports",
                        "assessment": {"relevance": "direct", "weight": "moderate"},
                    }
                ],
            },
        ],
    )
    assert project_graph_attention(service.history.state()).stale_proposal_ids == []
    _reword_evidence(service)
    assert project_graph_attention(service.history.state()).stale_proposal_ids == [PROPOSAL]
