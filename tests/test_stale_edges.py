from __future__ import annotations

import json

import pytest

from rcp.agents import AgentEvent
from rcp.agents.launcher import ProviderReadiness
from rcp.core.attention import project_graph_attention
from rcp.core.models import Patch
from rcp.providers.base import ModelChoice
from rcp.runs.proposal_refresh import proposal_refresh_message
from tests.helpers import append_fixture_patch, signed_in_client, wait_for_task
from tests.test_api_tasks import _event_frame
from tests.test_proposal_refresh import EV, HYP, PROPOSAL, _agent_patch, _reword_evidence, filed

__all__ = ["filed"]

EDGE = "edge/replan"


def _stale(service) -> list[str]:
    return project_graph_attention(service.history.state()).stale_edge_ids


def _rejudge(service, **changes):
    edge = (
        service.history.state()
        .edges[EDGE]
        .model_dump(
            mode="json", include={"id", "source", "target", "relation", "explanation", "assessment"}
        )
    )
    _agent_patch(
        service,
        [
            {"op": "remove_edges", "edge_ids": [EDGE]},
            {"op": "create_edges", "edges": [edge | changes]},
        ],
    )


def _context(message: str) -> dict:
    return json.JSONDecoder().raw_decode(message, message.index("{"))[0]


def test_an_outcome_edge_is_stale_until_rejudged_after_its_evidence_moves(filed):
    service = filed.state.service
    assert _stale(service) == []

    _reword_evidence(service)
    assert _stale(service) == [EDGE]

    _rejudge(service)
    assert _stale(service) == []

    # A standing judgment on the Evidence is a move too, as for Proposals.
    append_fixture_patch(
        service,
        Patch(
            kind="approval",
            author="human",
            summary="Contest the Evidence.",
            ops=[{"op": "set_standing", "node_id": EV, "standing": "contested"}],
        ),
    )
    assert _stale(service) == [EDGE]


def test_retired_evidence_marks_no_edge(filed):
    service = filed.state.service
    _agent_patch(
        service,
        [
            {
                "op": "create_nodes",
                "nodes": [
                    {
                        "id": "ev/replan-rerun",
                        "type": "evidence",
                        "title": "Replanning rerun",
                        "observation": "The rerun repeats the ablation.",
                        "origin": "analytic",
                    }
                ],
            }
        ],
    )
    _agent_patch(
        service,
        [{"op": "supersede_nodes", "nodes": [{"id": EV, "superseded_by": "ev/replan-rerun"}]}],
    )
    assert (
        service.history.state().nodes[EV].updated_rev
        > service.history.state().edges[EDGE].created_rev
    )
    assert _stale(service) == []


@pytest.fixture
def light_catalog(filed):
    service = filed.state.service
    launches = []

    async def stream(project_id, kind, request, execution):
        launches.append(kind)
        yield _event_frame(AgentEvent(event="done"))

    filed.state.background_tasks.stream = stream
    catalog = ProviderReadiness(
        provider=service.resolve_agent_profile("node_chat").provider,
        installed=True,
        authenticated=True,
        models=[ModelChoice(id="probed", label="Probed", reasoning=["minimal", "low", "high"])],
    )
    service.launcher.cached_readiness = lambda *_args, **_kwargs: catalog
    return launches


def test_edge_refresh_starts_one_light_work_chat_on_its_evidence(filed, light_catalog):
    service = filed.state.service
    store = filed.state.background_tasks.store
    client = signed_in_client(filed)
    url = f"/api/projects/{filed.state.default_project_id}/edges/{EDGE}/refresh"

    fresh = client.post(url, json={})
    assert fresh.status_code == 409
    assert fresh.json()["detail"]["code"] == "edge_not_stale"

    _reword_evidence(service)
    result = client.post(url, json={}).json()
    assert client.post(url, json={}).json() == result

    task = wait_for_task(store, result["operation_id"], expect="succeeded")
    assert task.kind == "node_chat"
    assert task.request["mode"] == "work"
    assert task.request["node_id"] == EV
    assert task.request["chat_id"] == result["chat_id"]
    assert task.request["reasoning"] == "minimal"
    assert task.request["invoked_skill_ids"] == ["evidence-triage"]
    context = _context(task.request["message"])
    assert context["edge"]["id"] == EDGE
    assert context["outcome"]["id"] == EV
    assert context["citing_proposal_ids"] == [PROPOSAL]
    assert light_catalog == ["node_chat"]

    # The re-judged edge is current; a retry still finds the same chat.
    _rejudge(service, explanation="Still supports it, more narrowly.")
    assert _stale(service) == []
    assert client.post(url, json={}).json() == result

    # A later reword is a new judgment and a new chat.
    _reword_evidence(service)
    assert client.post(url, json={}).json()["chat_id"] != result["chat_id"]


