from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcp.core.materialize import materialize_patches_from_state, prepare_patch_bookkeeping
from rcp.core.models import (
    CURRENT_PROPOSAL_DEPENDENCY_RULE,
    BeliefTransition,
    GraphState,
    Hypothesis,
    Patch,
    Proposal,
    Standing,
)
from rcp.core.transitions import GraphTransitionManager
from rcp.core.validation import ValidationContext, validate_patch
from rcp.core.validation.proposals import (
    proposal_approval_operations,
    proposal_dependencies_and_effects,
    proposal_is_stale,
)
from rcp.runs.branch_merge import branch_human_review_changes
from rcp.runs.merge_contract import (
    FieldConflict,
    MergeAnswers,
    MergeAnswersRejected,
    MergePlan,
    MergeRefused,
    PriorReview,
    ReviewCard,
    ReviewGroup,
    SourceHumanFact,
    merge_contract_digest,
)
from rcp.runs.merge_patch import build_merge_patch
from rcp.runs.merge_plan import outstanding_review_changes
from rcp.service import ProposalDecisionRequest

from . import test_branch_merge_api as merge_api
from .helpers import authorized_human
from .test_merge_patch import MISSING, change, make_plan, plan_from_context, value


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


def _review_steps(harness, patches):
    record = harness.service.history_engine.steps(harness.branch.head_ref())[-1]
    return [
        replace(record, patch=patch, accepted=patch.admission == "accepted") for patch in patches
    ]


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


def _approve_branch_status(harness, node_id, *, edge_id="edge/review"):
    operation = {
        "op": "update_nodes",
        "intent": "status_change",
        "nodes": [
            {
                "id": node_id,
                "changes": {"status": "active"},
                "cause": {"kind": "evidence_edge", "ref_id": edge_id},
            }
        ],
    }
    identity = f"prop/source-status-{node_id.rsplit('/', 1)[-1]}"
    _agent_patch(
        harness, {"op": "create_proposals", "proposals": [_proposal(operation, identity=identity)]}
    )
    harness.service.for_graph_target(harness.branch.graph_target).decide_proposal(
        identity,
        ProposalDecisionRequest(decision="approved"),
        authorized_by=authorized_human(harness.app),
    )
    return harness.service.history_engine.steps(harness.branch.head_ref())[-1].patch


def _plan(harness, *, direct=(), reviews=(), facts=(), prior_reviews=(), footprint_state=None):
    """Hand-built planner output: each test names the exact semantic coordinates."""
    context = SimpleNamespace(
        merge_task_id="merge-task",
        authorized_by=authorized_human(harness.app),
        metadata=harness.branch.branch_metadata(),
        base_graph=harness.branch.base_state(),
        branch_graph=harness.branch.state(),
        main_head=harness.service.history.head_ref(),
        main_graph=harness.service.history.state(),
        run_truth_scope=["repo-a"],
    )

    def descriptor(coordinate):
        collection, identity, field = coordinate
        values = []
        for state in (context.base_graph, context.branch_graph, context.main_graph):
            entity = getattr(state, collection).get(identity)
            values.append(
                MISSING
                if entity is None
                else (entity.model_dump(mode="json") if field is None else getattr(entity, field))
            )
        return change(collection, identity, field, *values)

    groups = []
    for subject, coordinates, operations, *replacement in reviews:
        proposal = Proposal.model_validate(
            {
                **_proposal(operations[0]),
                "ops": operations,
                "dependency_rule": 3 if len(operations) > 1 else CURRENT_PROPOSAL_DEPENDENCY_RULE,
            }
        )
        groups.append(
            ReviewGroup(
                subject_key=subject,
                changes=tuple(descriptor(coordinate) for coordinate in coordinates),
                footprint=proposal_dependencies_and_effects(
                    footprint_state or context.main_graph, proposal
                ),
                effect_digest=merge_contract_digest(proposal.model_dump(mode="json")),
                replaces_proposal_ids=tuple(replacement[0]) if replacement else (),
            )
        )
    return plan_from_context(
        context,
        direct_changes=tuple(descriptor(coordinate) for coordinate in direct),
        review_groups=tuple(groups),
        source_human_facts=facts,
        prior_reviews=prior_reviews,
    )


def _answers(plan):
    return MergeAnswers(
        answers={},
        cards={
            group.subject_key: ReviewCard.model_validate(_proposal(_content())["card"])
            for group in plan.review_groups
        },
    )


def _apply(plan, patch):
    state = plan.destination_graph
    patch = patch.model_copy(update={"revision": state.revision + 1})
    patch = prepare_patch_bookkeeping(state, patch)
    report = validate_patch(state, patch, ValidationContext(state.project_truth_scope))
    assert not report.rejected, [(item.code, item.message) for item in report.messages]
    prepared = GraphTransitionManager().prepare_validated(
        state, [patch], pre_head=plan.destination_head
    )
    return _replay(state, prepared)


