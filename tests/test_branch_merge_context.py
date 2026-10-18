from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from rcp.core.authority import AgentAuthorizerDeparted
from rcp.core.models import Patch
from rcp.core.transition_models import GraphTargetRef
from rcp.history import HistoryManager
from rcp.runs.branch_merge import (
    commit_branch_merge_with_history,
    merge_planning_context,
)
from rcp.runs.branch_merge_context import (
    _prior_merge_reviews,
    load_branch_merge_context,
    preview_merge_planning_context,
)
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.merge_contract import (
    MergeAnswers,
    ReviewCard,
    build_merge_patch,
    plan_merge,
    prepare_merge_candidate,
)
from rcp.service import ProposalDecisionRequest

from . import test_branch_merge_api as merge_api
from .helpers import authorized_human, unchecked_write_authority
from .test_owned_merge_storage import _owned_merge


@pytest.fixture
def context_branch(manifest, tmp_path, monkeypatch):
    seed = merge_api._main_fixture_patch().model_dump(mode="python")
    seed["ops"][0]["nodes"].extend(
        {
            "id": identity,
            "type": "research_question",
            "title": "Original title",
            "question": "Original question?",
        }
        for identity in ("rq/context", "rq/carried", "rq/unrelated")
    )
    monkeypatch.setattr(merge_api, "_main_fixture_patch", lambda: Patch.model_validate(seed))
    return merge_api._create_branch_harness(manifest, tmp_path, change="none")


def _edit(harness, history, identity, **changes):
    return history.append(
        Patch(
            kind="approval",
            author="human",
            summary="Edit the graph",
            ops=[
                {
                    "op": "update_nodes",
                    "nodes": [
                        {
                            "id": identity,
                            "base_updated_rev": history.state().nodes[identity].updated_rev,
                            "changes": changes,
                        }
                    ],
                }
            ],
        ),
        authorized_by=authorized_human(harness.app),
    )[0]


def _load(harness, monkeypatch):
    task = merge_api._admit_held_merge_task(harness, monkeypatch)
    context = load_branch_merge_context(
        harness.service,
        harness.store,
        harness.episode,
        operation_id=task.operation_id,
        authorized_by=authorized_human(harness.app),
    )
    planning = merge_planning_context(
        context, harness.service.history, created_at=datetime.fromisoformat(harness.store.now())
    )
    return task, context, plan_merge(planning)


def _commit(harness, task, plan):
    answers = MergeAnswers(
        answers={},
        cards={
            group.subject_key: ReviewCard(
                situation_cold="The source changed a protected subject.",
                why_human_now="Review the proposed change.",
                consequences="Approval changes the destination subject.",
                decision_needed="Approve or reject.",
            )
            for group in plan.review_groups
        },
    )
    prepare_merge_candidate(plan, answers, harness.service.history)
    committed = commit_branch_merge_with_history(
        harness.service.history,
        build_merge_patch(plan, answers),
        expected_main_head=plan.destination_head,
    )
    harness.store.complete_agent_task(
        task.operation_id, applied_revision=committed.patch.revision, result={}
    )
    return committed.patch


@pytest.mark.parametrize("resolution", ["pending", "rejected", "approved"])
def test_loaded_canonical_review_preserves_pending_and_resolved_remerge_effects(
    context_branch, monkeypatch, resolution
):
    harness = context_branch
    _edit(harness, harness.branch, "rq/context", question="Source question?")
    task, _, plan = _load(harness, monkeypatch)
    first = _commit(harness, task, plan)
    original = next(iter(harness.service.history.state().proposals.values()))
    if resolution != "pending":
        harness.service.decide_proposal(
            original.id,
            ProposalDecisionRequest(decision=resolution),
            authorized_by=authorized_human(harness.app),
        )
    if resolution == "approved":
        _edit(harness, harness.service.history, "rq/context", question="Later destination?")
    _edit(harness, harness.branch, "rq/context", title="New source title")
    task, context, plan = _load(harness, monkeypatch)
    assert len(context.prior_reviews) == 1
    prior = context.prior_reviews[0]
    assert prior.provenance == first.branch_merge
    assert prior.proposal == harness.service.history.state().proposals[original.id]
    assert prior.proposal.status == resolution
    assert plan.previous_source_graph is not None
    assert plan.previous_source_graph.nodes["rq/context"].title == "Original title"
    _commit(harness, task, plan)
    state = harness.service.history.state()
    replacement = next(p for p in state.proposals.values() if p.id != original.id)
    updates = {
        field: value
        for op in replacement.ops
        if op.op == "update_nodes"
        for node in op.nodes
        for field, value in node.changes.items()
    }
    assert updates["title"] == "New source title"
    if resolution == "pending":
        assert updates["question"] == "Source question?"
        assert state.proposals[original.id].status == "withdrawn"
    else:
        assert "question" not in updates
        assert state.proposals[original.id].status == resolution
    expected = "Later destination?" if resolution == "approved" else "Original question?"
    assert state.nodes["rq/context"].question == expected


def _fixture_history(harness, history):
    fixture = HistoryManager(
        harness.service.manifest,
        harness.service.history.workspace,
        write_authority=unchecked_write_authority(),
    )
    if history.graph_target.kind == "branch":
        fixture = fixture.branch(history.graph_target.branch_id)
    return fixture


