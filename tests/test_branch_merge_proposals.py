from __future__ import annotations

import json
from pathlib import Path

import pytest

from rcp.core.models import Patch, Proposal
from rcp.core.validation import validate_patch
from rcp.runs.branch_merge import branch_human_review_changes, branch_merge_review_proposal_id
from rcp.service import ProposalDecisionRequest

from . import test_branch_merge_api as merge_api
from .helpers import authorized_human, wait_for_task


@pytest.fixture
def review_branch(manifest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    seed = merge_api._main_fixture_patch().model_dump(mode="python")
    seed["ops"][0]["nodes"].extend(
        [
            {
                "id": "rq/review",
                "type": "research_question",
                "title": "Question",
                "question": "Original?",
            },
            {
                "id": "hyp/review",
                "type": "hypothesis",
                "title": "Hypothesis",
                "statement": "Original claim.",
            },
            {
                "id": "ev/review",
                "type": "evidence",
                "title": "Evidence",
                "observation": "A result.",
                "origin": "analytic",
            },
        ]
    )
    seed["ops"].append(
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/review",
                    "source": "ev/review",
                    "target": "hyp/review",
                    "relation": "supports",
                    "assessment": {
                        "relevance": "direct",
                        "weight": "moderate",
                        "scope": "Observed result.",
                    },
                }
            ],
        }
    )
    seed["ops"].append(
        {
            "op": "create_edges",
            "edges": [
                {
                    "id": "edge/question-hypothesis",
                    "source": "rq/review",
                    "target": "hyp/review",
                    "relation": "has_hypothesis",
                }
            ],
        }
    )
    monkeypatch.setattr(merge_api, "_main_fixture_patch", lambda: Patch.model_validate(seed))
    if getattr(request, "param", None) == "accepted_blocker":
        append_seed = merge_api.append_fixture_patch

        def append_accepted_seed(service, patch, **kwargs):
            result = append_seed(service, patch, **kwargs)
            append_seed(
                service,
                Patch(
                    kind="approval",
                    author="human",
                    summary="Accept the seed blocker.",
                    ops=[
                        {"op": "set_standing", "node_id": "blk/merge-ready", "standing": "accepted"}
                    ],
                ),
            )
            return result

        monkeypatch.setattr(merge_api, "append_fixture_patch", append_accepted_seed)
    return merge_api._create_branch_harness(manifest, tmp_path, change="none")


def _human_patch(harness, *ops):
    return harness.branch.append(
        Patch(kind="approval", author="human", summary="Review branch graph.", ops=list(ops)),
        authorized_by=authorized_human(harness.app),
    )[0]


def _edit(harness, node_id: str, changes: dict):
    node = harness.branch.state().nodes[node_id]
    return _human_patch(
        harness,
        {
            "op": "update_nodes",
            "nodes": [{"id": node_id, "base_updated_rev": node.updated_rev, "changes": changes}],
        },
    )


def _agent_patch(harness, *ops):
    return harness.branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Record staged research.",
            run_truth_scope=["repo-a"],
            source_operation_id=harness.root.operation_id,
            ops=list(ops),
        )
    )[0]


def _proposal(operation: dict, *, identity: str = "prop/review") -> dict:
    return {
        "id": identity,
        "title": "Review the branch change",
        "card": {
            "situation_cold": "The staged research graph contains this change.",
            "why_human_now": "Main requires a separate human review.",
            "consequences": "Approving applies the displayed change to main.",
            "decision_needed": "Approve or reject this change.",
        },
        "ops": [operation],
    }


def _content(*, question: str = "Revised?", **changes) -> dict:
    return {
        "op": "update_nodes",
        "intent": "content_change",
        "nodes": [{"id": "rq/review", "changes": {"question": question, **changes}}],
    }