def _replay(state, prepared):
    persisted = Patch.model_validate_json(prepared.patch.model_dump_json())
    replayed = materialize_patches_from_state(
        [persisted], initial_state=state, context=ValidationContext(state.project_truth_scope)
    )
    assert replayed.state.replay_status == "complete"
    assert replayed.state.model_dump(
        exclude={"validation_messages"}
    ) == prepared.projection.graph.model_dump(exclude={"validation_messages"})
    return prepared.projection.graph


def _approve(state, proposal):
    proposal = state.proposals[proposal.id]
    assert not proposal_is_stale(state, proposal)
    patch = Patch(
        revision=state.revision + 1,
        kind="approval",
        author="human",
        summary="Approve the merge review.",
        ops=proposal_approval_operations(state, proposal),
    )
    report = validate_patch(state, patch, ValidationContext(state.project_truth_scope))
    assert not report.rejected, [(item.code, item.message) for item in report.messages]
    return _replay(state, GraphTransitionManager().prepare_validated(state, [patch]))


def _filed(patch):
    return [
        proposal
        for operation in patch.ops
        if operation.op == "create_proposals"
        for proposal in operation.proposals
    ]


def _standing(node_id, standing):
    return {
        "op": "set_standing",
        "intent": "standing_change",
        "node_id": node_id,
        "standing": standing,
    }