def _file_proposal(harness, history, identity, subject):
    _fixture_history(harness, history).append(
        Patch(
            kind="work",
            author="agent",
            run_truth_scope=["repo-a"],
            summary="File a separate review",
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": identity,
                            "title": "Separate review",
                            "card": {
                                "situation_cold": "A separate change is pending.",
                                "why_human_now": "Human review is required.",
                                "consequences": "Approval changes the question.",
                                "decision_needed": "Approve or reject.",
                            },
                            "ops": [
                                {
                                    "op": "update_nodes",
                                    "intent": "content_change",
                                    "nodes": [
                                        {"id": subject, "changes": {"question": "Separate?"}}
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        ),
    )


def test_loader_scopes_reviews_by_canonical_source_destination_and_filing(
    context_branch, monkeypatch
):
    harness = context_branch
    _edit(harness, harness.branch, "rq/context", question="Source question?")
    _file_proposal(harness, harness.branch, "proposal/carried", "rq/carried")
    _file_proposal(harness, harness.service.history, "proposal/unrelated", "rq/unrelated")
    task, _, plan = _load(harness, monkeypatch)
    _commit(harness, task, plan)
    engine = harness.service.history_engine
    head = harness.service.history.head_ref()
    graph = engine.state_at(head)
    reviews = _prior_merge_reviews(
        engine, engine.steps(head), graph, source=harness.branch.graph_target, target=head.target
    )
    assert len(reviews) == 1
    assert reviews[0].proposal.id not in {"proposal/carried", "proposal/unrelated"}
    (change,) = reviews[0].outstanding
    assert (change.collection, change.entity_id, change.field_name) == (
        "nodes",
        "rq/context",
        "question",
    )
    assert change.branch.value == "Source question?"
    assert "filing_graph" not in reviews[0].model_dump(mode="json")
    other = GraphTargetRef(kind="branch", branch_id=str(uuid.uuid4()))
    assert not _prior_merge_reviews(
        engine, engine.steps(head), graph, source=other, target=head.target
    )
    assert not _prior_merge_reviews(
        engine, engine.steps(head), graph, source=harness.branch.graph_target, target=other
    )


def test_planning_adapter_uses_live_task_authority(context_branch, monkeypatch):
    harness = context_branch
    _edit(harness, harness.branch, "rq/context", question="Source question?")
    task, context, _ = _load(harness, monkeypatch)
    harness.service.history.write_authority.project_membership_check = lambda *_: False
    with pytest.raises(AgentAuthorizerDeparted):
        merge_planning_context(
            context, harness.service.history, created_at=datetime.fromisoformat(harness.store.now())
        )


def test_remerge_replaces_review_when_connected_subject_expands(context_branch, monkeypatch):
    harness = context_branch
    _edit(harness, harness.branch, "rq/context", question="Source question?")
    task, _, first = _load(harness, monkeypatch)
    _commit(harness, task, first)
    original = next(iter(harness.service.history.state().proposals.values()))
    _fixture_history(harness, harness.branch).append(
        Patch(
            kind="work",
            author="agent",
            summary="Connect the source questions",
            run_truth_scope=["repo-a"],
            ops=[
                {
                    "op": "create_proposals",
                    "proposals": [
                        {
                            "id": "proposal/source-relation",
                            "title": "Connect questions",
                            "card": original.card.model_dump(),
                            "ops": [
                                {
                                    "op": "create_edges",
                                    "intent": "protected_relation_change",
                                    "edges": [
                                        {
                                            "id": "rq/context::has_subquestion::rq/carried",
                                            "source": "rq/context",
                                            "target": "rq/carried",
                                            "relation": "has_subquestion",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )
    harness.service.for_graph_target(harness.branch.graph_target).decide_proposal(
        "proposal/source-relation",
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    task, context, plan = _load(harness, monkeypatch)
    assert len(plan.review_groups) == 1
    group = plan.review_groups[0]
    assert group.replaces_proposal_ids == (original.id,)
    assert group.subject_key != context.prior_reviews[0].subject_key
    _commit(harness, task, plan)
    state = harness.service.history.state()
    assert state.proposals[original.id].status == "withdrawn"
    assert len([p for p in state.proposals.values() if p.status == "pending"]) == 1


@pytest.mark.parametrize("dispatcher", ["episode", "member", "orchestrator"])
def test_preview_captures_real_membership_without_persisting_a_task(manifest, tmp_path, dispatcher):
    binding = None
    if dispatcher == "episode":
        harness = merge_api._create_branch_harness(manifest, tmp_path, change="evidence")
        episode = harness.episode
    else:
        harness, task = _owned_merge(manifest, tmp_path)
        binding = BranchMergeRunRequest.model_validate(task.request).merge_binding
        assert binding is not None
        episode = harness.episode
        if dispatcher == "member":
            episode = None
            binding = binding.model_copy(
                update={
                    "dispatcher_episode_id": None,
                    "dispatcher_operation_id": None,
                    "command_key": None,
                }
            )
    context = load_branch_merge_context(
        harness.service,
        harness.store,
        episode,
        operation_id="preview",
        authorized_by=authorized_human(harness.app),
        merge_binding=binding,
    )
    planning = preview_merge_planning_context(
        harness.service, harness.store, context, created_at=datetime.now(UTC)
    )
    plan_merge(planning)
    assert planning.captured_authority.apply_target == context.main_head.target
    assert harness.store.agent_task("preview") is None
    harness.service.history.write_authority.project_membership_check = lambda *_: False
    with pytest.raises(AgentAuthorizerDeparted):
        preview_merge_planning_context(
            harness.service, harness.store, context, created_at=datetime.now(UTC)
        )