def _merge(harness, monkeypatch, *ops, expect: str = "succeeded"):
    launcher = merge_api._PatchWritingLauncher(
        json.dumps(
            {
                "summary": "Bring branch changes to main for review.",
                "ops": list(ops),
                "repositories_read": [],
            }
        )
    )
    monkeypatch.setattr(harness.app.state.launcher, "stream", launcher.stream)
    response = harness.client.post(
        f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge"
    )
    assert response.status_code == 202, response.text
    task = wait_for_task(
        harness.store, response.json()["graph_branch"]["active_merge_task_id"], expect=expect
    )
    return task, launcher


def test_merge_commits_direct_changes_and_pending_protected_review_atomically(
    review_branch, monkeypatch
):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    _edit(harness, "blk/merge-ready", {"status": "resolved"})
    main_before = harness.service.history.state()

    task, _launcher = _merge(
        harness,
        monkeypatch,
        {
            "op": "update_nodes",
            "nodes": [{"id": "blk/merge-ready", "changes": {"status": "resolved"}}],
        },
        {"op": "create_proposals", "proposals": [_proposal(_content())]},
    )

    main = harness.service.history.state()
    assert main.revision == main_before.revision + 1 == task.applied_revision
    assert main.nodes["rq/review"].question == "Original?"
    assert main.nodes["blk/merge-ready"].status == "resolved"
    proposal = next(iter(main.proposals.values()))
    assert proposal.status == "pending"
    assert proposal.id.startswith("prop/branch-review-")
    assert proposal.created_by == "agent"
    assert harness.branch.state().nodes["rq/review"].question == "Revised?"
    assert harness.service.history.materialize(write_outputs=False).state == main
    approved = harness.service.decide_proposal(
        proposal.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert approved.nodes["rq/review"].question == "Revised?"


@pytest.mark.parametrize("standing", ["accepted", "contested"])
def test_human_branch_standing_returns_to_main_inbox_and_replays_exactly(
    review_branch, monkeypatch, standing
):
    harness = review_branch
    _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": standing})
    _merge(
        harness,
        monkeypatch,
        {
            "op": "create_proposals",
            "proposals": [
                _proposal(
                    {
                        "op": "set_standing",
                        "intent": "standing_change",
                        "node_id": "hyp/review",
                        "standing": standing,
                    }
                )
            ],
        },
    )
    main = harness.service.history.state()
    proposal = next(iter(main.proposals.values()))
    assert proposal.related_node_ids == ["hyp/review"]
    assert main.nodes["hyp/review"].standing == "asserted"
    approved = harness.service.decide_proposal(
        proposal.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert approved.nodes["hyp/review"].standing == standing
    assert harness.service.history.materialize(write_outputs=False).state == approved


def test_later_merge_reviews_human_standing_reverted_to_the_original_base(
    review_branch, monkeypatch
):
    harness = review_branch
    prior_standing = harness.branch.base_state().nodes["hyp/review"].standing
    assert prior_standing == "asserted"
    review_ids = set()
    for standing in ("accepted", "asserted", "accepted"):
        _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": standing})
        _merge(
            harness,
            monkeypatch,
            {
                "op": "create_proposals",
                "proposals": [
                    _proposal(
                        {
                            "op": "set_standing",
                            "intent": "standing_change",
                            "node_id": "hyp/review",
                            "standing": standing,
                        }
                    )
                ],
            },
        )
        main = harness.service.history.state()
        assert main.nodes["hyp/review"].standing == prior_standing
        pending = [proposal for proposal in main.proposals.values() if proposal.status == "pending"]
        assert len(pending) == 1
        assert pending[0].id not in review_ids
        review_ids.add(pending[0].id)
        approved = harness.service.decide_proposal(
            pending[0].id,
            ProposalDecisionRequest(decision="approved"),
            authorized_by=authorized_human(harness.app),
        )
        assert approved.nodes["hyp/review"].standing == standing
        assert harness.service.history.materialize(write_outputs=False).state == approved
        prior_standing = standing
    assert len(harness.branch.validated_merge_receipts()) == 3


def test_later_merge_reviews_approved_hypothesis_status_reverted_to_the_original_base(
    review_branch, monkeypatch
):
    harness = review_branch
    branch_service = harness.service.for_graph_target(harness.branch.graph_target)
    prior_status = harness.branch.base_state().nodes["hyp/review"].status
    assert prior_status == "proposed"
    review_ids = set()
    for occurrence, status in enumerate(("active", "proposed", "active")):
        source_id = f"prop/branch-status-{occurrence}"
        operation = {
            "op": "update_nodes",
            "intent": "status_change",
            "nodes": [
                {
                    "id": "hyp/review",
                    "changes": {"status": status},
                    "cause": {"kind": "evidence_edge", "ref_id": "edge/review"},
                }
            ],
        }
        _agent_patch(
            harness,
            {
                "op": "create_proposals",
                "proposals": [_proposal(operation, identity=source_id)],
            },
        )
        branch_service.decide_proposal(
            source_id,
            ProposalDecisionRequest(decision="approved"),
            authorized_by=authorized_human(harness.app),
        )
        operation["nodes"][0]["cause"] = {"kind": "human_edit"}
        _merge(
            harness,
            monkeypatch,
            {"op": "create_proposals", "proposals": [_proposal(operation)]},
        )
        main = harness.service.history.state()
        assert main.nodes["hyp/review"].status == prior_status
        assert source_id not in main.proposals
        pending = [proposal for proposal in main.proposals.values() if proposal.status == "pending"]
        assert len(pending) == 1
        assert pending[0].id not in review_ids
        review_ids.add(pending[0].id)
        approved = harness.service.decide_proposal(
            pending[0].id,
            ProposalDecisionRequest(decision="approved"),
            authorized_by=authorized_human(harness.app),
        )
        assert approved.nodes["hyp/review"].status == status
        assert approved.nodes["hyp/review"].standing == "accepted"
        assert harness.branch.state().proposals[source_id].status == "approved"
        assert harness.service.history.materialize(write_outputs=False).state == approved
        prior_status = status
    assert len(harness.branch.validated_merge_receipts()) == 3


def test_later_source_occurrence_reuses_an_equivalent_live_pending_review(
    review_branch, monkeypatch
):
    harness = review_branch
    operation = {
        "op": "set_standing",
        "intent": "standing_change",
        "node_id": "hyp/review",
        "standing": "accepted",
    }
    _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "accepted"})
    _merge(
        harness,
        monkeypatch,
        {"op": "create_proposals", "proposals": [_proposal(operation)]},
    )
    main = harness.service.history.state()
    pending = next(iter(main.proposals.values()))
    for standing in ("asserted", "accepted"):
        _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": standing})
        _task, launcher = _merge(harness, monkeypatch)
        assert launcher.calls == 0
        assert harness.service.history.state() == main
    assert len(harness.branch.validated_merge_receipts()) == 3
    approved = harness.service.decide_proposal(
        pending.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert approved.nodes["hyp/review"].standing == "accepted"
    assert len(approved.proposals) == 1


def test_merge_retry_keeps_review_identity_when_main_advances(review_branch, monkeypatch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    proposal = _proposal(_content())
    identity = branch_merge_review_proposal_id(
        harness.branch.branch_id,
        Proposal.model_validate(proposal),
        source_base_head=harness.branch.branch_metadata().base_head,
    )
    _merge(
        harness,
        monkeypatch,
        {"op": "create_proposals", "proposals": [proposal]},
        {
            "op": "update_nodes",
            "nodes": [{"id": "blk/merge-ready", "changes": {"description": "Unrelated."}}],
        },
        expect="failed",
    )
    assert harness.branch.validated_merge_receipts() == []
    assert harness.service.history.state().proposals == {}
    merge_api.append_fixture_patch(
        harness.service,
        Patch(
            kind="approval",
            author="human",
            summary="Advance main before retrying the unchanged source.",
            ops=[{"op": "set_standing", "node_id": "blk/merge-ready", "standing": "accepted"}],
        ),
    )
    proposal["title"] = "Reworded review title after retry"
    _merge(harness, monkeypatch, {"op": "create_proposals", "proposals": [proposal]})
    main = harness.service.history.state()
    assert set(main.proposals) == {identity}
    assert main.proposals[identity].status == "pending"
    assert main.nodes["blk/merge-ready"].standing == "accepted"
    assert len(harness.branch.validated_merge_receipts()) == 1


@pytest.mark.parametrize("extra", ["wrong_value", "unrelated_field", "same_value_field"])
def test_merge_review_cannot_smuggle_or_misrepresent_source_changes(
    review_branch, monkeypatch, extra
):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    operation = _content(
        question="Wrong?" if extra == "wrong_value" else "Revised?",
        **(
            {"title": "Unrelated" if extra == "unrelated_field" else "Question"}
            if extra != "wrong_value"
            else {}
        ),
    )
    before = harness.service.history.state()
    _merge(
        harness,
        monkeypatch,
        {"op": "create_proposals", "proposals": [_proposal(operation)]},
        expect="failed",
    )
    assert harness.service.history.state() == before
    assert harness.branch.merge_receipts() == []


def test_later_branch_head_does_not_repeat_a_rejected_main_review(review_branch, monkeypatch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    _merge(harness, monkeypatch, {"op": "create_proposals", "proposals": [_proposal(_content())]})
    proposal = next(iter(harness.service.history.state().proposals.values()))
    harness.service.decide_proposal(
        proposal.id,
        ProposalDecisionRequest(decision="rejected", reason="Keep the original."),
        authorized_by=authorized_human(harness.app),
    )
    _edit(harness, "blk/merge-ready", {"status": "resolved"})
    _merge(
        harness,
        monkeypatch,
        {
            "op": "update_nodes",
            "nodes": [{"id": "blk/merge-ready", "changes": {"status": "resolved"}}],
        },
    )
    main = harness.service.history.state()
    assert len(main.proposals) == 1
    assert main.proposals[proposal.id].status == "rejected"
    assert main.nodes["rq/review"].question == "Original?"
    assert main.nodes["blk/merge-ready"].status == "resolved"
    assert len(harness.branch.validated_merge_receipts()) == 2


def test_special_review_intents_are_rejected_outside_canonical_merge(review_branch):
    harness = review_branch
    for operation in [
        {
            "op": "set_standing",
            "intent": "standing_change",
            "node_id": "hyp/review",
            "standing": "accepted",
        },
        {
            "op": "update_nodes",
            "intent": "status_change",
            "nodes": [
                {
                    "id": "hyp/review",
                    "changes": {"status": "active"},
                    "cause": {"kind": "human_edit"},
                }
            ],
        },
    ]:
        state = harness.service.history.state()
        proposal = _proposal(operation)
        proposal["base_rev"] = state.revision
        report = validate_patch(
            state,
            Patch(
                revision=state.revision + 1,
                kind="work",
                author="agent",
                profile="orchestrator",
                summary="Unattributed claim of human authority.",
                run_truth_scope=["repo-a"],
                ops=[{"op": "create_proposals", "proposals": [proposal]}],
            ),
            state.project_truth_scope,
        )
        assert report.rejected


def test_same_node_content_and_standing_review_approves_atomically(review_branch, monkeypatch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    _human_patch(harness, {"op": "set_standing", "node_id": "rq/review", "standing": "contested"})
    proposal = _proposal(_content())
    proposal["ops"].append(
        {
            "op": "set_standing",
            "intent": "standing_change",
            "node_id": "rq/review",
            "standing": "contested",
        }
    )
    identity = branch_merge_review_proposal_id(
        harness.branch.branch_id,
        Proposal.model_validate(proposal),
        source_base_head=harness.branch.branch_metadata().base_head,
    )
    proposal["ops"].reverse()
    assert (
        branch_merge_review_proposal_id(
            harness.branch.branch_id,
            Proposal.model_validate(proposal),
            source_base_head=harness.branch.branch_metadata().base_head,
        )
        == identity
    )
    _merge(harness, monkeypatch, {"op": "create_proposals", "proposals": [proposal]})
    pending = next(iter(harness.service.history.state().proposals.values()))
    assert [operation.intent for operation in pending.ops] == ["content_change", "standing_change"]
    before = harness.service.history.state().revision
    approved = harness.service.decide_proposal(
        pending.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert approved.revision == before + 1
    assert approved.nodes["rq/review"].question == "Revised?"
    assert approved.nodes["rq/review"].standing == "contested"
    assert approved.proposals[pending.id].status == "approved"


@pytest.mark.parametrize("include_other_edits", [False, True])
def test_branch_approval_with_retired_evidence_becomes_a_fresh_main_review(
    review_branch, monkeypatch, include_other_edits
):
    harness = review_branch
    _agent_patch(
        harness,
        {
            "op": "create_proposals",
            "proposals": [
                _proposal(
                    {
                        "op": "update_nodes",
                        "intent": "status_change",
                        "nodes": [
                            {
                                "id": "hyp/review",
                                "changes": {"status": "active"},
                                "cause": {"kind": "evidence_edge", "ref_id": "edge/review"},
                            }
                        ],
                    },
                    identity="prop/branch-status",
                )
            ],
        },
    )
    branch_service = harness.service.for_graph_target(harness.branch.graph_target)
    branch_service.decide_proposal(
        "prop/branch-status",
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    _human_patch(harness, {"op": "remove_edges", "edge_ids": ["edge/review"]})
    proposal = _proposal(
        {
            "op": "update_nodes",
            "intent": "status_change",
            "nodes": [
                {
                    "id": "hyp/review",
                    "changes": {"status": "active"},
                    "cause": {"kind": "human_edit"},
                }
            ],
        }
    )
    if include_other_edits:
        _edit(harness, "hyp/review", {"statement": "Revised claim."})
        _human_patch(
            harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "contested"}
        )
        proposal["ops"].extend(
            [
                {
                    "op": "update_nodes",
                    "intent": "content_change",
                    "nodes": [{"id": "hyp/review", "changes": {"statement": "Revised claim."}}],
                },
                {
                    "op": "set_standing",
                    "intent": "standing_change",
                    "node_id": "hyp/review",
                    "standing": "contested",
                },
            ]
        )
    _merge(
        harness,
        monkeypatch,
        {"op": "remove_edges", "edge_ids": ["edge/review"]},
        {"op": "create_proposals", "proposals": [proposal]},
    )
    main = harness.service.history.state()
    assert main.nodes["hyp/review"].status == "proposed"
    assert "edge/review" not in main.edges
    assert "prop/branch-status" not in main.proposals
    pending = next(iter(main.proposals.values()))
    assert pending.status == "pending"
    approved = harness.service.decide_proposal(
        pending.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert approved.nodes["hyp/review"].status == "active"
    assert approved.nodes["hyp/review"].standing == (
        "contested" if include_other_edits else "accepted"
    )
    assert approved.nodes["hyp/review"].statement == (
        "Revised claim." if include_other_edits else "Original claim."
    )
    assert harness.branch.state().proposals["prop/branch-status"].status == "approved"
    assert harness.service.history.materialize(write_outputs=False).state == approved


def test_merge_cannot_claim_agent_standing_as_a_human_source_fact(review_branch, monkeypatch):
    harness = review_branch
    _agent_patch(
        harness, {"op": "set_standing", "node_id": "blk/merge-ready", "standing": "accepted"}
    )
    rejected = Patch(
        revision=harness.branch.state().revision,
        kind="approval",
        author="human",
        admission="rejected",
        summary="A rejected request is not a human source effect.",
        ops=[{"op": "set_standing", "node_id": "blk/merge-ready", "standing": "accepted"}],
    )
    assert (
        branch_human_review_changes([rejected], harness.branch.base_state(), harness.branch.state())
        == []
    )
    before = harness.service.history.state()
    _merge(
        harness,
        monkeypatch,
        {
            "op": "create_proposals",
            "proposals": [
                _proposal(
                    {
                        "op": "set_standing",
                        "intent": "standing_change",
                        "node_id": "blk/merge-ready",
                        "standing": "accepted",
                    }
                )
            ],
        },
        expect="failed",
    )
    assert harness.service.history.state() == before


@pytest.mark.parametrize("review_branch", ["accepted_blocker"], indirect=True)
def test_removed_accepted_ordinary_node_requires_main_review(review_branch, monkeypatch):
    harness = review_branch
    _human_patch(
        harness, {"op": "set_standing", "node_id": "blk/merge-ready", "standing": "contested"}
    )
    _human_patch(harness, {"op": "remove_nodes", "node_ids": ["blk/merge-ready"]})
    state = harness.service.history.state()
    ordinary_proposal = _proposal(
        {"op": "remove_nodes", "intent": "removal", "node_ids": ["blk/merge-ready"]}
    )
    ordinary_proposal["base_rev"] = state.revision
    for operation in [
        {"op": "remove_nodes", "node_ids": ["blk/merge-ready"]},
        {"op": "create_proposals", "proposals": [ordinary_proposal]},
    ]:
        report = validate_patch(
            state,
            Patch(
                revision=state.revision + 1,
                kind="work",
                author="agent",
                profile="orchestrator",
                summary="Ordinary agent attempts accepted-node removal.",
                run_truth_scope=["repo-a"],
                ops=[operation],
            ),
            state.project_truth_scope,
        )
        assert report.rejected
    _merge(
        harness,
        monkeypatch,
        {
            "op": "create_proposals",
            "proposals": [
                _proposal(
                    {
                        "op": "remove_nodes",
                        "intent": "removal",
                        "node_ids": ["blk/merge-ready"],
                    }
                )
            ],
        },
    )
    main = harness.service.history.state()
    assert main.nodes["blk/merge-ready"].standing == "accepted"
    pending = next(iter(main.proposals.values()))
    approved = harness.service.decide_proposal(
        pending.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert "blk/merge-ready" not in approved.nodes


@pytest.mark.parametrize("review_branch", ["accepted_blocker"], indirect=True)
def test_ordinary_content_merge_keeps_the_standing_both_sides_agree_on(review_branch, monkeypatch):
    harness = review_branch
    _edit(harness, "blk/merge-ready", {"description": "Revised description."})

    _task, launcher = _merge(harness, monkeypatch)

    # Branch and main still agree the node is accepted, so the merge carries the
    # edit and its standing itself rather than asking an agent to restore it.
    assert launcher.calls == 0
    main = harness.service.history.state()
    assert main.nodes["blk/merge-ready"].standing == "accepted"
    assert main.nodes["blk/merge-ready"].description == "Revised description."


def test_stale_source_proposal_stays_on_branch_while_its_edit_is_reviewed(
    review_branch, monkeypatch
):
    harness = review_branch
    _agent_patch(
        harness,
        {
            "op": "create_proposals",
            "proposals": [_proposal(_content(), identity="prop/old-source")],
        },
    )
    _edit(harness, "rq/review", {"question": "Revised?"})
    _merge(harness, monkeypatch, {"op": "create_proposals", "proposals": [_proposal(_content())]})
    main = harness.service.history.state()
    assert len(main.proposals) == 1
    assert "prop/old-source" not in main.proposals
    assert harness.branch.state().proposals["prop/old-source"].status == "pending"


def test_live_source_proposal_keeps_its_identity_and_stays_pending(review_branch, monkeypatch):
    harness = review_branch
    source = _proposal(_content(), identity="prop/live-source")
    _agent_patch(harness, {"op": "create_proposals", "proposals": [source]})
    _merge(harness, monkeypatch, {"op": "create_proposals", "proposals": [source]})
    main = harness.service.history.state()
    assert set(main.proposals) == {"prop/live-source"}
    assert main.proposals["prop/live-source"].status == "pending"
    assert main.nodes["rq/review"].question == "Original?"


@pytest.mark.parametrize("remove", ["node", "relation"])
def test_protected_removal_is_reviewed_with_its_exact_incident_graph_effects(
    review_branch, monkeypatch, remove
):
    harness = review_branch
    operation = (
        {"op": "remove_nodes", "node_ids": ["rq/review"]}
        if remove == "node"
        else {"op": "remove_edges", "edge_ids": ["edge/question-hypothesis"]}
    )
    _human_patch(harness, operation)
    _merge(
        harness,
        monkeypatch,
        {
            "op": "create_proposals",
            "proposals": [
                _proposal(
                    {
                        **operation,
                        "intent": "removal" if remove == "node" else "protected_relation_change",
                    }
                )
            ],
        },
    )
    main = harness.service.history.state()
    assert "rq/review" in main.nodes
    assert "edge/question-hypothesis" in main.edges
    pending = next(iter(main.proposals.values()))
    approved = harness.service.decide_proposal(
        pending.id,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    assert "edge/question-hypothesis" not in approved.edges
    assert ("rq/review" not in approved.nodes) == (remove == "node")


def test_later_merge_replays_a_prior_source_head_that_ended_on_a_rejected_patch(
    review_branch, monkeypatch
):
    harness = review_branch
    _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "accepted"})
    rejected = harness.branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Retain a rejected branch revision at the head.",
            run_truth_scope=["repo-a"],
            source_operation_id=harness.root.operation_id,
            ops=[
                {
                    "op": "create_edges",
                    "edges": [
                        {
                            "source": "ev/review",
                            "target": "hyp/review",
                            "relation": "not_a_relation",
                        }
                    ],
                }
            ],
        ),
        raise_on_reject=False,
    )[0]
    assert rejected.admission == "rejected"
    assert harness.branch.head_ref().revision == rejected.revision
    for standing in ("accepted", "asserted"):
        _merge(
            harness,
            monkeypatch,
            {
                "op": "create_proposals",
                "proposals": [
                    _proposal(
                        {
                            "op": "set_standing",
                            "intent": "standing_change",
                            "node_id": "hyp/review",
                            "standing": standing,
                        }
                    )
                ],
            },
        )
        pending = [
            proposal
            for proposal in harness.service.history.state().proposals.values()
            if proposal.status == "pending"
        ]
        assert len(pending) == 1
        harness.service.decide_proposal(
            pending[0].id,
            ProposalDecisionRequest(decision="approved"),
            authorized_by=authorized_human(harness.app),
        )
        _human_patch(
            harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "asserted"}
        )
    receipts = harness.branch.validated_merge_receipts()
    assert sorted(receipt.provenance.branch_head.revision for receipt in receipts) == [
        rejected.revision,
        rejected.revision + 1,
    ]


def test_human_review_fact_retires_when_an_agent_writes_the_field_last(review_branch):
    harness = review_branch
    base_revision = harness.branch.branch_metadata().base_head.revision
    base = harness.branch.base_state()
    human = _human_patch(
        harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "accepted"}
    )
    branch = harness.branch.state()
    assert branch.nodes["hyp/review"].standing == "accepted"
    assert human.revision > base_revision
    assert [
        change.operation.standing for change in branch_human_review_changes([human], base, branch)
    ] == ["accepted"]
    # A later non-human writer owns the field even when it restores the human's value.
    agent_writes = [
        Patch(
            kind="work",
            author="agent",
            admission="accepted",
            revision=human.revision + offset,
            summary="Agent standing write.",
            run_truth_scope=["repo-a"],
            source_operation_id=harness.root.operation_id,
            ops=[{"op": "set_standing", "node_id": "hyp/review", "standing": standing}],
        )
        for offset, standing in ((1, "asserted"), (2, "accepted"))
    ]
    assert branch_human_review_changes([human, *agent_writes], base, branch) == []
    later_human = human.model_copy(update={"revision": human.revision + 3})
    assert [
        change.operation.standing
        for change in branch_human_review_changes([human, *agent_writes, later_human], base, branch)
    ] == ["accepted"]