def test_proposal_refresh_names_its_stale_cause_edge(filed):
    service = filed.state.service
    _reword_evidence(service)
    state = service.history.state()
    context = _context(proposal_refresh_message(state, state.proposals[PROPOSAL]))
    assert [edge["id"] for edge in context["stale_cause_edges"]] == [EDGE]

    _rejudge(service)
    state = service.history.state()
    context = _context(proposal_refresh_message(state, state.proposals[PROPOSAL]))
    assert context["stale_cause_edges"] == []
    assert HYP in json.dumps(context["proposal"])


def test_one_update_rejudges_the_cause_edge_and_refiles_the_proposal(filed):
    service = filed.state.service
    _reword_evidence(service)
    state = service.history.state()
    stale = state.proposals[PROPOSAL]
    edge = state.edges[EDGE].model_dump(
        mode="json", include={"id", "source", "target", "relation", "explanation", "assessment"}
    )
    refiled = stale.model_dump(mode="json", include={"title", "card", "ops"}) | {
        "id": f"{PROPOSAL}-refiled"
    }
    _agent_patch(
        service,
        [
            {"op": "remove_edges", "edge_ids": [EDGE]},
            {"op": "create_edges", "edges": [edge]},
            {"op": "withdraw_proposals", "proposals": [{"id": PROPOSAL, "reason": "Refiled."}]},
            {"op": "create_proposals", "proposals": [refiled]},
        ],
    )
    attention = project_graph_attention(service.history.state())
    assert attention.stale_edge_ids == []
    assert attention.stale_proposal_ids == []
    assert attention.pending_proposal_ids == [f"{PROPOSAL}-refiled"]


def test_a_cache_from_before_stale_edges_takes_them_from_its_graph(filed, tmp_path):
    _reword_evidence(filed.state.service)
    client = signed_in_client(filed)
    project_id = filed.state.default_project_id
    client.get(f"/api/projects/{project_id}")
    cache_path = next((tmp_path / "data" / "project-snapshots").iterdir())
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["snapshot"]["attention"].pop("stale_edge_ids")
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    restored = client.get(f"/api/projects/{project_id}/cached")

    assert restored.status_code == 200
    assert restored.json()["attention"]["stale_edge_ids"] == [EDGE]


@pytest.mark.parametrize("revised", [False, True], ids=["kept", "revised"])
def test_a_merged_reword_lands_stale_unless_the_fork_revised_the_edge(manifest, revised):
    from tests.test_graph_invariants import REQUIRED, _agent_append
    from tests.test_merge_regressions import _answers, _append, _commit, _fork_history, _plan

    edge = {
        "id": "edge/fork",
        "source": "ev/fork",
        "target": "hyp/fork",
        "relation": "supports",
        "explanation": "Judged on the fork's base.",
        "assessment": {"relevance": "direct", "weight": "moderate"},
    }
    history, branch = _fork_history(
        manifest,
        [
            {"id": "hyp/fork", "type": "hypothesis", "title": "Claim", **REQUIRED["hypothesis"]},
            {"id": "ev/fork", "type": "evidence", "title": "Result", **REQUIRED["evidence"]},
        ],
        edges=[edge],
    )
    _append(
        branch,
        {"op": "update_nodes", "nodes": [{"id": "ev/fork", "changes": {"title": "Narrower"}}]},
    )
    if revised:
        _agent_append(
            branch,
            "fixture-branch",
            sorted(manifest.project.truth_scope),
            {"op": "remove_edges", "edge_ids": [edge["id"]]},
            {"op": "create_edges", "edges": [edge | {"explanation": "Re-judged on the fork."}]},
        )
    assert project_graph_attention(branch.state()).stale_edge_ids == (
        [] if revised else [edge["id"]]
    )

    plan = _plan(history, branch)
    _commit(history, branch, plan, _answers(plan))

    # Merge carries content only: an unchanged edge keeps main's older revision.
    assert project_graph_attention(history.state()).stale_edge_ids == (
        [] if revised else [edge["id"]]
    )