def test_merge_commits_direct_changes_and_pending_protected_review_atomically(review_branch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    _edit(harness, "blk/merge-ready", {"status": "resolved"})
    plan = _plan(
        harness,
        direct=[("nodes", "blk/merge-ready", "status")],
        reviews=[("question", [("nodes", "rq/review", "question")], [_content()])],
    )
    patch = build_merge_patch(plan, _answers(plan))
    main = _apply(plan, patch)
    assert main.revision == plan.destination_graph.revision + 1
    assert main.nodes["rq/review"].question == "Original?"
    assert main.nodes["blk/merge-ready"].status == "resolved"
    proposal = next(iter(main.proposals.values()))
    assert proposal.status == "pending"
    assert _approve(main, proposal).nodes["rq/review"].question == "Revised?"
    assert plan.destination_graph.nodes["blk/merge-ready"].status != "resolved"


@pytest.mark.parametrize("standing", ["accepted", "contested"])
def test_human_branch_standing_returns_to_main_inbox(review_branch, standing):
    harness = review_branch
    _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": standing})
    plan = _plan(
        harness,
        reviews=[
            (
                "hypothesis",
                [("nodes", "hyp/review", "standing")],
                [_standing("hyp/review", standing)],
            )
        ],
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert main.nodes["hyp/review"].standing == "asserted"
    pending = next(iter(main.proposals.values()))
    assert pending.related_node_ids == ["hyp/review"]
    assert _approve(main, pending).nodes["hyp/review"].standing == standing


def test_same_node_content_and_standing_review_approves_atomically(review_branch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    _human_patch(harness, {"op": "set_standing", "node_id": "rq/review", "standing": "contested"})
    plan = _plan(
        harness,
        reviews=[
            (
                "question",
                [("nodes", "rq/review", "question"), ("nodes", "rq/review", "standing")],
                [_content(), _standing("rq/review", "contested")],
            )
        ],
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert len(main.proposals) == 1
    approved = _approve(main, next(iter(main.proposals.values())))
    assert approved.revision == main.revision + 1
    assert approved.nodes["rq/review"].question == "Revised?"
    assert approved.nodes["rq/review"].standing == "contested"


@pytest.mark.parametrize("recorded_cause", ["human_edit", "evidence_edge"])
@pytest.mark.parametrize("include_other_edits", [False, True])
def test_retired_evidence_requires_the_exact_recorded_status_cause(
    review_branch, include_other_edits, recorded_cause
):
    harness = review_branch
    operation = {
        "op": "update_nodes",
        "intent": "status_change",
        "nodes": [
            {"id": "hyp/review", "changes": {"status": "active"}, "cause": {"kind": "human_edit"}}
        ],
    }
    human = _approve_branch_status(harness, "hyp/review")
    _human_patch(harness, {"op": "remove_edges", "edge_ids": ["edge/review"]})
    coordinates = [("nodes", "hyp/review", "status")]
    operations = [operation]
    if include_other_edits:
        _edit(harness, "hyp/review", {"statement": "Revised claim."})
        _human_patch(
            harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "contested"}
        )
        coordinates.extend(
            [("nodes", "hyp/review", "statement"), ("nodes", "hyp/review", "standing")]
        )
        operations.extend(
            [
                {
                    "op": "update_nodes",
                    "intent": "content_change",
                    "nodes": [{"id": "hyp/review", "changes": {"statement": "Revised claim."}}],
                },
                _standing("hyp/review", "contested"),
            ]
        )
    fact = SourceHumanFact(revision=human.revision, operation_index=0, operation=operation)
    plan = _plan(
        harness,
        direct=[("edges", "edge/review", None)],
        reviews=[("hypothesis", coordinates, operations)],
        facts=(fact,),
    )
    if recorded_cause == "evidence_edge":
        with pytest.raises(MergeRefused) as rejected:
            build_merge_patch(plan, _answers(plan))
        assert {(item.code, item.related_node_ids) for item in rejected.value.diagnostics} == {
            ("invalid-belief-cause", ("hyp/review",))
        }
        return
    source = plan.source_graph.model_copy(
        update={
            "belief_transitions": [
                transition.model_copy(update={"cause": {"kind": "human_edit"}})
                if transition.revision == human.revision
                and transition.hypothesis_id == "hyp/review"
                else transition
                for transition in plan.source_graph.belief_transitions
            ]
        }
    )
    plan = MergePlan.model_validate(
        {**plan.model_dump(mode="json", exclude={"plan_id"}), "source_graph": source}
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert "edge/review" not in main.edges
    assert main.nodes["hyp/review"].status == "proposed"
    pending = next(iter(main.proposals.values()))
    status = next(op for op in pending.ops if op.intent == "status_change")
    assert status.nodes[0].cause.kind == "human_edit"
    approved = _approve(main, pending)
    assert approved.nodes["hyp/review"].status == "active"
    assert approved.nodes["hyp/review"].standing == (
        "contested" if include_other_edits else "accepted"
    )


@pytest.mark.parametrize("review_branch", ["accepted_blocker"], indirect=True)
def test_removed_accepted_ordinary_node_requires_main_review(review_branch):
    harness = review_branch
    _human_patch(
        harness, {"op": "set_standing", "node_id": "blk/merge-ready", "standing": "contested"}
    )
    _human_patch(harness, {"op": "remove_nodes", "node_ids": ["blk/merge-ready"]})
    plan = _plan(
        harness,
        reviews=[
            (
                "blocker",
                [("nodes", "blk/merge-ready", None)],
                [{"op": "remove_nodes", "intent": "removal", "node_ids": ["blk/merge-ready"]}],
            )
        ],
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert main.nodes["blk/merge-ready"].standing == "accepted"
    assert "blk/merge-ready" not in _approve(main, next(iter(main.proposals.values()))).nodes


@pytest.mark.parametrize("review_branch", ["accepted_blocker"], indirect=True)
def test_ordinary_content_merge_keeps_the_standing_both_sides_agree_on(review_branch):
    _edit(review_branch, "blk/merge-ready", {"description": "Revised description."})
    plan = _plan(review_branch, direct=[("nodes", "blk/merge-ready", "description")])
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert main.nodes["blk/merge-ready"].standing == "accepted"
    assert main.nodes["blk/merge-ready"].description == "Revised description."
    assert not main.proposals


def test_live_source_proposal_keeps_identity_and_authored_content_with_destination_stamps(
    review_branch,
):
    harness = review_branch
    _agent_patch(
        harness,
        {
            "op": "create_proposals",
            "proposals": [_proposal(_content(), identity="prop/live-source")],
        },
    )
    source = harness.branch.state().proposals["prop/live-source"]
    plan = _plan(harness, direct=[("proposals", source.id, None)])
    patch = build_merge_patch(plan, _answers(plan))
    main = _apply(plan, patch)
    carried = main.proposals[source.id]
    assert (carried.id, carried.title, carried.card, carried.ops) == (
        source.id,
        source.title,
        source.card,
        source.ops,
    )
    assert carried.status == "pending"
    assert carried.base_rev == plan.destination_graph.revision
    assert carried.raised_rev == main.revision
    assert carried.created_by_operation_id == plan.merge_task_id
    assert carried.dependency_rule == CURRENT_PROPOSAL_DEPENDENCY_RULE
    assert main.nodes["rq/review"].question == "Original?"


@pytest.mark.parametrize("remove", ["node", "relation"])
def test_protected_removal_is_reviewed_with_its_exact_incident_graph_effects(review_branch, remove):
    harness = review_branch
    operation = (
        {"op": "remove_nodes", "node_ids": ["rq/review"]}
        if remove == "node"
        else {"op": "remove_edges", "edge_ids": ["edge/question-hypothesis"]}
    )
    _human_patch(harness, operation)
    coordinate = (
        ("nodes", "rq/review", None)
        if remove == "node"
        else ("edges", "edge/question-hypothesis", None)
    )
    plan = _plan(
        harness,
        reviews=[
            (
                "question",
                [coordinate],
                [
                    {
                        **operation,
                        "intent": "removal" if remove == "node" else "protected_relation_change",
                    }
                ],
            )
        ],
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert "edge/question-hypothesis" in main.edges
    approved = _approve(main, next(iter(main.proposals.values())))
    assert "edge/question-hypothesis" not in approved.edges
    assert ("rq/review" not in approved.nodes) == (remove == "node")


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
            ValidationContext(state.project_truth_scope),
        )
        assert report.rejected


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
        change.operation.standing
        for change in branch_human_review_changes(_review_steps(harness, [human]), base, branch)
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
    assert (
        branch_human_review_changes(_review_steps(harness, [human, *agent_writes]), base, branch)
        == []
    )
    later_human = human.model_copy(update={"revision": human.revision + 3})
    assert [
        change.operation.standing
        for change in branch_human_review_changes(
            _review_steps(harness, [human, *agent_writes, later_human]), base, branch
        )
    ] == ["accepted"]


def test_remerge_withdraws_pending_predecessor_and_keeps_outstanding_effect(review_branch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?", "motivation": "New motivation."})
    operation = _content(motivation="New motivation.")
    plan = _plan(
        harness,
        reviews=[
            (
                "question",
                [("nodes", "rq/review", "question"), ("nodes", "rq/review", "motivation")],
                [operation],
            )
        ],
    )
    predecessor = Proposal.model_validate(
        {
            **_proposal(_content(), identity="prop/earlier-merge"),
            "base_rev": plan.destination_graph.revision,
            "raised_rev": plan.destination_graph.revision,
            "dependency_rule": CURRENT_PROPOSAL_DEPENDENCY_RULE,
            "related_node_ids": ["rq/review"],
        }
    )
    destination = plan.destination_graph.model_copy(
        update={
            "proposals": {predecessor.id: predecessor},
        }
    )
    group = plan.review_groups[0].model_copy(
        update={
            "replaces_proposal_ids": (predecessor.id,),
        }
    )
    prior = PriorReview(
        outstanding=outstanding_review_changes(
            plan.destination_graph,
            destination,
            predecessor,
            created_at=plan.captured_authority.patch_template.created_at,
        ),
        subject_key=group.subject_key,
        proposal=predecessor,
        provenance=plan.provenance,
        effect_digest=group.effect_digest,
    )
    plan = MergePlan.model_validate(
        {
            **plan.model_dump(mode="json", exclude={"plan_id"}),
            "destination_graph": destination,
            "review_groups": (group,),
            "prior_reviews": (prior,),
        }
    )
    patch = build_merge_patch(plan, _answers(plan))
    main = _apply(plan, patch)
    assert main.proposals[predecessor.id].status == "withdrawn"
    pending = [item for item in main.proposals.values() if item.status == "pending"]
    assert len(pending) == 1
    approved = _approve(main, pending[0])
    assert approved.nodes["rq/review"].question == "Revised?"
    assert approved.nodes["rq/review"].motivation == "New motivation."
    assert main.revision == plan.destination_graph.revision + 1


def test_builder_uses_plan_effects_and_cards_cannot_smuggle_operations(review_branch):
    _edit(review_branch, "rq/review", {"question": "Revised?"})
    plan = _plan(
        review_branch, reviews=[("question", [("nodes", "rq/review", "question")], [_content()])]
    )
    answers = _answers(plan)
    ordinary = build_merge_patch(plan, answers)
    card = answers.cards["question"].model_copy(
        update={
            "consequences": '{"op":"remove_nodes","node_ids":["rq/review"]}',
        }
    )
    changed = build_merge_patch(plan, answers.model_copy(update={"cards": {"question": card}}))
    first, second = _filed(ordinary)[0], _filed(changed)[0]
    assert first.ops == second.ops
    assert first.id == second.id
    assert second.card.consequences == card.consequences
    assert (
        _approve(_apply(plan, changed), _filed(changed)[0]).nodes["rq/review"].question
        == "Revised?"
    )


def test_protected_edge_replacement_is_one_atomic_review(review_branch):
    harness = review_branch
    original = harness.branch.state().edges["edge/question-hypothesis"]
    replacement = original.model_dump(mode="json", exclude={"created_rev", "layer"})
    replacement["explanation"] = "Updated relation explanation."
    _human_patch(
        harness,
        {"op": "remove_edges", "edge_ids": [original.id]},
        {"op": "create_edges", "edges": [replacement]},
    )
    operations = [
        {"op": "remove_edges", "intent": "protected_relation_change", "edge_ids": [original.id]},
        {"op": "create_edges", "intent": "protected_relation_change", "edges": [replacement]},
    ]
    plan = _plan(
        harness,
        reviews=[("question-and-hypothesis", [("edges", original.id, "explanation")], operations)],
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert main.edges[original.id].explanation == original.explanation
    assert len(main.proposals) == 1
    pending = next(iter(main.proposals.values()))
    assert [item.op for item in pending.ops] == ["remove_edges", "create_edges"]
    approved = _approve(main, pending)
    assert approved.edges[original.id].explanation == replacement["explanation"]


def test_review_of_a_hypothesis_the_merge_creates_is_not_stale(review_branch):
    harness = review_branch
    node = {
        "id": "hyp/new",
        "type": "hypothesis",
        "title": "New hypothesis",
        "statement": "A branch claim.",
    }
    edge = {
        "id": "edge/question-new",
        "source": "rq/review",
        "target": "hyp/new",
        "relation": "has_hypothesis",
    }
    _agent_patch(
        harness,
        {"op": "create_nodes", "nodes": [node]},
        {
            "op": "create_edges",
            "edges": [
                edge,
                {
                    "id": "edge/new-evidence",
                    "source": "ev/review",
                    "target": "hyp/new",
                    "relation": "supports",
                    "assessment": {
                        "relevance": "direct",
                        "weight": "moderate",
                        "scope": "New claim evidence.",
                    },
                },
            ],
        },
    )
    human = _approve_branch_status(harness, "hyp/new", edge_id="edge/new-evidence")
    _human_patch(harness, {"op": "set_standing", "node_id": "hyp/new", "standing": "accepted"})
    status = {
        "op": "update_nodes",
        "intent": "status_change",
        "nodes": [
            {
                "id": "hyp/new",
                "changes": {"status": "active"},
                "cause": {"kind": "evidence_edge", "ref_id": "edge/new-evidence"},
            }
        ],
    }
    source_node = harness.branch.state().nodes["hyp/new"]
    destination = harness.service.history.state()
    footprint_state = destination.model_copy(
        update={
            "revision": destination.revision + 1,
            "edges": {
                **destination.edges,
                "edge/new-evidence": harness.branch.state()
                .edges["edge/new-evidence"]
                .model_copy(update={"created_rev": destination.revision + 1}),
            },
            "nodes": {
                **destination.nodes,
                "hyp/new": source_node.model_copy(
                    update={
                        "status": "proposed",
                        "standing": Standing.ASSERTED,
                        "created_rev": destination.revision + 1,
                        "updated_rev": destination.revision + 1,
                    }
                ),
            },
        }
    )
    plan = _plan(
        harness,
        direct=[("edges", edge["id"], None), ("edges", "edge/new-evidence", None)],
        reviews=[
            (
                "new-hypothesis",
                [("nodes", "hyp/new", None)],
                [status, _standing("hyp/new", "accepted")],
            )
        ],
        facts=(SourceHumanFact(revision=human.revision, operation_index=0, operation=status),),
        footprint_state=footprint_state,
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert main.nodes["hyp/new"].status == "proposed"
    assert main.nodes["hyp/new"].standing == "asserted"
    assert edge["id"] in main.edges
    pending = next(iter(main.proposals.values()))
    assert pending.status == "pending"
    assert not proposal_is_stale(main, pending)
    approved = _approve(main, pending)
    assert approved.nodes["hyp/new"].status == "active"
    assert approved.nodes["hyp/new"].standing == "accepted"


def test_nonhuman_latest_same_status_occurrence_does_not_inherit_an_older_human_fact(review_branch):
    harness = review_branch
    _approve_branch_status(harness, "hyp/review")
    cause = {"kind": "evidence_edge", "ref_id": "edge/review"}
    status = {
        "op": "update_nodes",
        "intent": "status_change",
        "nodes": [{"id": "hyp/review", "changes": {"status": "active"}, "cause": cause}],
    }
    stale_fact = SourceHumanFact(
        revision=5,
        operation_index=0,
        operation={
            **status,
            "nodes": [
                {
                    "id": "hyp/review",
                    "changes": {"status": "active"},
                    "cause": {"kind": "human_edit"},
                }
            ],
        },
    )
    plan = _plan(
        harness,
        reviews=[("hypothesis", [("nodes", "hyp/review", "status")], [status])],
        facts=(stale_fact,),
    )
    source = plan.source_graph.model_copy(
        update={
            "revision": 7,
            "belief_transitions": [
                BeliefTransition(
                    hypothesis_id="hyp/review",
                    from_status="proposed",
                    to_status="active",
                    revision=5,
                    cause={"kind": "human_edit"},
                ),
                BeliefTransition(
                    hypothesis_id="hyp/review",
                    from_status="active",
                    to_status="refuted",
                    revision=6,
                    cause=cause,
                ),
                BeliefTransition(
                    hypothesis_id="hyp/review",
                    from_status="refuted",
                    to_status="active",
                    revision=7,
                    cause=cause,
                ),
            ],
        }
    )
    plan = make_plan(
        base=plan.base_graph,
        source=source,
        destination=plan.destination_graph,
        review_groups=plan.review_groups,
        source_human_facts=(stale_fact,),
    )
    patch = build_merge_patch(plan, _answers(plan))
    update = next(
        operation for operation in _filed(patch)[0].ops if operation.intent == "status_change"
    )
    assert update.nodes[0].cause.model_dump() == cause
    main = _apply(plan, patch)
    assert (
        _approve(main, next(iter(main.proposals.values()))).nodes["hyp/review"].status == "active"
    )


def test_review_requires_its_subject_keyed_card(review_branch):
    _edit(review_branch, "rq/review", {"question": "Revised?"})
    plan = _plan(
        review_branch, reviews=[("question", [("nodes", "rq/review", "question")], [_content()])]
    )
    with pytest.raises(MergeAnswersRejected) as caught:
        build_merge_patch(plan, MergeAnswers(answers={}, cards={}))
    assert [(item.code, item.related_node_ids) for item in caught.value.diagnostics] == [
        ("invalid-merge-answer", ("question",))
    ]


@pytest.mark.parametrize(
    "field,original,reviewed",
    [("standing", "asserted", "accepted"), ("status", "proposed", "active")],
)
def test_later_merge_reviews_judgment_reverted_to_the_original_base(field, original, reviewed):
    node = Hypothesis(id="hyp/review", type="hypothesis", title="Hypothesis", statement="Claim.")
    base = GraphState(revision=1, project_truth_scope=["repo-a"], nodes={node.id: node})
    source = base.model_copy(update={"revision": 3})
    if field == "status":
        source = source.model_copy(
            update={
                "belief_transitions": [
                    BeliefTransition(
                        hypothesis_id=node.id,
                        from_status=reviewed,
                        to_status=original,
                        revision=3,
                        cause={"kind": "human_edit"},
                    )
                ]
            }
        )
    destination = base.model_copy(
        update={
            "revision": 2,
            "nodes": {
                node.id: node.model_copy(
                    update={field: Standing(reviewed) if field == "standing" else reviewed}
                )
            },
        }
    )
    operation = (
        _standing(node.id, original)
        if field == "standing"
        else {
            "op": "update_nodes",
            "intent": "status_change",
            "nodes": [
                {"id": node.id, "changes": {"status": original}, "cause": {"kind": "human_edit"}}
            ],
        }
    )
    draft = Proposal.model_validate(
        {**_proposal(operation), "dependency_rule": CURRENT_PROPOSAL_DEPENDENCY_RULE}
    )
    group = ReviewGroup(
        subject_key="hypothesis",
        changes=(change("nodes", node.id, field, original, original, reviewed),),
        footprint=proposal_dependencies_and_effects(destination, draft),
        effect_digest=merge_contract_digest(draft.model_dump(mode="json")),
    )
    plan = make_plan(
        base=base,
        source=source,
        destination=destination,
        review_groups=(group,),
        source_human_facts=(SourceHumanFact(revision=3, operation_index=0, operation=operation),),
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert getattr(main.nodes[node.id], field) == reviewed
    approved = _approve(main, next(iter(main.proposals.values())))
    assert getattr(approved.nodes[node.id], field) == original


def test_stale_source_proposal_stays_on_branch_while_its_edit_is_reviewed(review_branch):
    harness = review_branch
    _agent_patch(
        harness,
        {
            "op": "create_proposals",
            "proposals": [_proposal(_content(), identity="prop/old-source")],
        },
    )
    _edit(harness, "rq/review", {"question": "Revised?"})
    assert proposal_is_stale(
        harness.branch.state(), harness.branch.state().proposals["prop/old-source"]
    )
    plan = _plan(
        harness, reviews=[("question", [("nodes", "rq/review", "question")], [_content()])]
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert len(main.proposals) == 1
    assert "prop/old-source" not in main.proposals
    assert plan.source_graph.proposals["prop/old-source"].status == "pending"


@pytest.mark.parametrize("resolution", ["rejected", "approved"])
def test_later_branch_head_does_not_resurrect_resolved_prior_review(review_branch, resolution):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    _edit(harness, "blk/merge-ready", {"status": "resolved"})
    plan = _plan(harness, direct=[("nodes", "blk/merge-ready", "status")])
    prior = Proposal.model_validate(
        {
            **_proposal(_content(), identity="prop/earlier-merge"),
            "status": resolution,
            "resolved_rev": plan.destination_graph.revision,
            "resolved_by": "human",
            "dependency_rule": CURRENT_PROPOSAL_DEPENDENCY_RULE,
        }
    )
    destination = plan.destination_graph.model_copy(update={"proposals": {prior.id: prior}})
    if resolution == "approved":
        destination.nodes["rq/review"] = destination.nodes["rq/review"].model_copy(
            update={"question": "A later destination judgment?"}
        )
    review = PriorReview(
        outstanding=outstanding_review_changes(
            plan.destination_graph,
            destination,
            prior,
            created_at=plan.captured_authority.patch_template.created_at,
        ),
        subject_key="question",
        proposal=prior,
        provenance=plan.provenance,
        effect_digest=merge_contract_digest(prior.model_dump(mode="json")),
    )
    plan = MergePlan.model_validate(
        {
            **plan.model_dump(mode="json", exclude={"plan_id"}),
            "destination_graph": destination,
            "prior_reviews": (review,),
        }
    )
    main = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert set(main.proposals) == {prior.id}
    assert main.proposals[prior.id].status == resolution
    assert main.nodes["rq/review"].question == destination.nodes["rq/review"].question
    assert main.nodes["blk/merge-ready"].status == "resolved"


def test_merge_accepts_source_head_ending_on_a_rejected_patch(review_branch):
    harness = review_branch
    _human_patch(harness, {"op": "set_standing", "node_id": "hyp/review", "standing": "accepted"})
    rejected = harness.branch.append(
        Patch(
            kind="work",
            author="agent",
            summary="Rejected source tail.",
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
    plan = _plan(
        harness,
        reviews=[
            (
                "hypothesis",
                [("nodes", "hyp/review", "standing")],
                [_standing("hyp/review", "accepted")],
            )
        ],
    )
    patch = build_merge_patch(plan, _answers(plan))
    assert patch.branch_merge.branch_head.revision == rejected.revision
    main = _apply(plan, patch)
    assert (
        _approve(main, next(iter(main.proposals.values()))).nodes["hyp/review"].standing
        == "accepted"
    )


def test_rejected_human_request_does_not_prove_an_agent_source_judgment(review_branch):
    harness = review_branch
    _agent_patch(
        harness, {"op": "set_standing", "node_id": "blk/merge-ready", "standing": "accepted"}
    )
    rejected = Patch(
        revision=harness.branch.state().revision,
        kind="approval",
        author="human",
        admission="rejected",
        summary="A rejected judgment is not source proof.",
        ops=[{"op": "set_standing", "node_id": "blk/merge-ready", "standing": "accepted"}],
    )
    assert (
        branch_human_review_changes(
            _review_steps(harness, [rejected]), harness.branch.base_state(), harness.branch.state()
        )
        == []
    )
    plan = _plan(harness, direct=[("nodes", "blk/merge-ready", "standing")])
    assert not _filed(build_merge_patch(plan, _answers(plan)))


def test_review_identity_survives_unrelated_destination_advance(review_branch):
    _edit(review_branch, "rq/review", {"question": "Revised?"})
    plan = _plan(
        review_branch, reviews=[("question", [("nodes", "rq/review", "question")], [_content()])]
    )
    original = _filed(build_merge_patch(plan, _answers(plan)))[0]
    destination = plan.destination_graph.model_copy(
        deep=True, update={"revision": plan.destination_graph.revision + 1}
    )
    destination.nodes["blk/merge-ready"] = destination.nodes["blk/merge-ready"].model_copy(
        update={"description": "An unrelated destination edit."}
    )
    context = SimpleNamespace(
        metadata=SimpleNamespace(
            branch_id=plan.provenance.branch_id,
            episode_id=plan.provenance.episode_id,
            project_id=plan.captured_authority.project_id,
            effective_parent_head=plan.base_head,
            head=plan.source_head,
        ),
        merge_task_id=plan.merge_task_id,
        authorized_by=plan.authorized_by,
        main_head=plan.destination_head.model_copy(
            update={"revision": destination.revision, "transition_id": "e" * 64}
        ),
        base_graph=plan.base_graph,
        branch_graph=plan.source_graph,
        main_graph=destination,
        run_truth_scope=plan.run_truth_scope,
    )
    rebased = plan_from_context(context, review_groups=plan.review_groups)
    replacement = _filed(build_merge_patch(rebased, _answers(rebased)))[0]
    assert replacement.id == original.id
    main = _apply(rebased, build_merge_patch(rebased, _answers(rebased)))
    assert main.nodes["blk/merge-ready"].description == "An unrelated destination edit."


def test_remerge_does_not_restore_prior_field_declined_by_answer(review_branch):
    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?", "title": "Branch title"})
    plan = _plan(
        harness,
        reviews=[
            (
                "question",
                [("nodes", "rq/review", "question"), ("nodes", "rq/review", "title")],
                [_content(title="Branch title")],
            )
        ],
    )
    prior = Proposal.model_validate(
        {
            **_proposal(
                {
                    "op": "update_nodes",
                    "intent": "content_change",
                    "nodes": [{"id": "rq/review", "changes": {"title": "Branch title"}}],
                },
                identity="prop/earlier-title",
            ),
            "base_rev": plan.destination_graph.revision,
            "raised_rev": plan.destination_graph.revision,
            "dependency_rule": CURRENT_PROPOSAL_DEPENDENCY_RULE,
            "related_node_ids": ["rq/review"],
        }
    )
    destination = plan.destination_graph.model_copy(
        deep=True, update={"proposals": {prior.id: prior}}
    )
    destination.nodes["rq/review"] = destination.nodes["rq/review"].model_copy(
        update={"title": "Destination title"}
    )
    conflict = FieldConflict(
        conflict_id="question-title",
        collection="nodes",
        entity_id="rq/review",
        field_name="title",
        base=value("Question"),
        branch=value("Branch title"),
        destination=value("Destination title"),
        allowed_answer_kinds=("branch", "destination", "merged"),
    )
    group = plan.review_groups[0].model_copy(
        update={
            "changes": tuple(
                change.model_copy(update={"destination": value("Destination title")})
                if change.field_name == "title"
                else change
                for change in plan.review_groups[0].changes
            ),
            "replaces_proposal_ids": (prior.id,),
        }
    )
    review = PriorReview(
        outstanding=outstanding_review_changes(
            plan.destination_graph,
            destination,
            prior,
            created_at=plan.captured_authority.patch_template.created_at,
        ),
        subject_key=group.subject_key,
        proposal=prior,
        provenance=plan.provenance,
        effect_digest=group.effect_digest,
    )
    plan = MergePlan.model_validate(
        {
            **plan.model_dump(mode="json", exclude={"plan_id"}),
            "destination_graph": destination,
            "review_groups": (group,),
            "prior_reviews": (review,),
            "conflicts": (conflict,),
        }
    )
    answers = MergeAnswers(
        answers={conflict.conflict_id: {"kind": "destination"}}, cards=_answers(plan).cards
    )
    main = _apply(plan, build_merge_patch(plan, answers))
    assert main.proposals[prior.id].status == "withdrawn"
    pending = [proposal for proposal in main.proposals.values() if proposal.status == "pending"]
    assert len(pending) == 1
    approved = _approve(main, pending[0])
    assert approved.nodes["rq/review"].title == "Destination title"
    assert approved.nodes["rq/review"].question == "Revised?"


@pytest.mark.parametrize("node_id,reviewed", [("blk/merge-ready", False), ("hyp/review", True)])
def test_standing_uses_core_admission_instead_of_its_planned_review_slot(
    review_branch, node_id, reviewed
):
    harness = review_branch
    _human_patch(harness, {"op": "set_standing", "node_id": node_id, "standing": "accepted"})
    plan = _plan(
        harness,
        reviews=[("judgment", [("nodes", node_id, "standing")], [_standing(node_id, "accepted")])],
    )
    patch = build_merge_patch(plan, _answers(plan))
    main = _apply(plan, patch)
    assert bool(main.proposals) is reviewed
    assert main.nodes[node_id].standing == ("asserted" if reviewed else "accepted")
    if reviewed:
        assert (
            _approve(main, next(iter(main.proposals.values()))).nodes[node_id].standing
            == "accepted"
        )


def test_carried_source_proposal_joins_and_approves_with_overlapping_merge_review(review_branch):
    from rcp.runs.merge_contract import MergePlanningContext
    from rcp.runs.merge_plan import plan_merge

    harness = review_branch
    _edit(harness, "rq/review", {"question": "Revised?"})
    source_proposal = _proposal(
        _content(question="A further source proposal?"), identity="prop/live-source"
    )
    _agent_patch(harness, {"op": "create_proposals", "proposals": [source_proposal]})
    captured = _plan(harness)
    context = MergePlanningContext.model_validate(
        captured.model_dump(exclude={"plan_id", "direct_changes", "conflicts", "review_groups"})
    )
    plan = plan_merge(context)
    assert len(plan.review_groups) == 1
    assert plan.review_groups[0].carried_proposal_ids == ("prop/live-source",)
    merged = _apply(plan, build_merge_patch(plan, _answers(plan)))
    assert "prop/live-source" not in merged.proposals
    assert len(merged.proposals) == 1
    assert merged.nodes["rq/review"].question == "Original?"
    approved = _approve(merged, next(iter(merged.proposals.values())))
    assert approved.nodes["rq/review"].question == "A further source proposal?"
    assert (
        plan.source_graph.proposals["prop/live-source"].model_dump(mode="json")["ops"]
        == source_proposal["ops"]
    )


def test_stale_source_proposal_omission_remains_an_advisory(review_branch):
    harness = review_branch
    _agent_patch(
        harness,
        {
            "op": "create_proposals",
            "proposals": [_proposal(_content(), identity="prop/stale-source")],
        },
    )
    _edit(harness, "rq/review", {"question": "Revised?"})
    _edit(harness, "blk/merge-ready", {"description": "Ordinary source work."})
    plan = _plan(
        harness,
        direct=[
            ("proposals", "prop/stale-source", None),
            ("nodes", "blk/merge-ready", "description"),
        ],
    )
    patch = build_merge_patch(plan, _answers(plan))
    assert not _filed(patch)
    assert [
        (item.level, item.code, item.related_node_ids) for item in patch.admission_messages
    ] == [("flag", "stale-source-proposal", ["prop/stale-source"])]
    from rcp.core.validation import ValidationReport
    from rcp.runs.merge_patch import prepare_merge_candidate

    from .test_merge_patch import DiagnosticHistory

    prepared = prepare_merge_candidate(
        plan, _answers(plan), DiagnosticHistory(plan, ValidationReport())
    )
    assert [
        (item.level, item.code, item.related_node_ids) for item in prepared.patch.admission_messages
    ] == [("flag", "stale-source-proposal", ["prop/stale-source"])]
    main = _apply(plan, patch)
    assert main.nodes["blk/merge-ready"].description == "Ordinary source work."


def test_merge_carries_a_proposal_on_a_belief_the_branch_created(review_branch):
    # Main gains the Hypothesis and its pending change in the same revision.
    harness = review_branch
    _agent_patch(
        harness,
        {
            "op": "create_nodes",
            "nodes": [
                {
                    "id": "hyp/branch-born",
                    "type": "hypothesis",
                    "title": "Branch hypothesis",
                    "statement": "Original branch claim.",
                }
            ],
        },
    )
    operation = {
        "op": "update_nodes",
        "intent": "content_change",
        "nodes": [{"id": "hyp/branch-born", "changes": {"statement": "Revised branch claim."}}],
    }
    _agent_patch(
        harness,
        {"op": "create_proposals", "proposals": [_proposal(operation, identity="prop/born")]},
    )
    url = f"/api/projects/{harness.project_id}/episodes/{harness.episode.episode_id}/merge-preview"
    response = harness.client.get(url)
    assert response.status_code == 200, response.json()
    assert response.json()["graph"]["direct"] > 0
